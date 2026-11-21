<?php
// Se instala fuera de public_html y se invoca únicamente por SSH.
declare(strict_types=1);
umask(0077);
$lock = null;
try {
    if (PHP_SAPI !== 'cli') { http_response_code(404); exit(1); }
    $root = $argv[1] ?? '';
    if (!str_starts_with($root, '/home/') || str_contains($root, '/public_html') || str_contains($root, '..')) {
        throw new RuntimeException('Raíz privada inválida');
    }
    $raw = stream_get_contents(STDIN, 4194305);
    if (strlen($raw) > 4194304) { throw new RuntimeException('Solicitud demasiado grande'); }
    $input = json_decode($raw, true, 512, JSON_THROW_ON_ERROR);
    $key = $input['key'] ?? '';
    if (!is_string($key) || $key === '' || str_starts_with($key, '/') || str_contains($key, '\\')) {
        throw new RuntimeException('Ruta inválida');
    }
    foreach (explode('/', $key) as $part) {
        if ($part === '' || $part === '.' || $part === '..') { throw new RuntimeException('Ruta inválida'); }
    }
    $physical = $key;
    $member = null;
    if (preg_match('#^(runs/[^/]+/results/(?:companias|personas))/([0-9]{13}\.json)$#', $key, $matches)) {
        $physical = $matches[1] . '/_packs/' . substr(hash('sha256', $key), 0, 3) . '.json';
        $member = $matches[2];
    }
    $path = $root . '/objects/' . $physical;
    $lockDir = $root . '/.locks';
    if (!is_dir($lockDir) && !mkdir($lockDir, 0700, true) && !is_dir($lockDir)) {
        throw new RuntimeException('No se puede crear el directorio de coordinación');
    }
    $lock = fopen($lockDir . '/' . hash('sha256', $physical) . '.lock', 'c');
    if (!$lock) { throw new RuntimeException('No se puede abrir el bloqueo'); }
    $deadline = microtime(true) + 10;
    while (!flock($lock, LOCK_EX | LOCK_NB)) {
        if (microtime(true) >= $deadline) { throw new RuntimeException('Bloqueo ocupado'); }
        usleep(25000);
    }
    $generation = 0;
    $pack = [];
    if (is_file($path)) {
        $old = json_decode(file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if ($member !== null) {
            $pack = $old;
            $old = $pack[$member] ?? null;
        }
        if ($old !== null) {
            if (($old['_websr_store'] ?? 0) !== 1) { throw new RuntimeException('Objeto sin control de versión'); }
            $generation = $old['generation'];
        }
    }
    if (!is_int($input['generation'] ?? null) || $input['generation'] !== $generation) {
        echo json_encode(['ok' => false, 'conflict' => true]); exit(0);
    }
    $dir = dirname($path);
    if (!is_dir($dir) && !mkdir($dir, 0700, true) && !is_dir($dir)) { throw new RuntimeException('No se puede crear la carpeta'); }
    $envelope = ['_websr_store' => 1, 'generation' => $generation + 1, 'value' => $input['value']];
    if ($member !== null) { $pack[$member] = $envelope; }
    $value = json_encode($member !== null ? $pack : $envelope, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE);
    $temp = tempnam($dir, '.cas-');
    if ($temp === false || file_put_contents($temp, $value) !== strlen($value)) { throw new RuntimeException('Escritura incompleta'); }
    chmod($temp, 0600);
    if (!rename($temp, $path)) { unlink($temp); throw new RuntimeException('No se pudo confirmar la escritura'); }
    echo json_encode(['ok' => true, 'generation' => $generation + 1]);
} catch (Throwable $error) {
    fwrite(STDERR, "No se confirmó la operación de almacenamiento\n");
    echo json_encode(['ok' => false]); exit(1);
} finally {
    if (is_resource($lock)) { flock($lock, LOCK_UN); fclose($lock); }
}
