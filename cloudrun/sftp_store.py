"""Almacenamiento privado Hostinger: SFTP y CAS con flock en PHP por SSH."""
import base64
import hashlib
import io
import json
import posixpath
import re
import shlex
import stat
import threading
import uuid

from google.api_core.exceptions import PreconditionFailed
import paramiko

from cloudrun.settings import settings


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class HostPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected, first_connection=False):
        self.expected, self.first_connection = expected, first_connection

    def missing_host_key(self, client, hostname, key):
        if self.expected:
            if fingerprint(key) != self.expected:
                raise paramiko.SSHException("La huella SSH no coincide con la configurada")
        elif not self.first_connection:
            raise paramiko.SSHException("Falta SFTP_HOST_KEY_SHA256; inicializa Hostinger desde el equipo local")


def connect(config, first_connection=False):
    client = paramiko.SSHClient()
    expected = config.get("SFTP_HOST_KEY_SHA256", "")
    client.set_missing_host_key_policy(HostPolicy(expected, first_connection))
    try:
        client.connect(config["FTP_HOST"], port=int(config.get("FTP_PORT", 65002)),
                       username=config["FTP_USER"], password=config["FTP_PASSWORD"],
                       look_for_keys=False, allow_agent=False, timeout=15,
                       banner_timeout=20, auth_timeout=20)
        actual = fingerprint(client.get_transport().get_remote_server_key())
        if expected and actual != expected:
            raise paramiko.SSHException("La huella SSH cambió")
        client.get_transport().set_keepalive(30)
        return client, actual
    except Exception:
        client.close()
        raise


def validate_key(key):
    if not key or key.startswith("/") or "\\" in key or any(p in {"", ".", ".."} for p in key.split("/")):
        raise ValueError("Ruta de objeto no válida")
    return key


def object_location(key):
    """Agrupa resultados por RUC para evitar cientos de miles de archivos JSON."""
    match = re.fullmatch(r"(runs/[^/]+/results/(?:companias|personas))/([0-9]{13}\.json)", key)
    if match:
        bucket = hashlib.sha256(key.encode()).hexdigest()[:3]
        return f"{match[1]}/_packs/{bucket}.json", match[2]
    return key, None


def mkdirs(sftp, directory):
    parts = directory.split("/")
    current = "/" if directory.startswith("/") else ""
    for part in parts:
        if not part:
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current, mode=0o700)
            except OSError:
                sftp.stat(current)


class SFTPStore:
    def __init__(self, config=None):
        config = config or settings()
        self.root = config.get("SFTP_ROOT", "").rstrip("/")
        if not self.root.startswith("/home/") or "public_html" in self.root.split("/") or ".." in self.root.split("/"):
            raise ValueError("SFTP_ROOT debe ser una carpeta privada bajo /home, fuera de public_html")
        self.client, _ = connect(config)
        self.sftp = self.client.open_sftp()
        self.sftp.get_channel().settimeout(45)
        self.lock = threading.RLock()
        self.directories = set()
        self.php = config.get("SFTP_PHP_BIN", "php")
        self.host, self.port = config["FTP_HOST"], int(config.get("FTP_PORT", 65002))

    def path(self, key):
        return posixpath.join(self.root, "objects", validate_key(key))

    def uri(self, key):
        return f"sftp://{self.host}:{self.port}{self.path(key)}"

    def bytes(self, key):
        with self.lock:
            with self.sftp.open(self.path(key), "rb") as stream:
                return stream.read()

    def read(self, key):
        physical, member = object_location(key)
        try:
            data = json.loads(self.bytes(physical))
        except FileNotFoundError:
            return None, 0
        if member:
            data = data.get(member)
            if data is None: return None, 0
        if isinstance(data, dict) and data.get("_websr_store") == 1:
            return data["value"], data["generation"]
        return data, 0

    def write(self, key, value, generation):
        validate_key(key)
        request = json.dumps(dict(key=key, value=value, generation=generation), ensure_ascii=False)
        command = " ".join(shlex.quote(part) for part in
                           [self.php, posixpath.join(self.root, ".bin/cas.php"), self.root])
        with self.lock:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=45)
            stdin.write(request)
            stdin.flush()
            stdin.channel.shutdown_write()
            raw = stdout.read()
            error = stderr.read()
            code = stdout.channel.recv_exit_status()
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Hostinger CAS devolvió una respuesta inválida (código {code})") from exc
        if response.get("conflict"):
            raise PreconditionFailed("El objeto fue actualizado por otro worker")
        if code or not response.get("ok"):
            raise RuntimeError(f"Hostinger CAS rechazó la escritura (código {code})")

    def put(self, key, value, content_type=None):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        destination = self.path(key)
        temporary = destination + ".tmp-" + uuid.uuid4().hex
        with self.lock:
            directory = posixpath.dirname(destination)
            if directory not in self.directories:
                mkdirs(self.sftp, directory)
                self.directories.add(directory)
            committed = False
            try:
                self.sftp.putfo(io.BytesIO(raw), temporary, file_size=len(raw))
                self.sftp.chmod(temporary, 0o600)
                self.sftp.posix_rename(temporary, destination)
                committed = True
            finally:
                if not committed:
                    try:
                        self.sftp.remove(temporary)
                    except FileNotFoundError:
                        pass

    def exists(self, key):
        if object_location(key)[1]:
            return self.read(key)[0] is not None
        with self.lock:
            try:
                self.sftp.stat(self.path(key))
                return True
            except FileNotFoundError:
                return False

    def list(self, prefix):
        prefix = prefix.rstrip("/")
        validate_key(prefix)
        with self.lock:
            def walk(relative):
                try:
                    entries = self.sftp.listdir_attr(self.path(relative))
                except FileNotFoundError:
                    return
                for entry in entries:
                    key = relative + "/" + entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        yield from walk(key)
                    elif stat.S_ISREG(entry.st_mode) and ".tmp-" not in entry.filename:
                        yield key
            names = list(walk(prefix))
        yield from names

    def close(self):
        self.sftp.close()
        self.client.close()
