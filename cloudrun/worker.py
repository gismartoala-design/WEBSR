"""Un lote por tarea Cloud Run, sin servidor HTTP ni consultas concurrentes internas."""
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import uuid
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.api_core.exceptions import PreconditionFailed

import consultar_iess as iess
from cloudrun.shared import Halted, RateGate, open_storage, update, retry_after_seconds, utc_now

LOG = logging.getLogger("iess.worker")


def publish_exports(store, manifest_key, prefix, phase, force=False):
    interval = int(os.environ.get("EXPORT_INTERVAL", "0"))
    if interval <= 0:
        return
    marker = f"{prefix}/progress/export-{phase}.json"
    state, _ = store.read(marker)
    if not force and state and time.time() - state.get("time", 0) < interval:
        return
    deadline = time.monotonic() + (120 if force else 0)
    while True:
        try:
            with ShardLease(store, f"{prefix}/leases/export-{phase}.json"):
                state, _ = store.read(marker)
                if not force and state and time.time() - state.get("time", 0) < interval:
                    return
                from cloudrun.collect import collect_report
                with tempfile.TemporaryDirectory() as temp:
                    result = collect_report(store, manifest_key, Path(temp), publish=True)
                store.put(marker, json.dumps(dict(time=time.time(), **result)), "application/json")
                LOG.info("CSV y Excel consolidados actualizados: fase=%s resultados=%s", phase, result["resultados"])
                return
        except Halted:
            if time.monotonic() >= deadline:
                return
            time.sleep(2)


class TemporaryPortalError(RuntimeError):
    pass


class ShardLease:
    """Impide que dos ejecuciones procesen el mismo lote simultáneamente."""
    def __init__(self, store, key):
        self.store, self.key = store, key
        self.owner = str(uuid.uuid4())
        self.stop = threading.Event()
        self.lost = threading.Event()

    def touch(self, initial=False):
        def change(old):
            now = time.time()
            if old and old.get("owner") != self.owner:
                if not initial or old.get("expires", 0) > now:
                    raise Halted("Lote ocupado por otra ejecución; no se duplican consultas")
            return dict(owner=self.owner, expires=now + 180)
        update(self.store, self.key, change)

    def __enter__(self):
        self.touch(initial=True)

        def heartbeat():
            while not self.stop.wait(30):
                try:
                    self.touch()
                except Exception:
                    LOG.exception("Se perdió la coordinación del lote")
                    self.lost.set()
                    return
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def check(self):
        if self.lost.is_set() or self.stop.is_set():
            raise Halted("Ejecución interrumpida o sin exclusividad del lote")

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=35)
        try:
            def release(old):
                if old and old.get("owner") == self.owner:
                    return dict(old, expires=0)
                return None
            update(self.store, self.key, release)
        except Exception:
            LOG.exception("El bloqueo del lote expirará automáticamente")


class PoliteSession(requests.Session):
    def __init__(self, gate, check):
        super().__init__()
        self.gate, self.check = gate, check
        self.headers.update({"User-Agent": iess.UA, "Origin": iess.BASE,
                             "Accept-Language": "es-EC,es;q=0.9"})

    def request(self, method, url, **kwargs):
        # Cada GET, POST y redirección debe respetar el mismo control global.
        from urllib.parse import urlsplit, urljoin
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "www.iess.gob.ec":
            self.gate.halt("El portal redirigió fuera del dominio oficial permitido")
            raise Halted("Dominio inesperado")
        self.gate.acquire(self.check)
        kwargs["allow_redirects"] = False
        try:
            r = super().request(method, url, **kwargs)
        except requests.RequestException as exc:
            self.gate.failure("Fallo de conexión con IESS")
            raise TemporaryPortalError("Fallo de conexión con IESS") from exc
        self.check()
        if r.status_code in (401, 403):
            self.gate.halt(f"HTTP {r.status_code}: acceso restringido por el portal")
            raise Halted(f"HTTP {r.status_code}; se requiere revisión manual")
        if r.status_code == 429 or r.status_code >= 500:
            self.gate.failure(f"HTTP {r.status_code}", retry_after_seconds(r.headers.get("Retry-After")))
            raise TemporaryPortalError(f"HTTP {r.status_code}; pausa compartida")
        if 300 <= r.status_code < 400:
            # Un redirect inesperado puede cambiar el contexto de sesión. No se
            # reenvían RUC ni estados de formulario automáticamente.
            self.gate.halt("Redirección del portal: requiere revisión manual")
            raise Halted("Redirección inesperada")
        r.raise_for_status()
        if "pdf" not in r.headers.get("Content-Type", "").lower() and not r.content.startswith(b"%PDF"):
            lower = r.text.lower()
            if any(s in lower for s in ("captcha", "access denied", "request rejected", "too many requests")):
                self.gate.halt("El portal solicita validación humana o ha restringido las consultas")
                raise Halted("Validación humana o bloqueo; no se continúa automáticamente")
        return r


def validate_response(gate, response, ruc):
    result = iess.interpretar(response, ruc)
    if result["estado"] not in iess.ESTADOS_VALIDOS:
        gate.halt(f"Respuesta del portal no reconocida: {result['estado']}")
        raise Halted("Respuesta no verificable; se requiere revisión manual")
    gate.success()
    return result


def process_rows(rows, store, gate, prefix, phase, check, limit=0, records=None, snapshot=lambda: None):
    """Persiste cada resultado antes de continuar. Los reintentos lo reutilizan."""
    new = skipped = 0
    records = [] if records is None else records
    with PoliteSession(gate, check) as session:
        action = None
        for row in rows:
            check()
            ruc = row["ruc"]
            key = f"{prefix}/results/{phase}/{ruc}.json"
            existing, _ = store.read(key)
            if existing is not None:
                if existing.get("ruc") != ruc or existing.get("estado") not in iess.ESTADOS_VALIDOS:
                    raise Halted("Resultado persistido no válido; revisar antes de reanudar")
                records.append(existing)
                skipped += 1
                if len(records) % 50 == 0:
                    snapshot()
                continue
            if limit and new >= limit:
                return dict(nuevos=new, existentes=skipped, estado="piloto_parcial")
            for attempt in range(3):
                try:
                    if action is None:
                        action = iess.abrir(session)
                    response = iess.pedir(session, ruc, action)
                    result = validate_response(gate, response, ruc)
                    break
                except TemporaryPortalError:
                    action = None
                    if attempt == 2:
                        raise
            result.update(razon_social=row.get("razon_social", ""),
                          fecha=datetime.now(ZoneInfo("America/Guayaquil")).isoformat(timespec="seconds"))
            if result["pdf"]:
                path = Path(result["pdf"])
                pdfkey = f"{prefix}/pdf/{phase}/{ruc}.pdf"
                store.put(pdfkey, path.read_bytes(), "application/pdf")
                result["pdf"] = store.uri(pdfkey)
            check()
            try:
                store.write(key, result, 0)
            except PreconditionFailed:
                raise Halted("Otro proceso guardó el mismo RUC; revisar ejecuciones duplicadas")
            records.append(result)
            # El archivo remoto se confirma antes de liberar el espacio local.
            local = iess.SALIDA / "pdf" / f"{ruc}.pdf"
            local.unlink(missing_ok=True)
            new += 1
            if len(records) % 50 == 0:
                snapshot()
            LOG.info("Resultado persistido: fase=%s ruc=%s estado=%s", phase, ruc, result["estado"])
    return dict(nuevos=new, existentes=skipped, estado="completado")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store, manifest_key = open_storage(os.environ["MANIFEST_URI"])
    manifest = json.loads(store.bytes(manifest_key))
    prefix = manifest_key.rsplit("/", 1)[0]
    phase = manifest["phase"]
    index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    if phase not in {"companias", "personas"} or not 0 <= index < len(manifest["shards"]):
        raise ValueError("Fase o índice de tarea no válido para el manifiesto")
    shard = manifest["shards"][index]
    relative = shard["path"]
    if relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("Ruta de lote no válida")
    raw = store.bytes(f"{prefix}/{relative}")
    if hashlib.sha256(raw).hexdigest() != shard["sha256"]:
        raise ValueError("El lote cambió después de preparar el manifiesto")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if len(rows) != shard["rows"] or len({r["ruc"] for r in rows}) != len(rows):
        raise ValueError("Lote incompleto o con RUC duplicados")
    if not all(len(r["ruc"]) == 13 and r["ruc"].isascii() and r["ruc"].isdigit() for r in rows):
        raise ValueError("RUC no válido en el lote")
    gate = RateGate(store, os.environ.get("CONTROL_OBJECT", "control/iess.json"),
                    initial=float(os.environ.get("INITIAL_INTERVAL", "4")),
                    minimum=float(os.environ.get("MIN_INTERVAL", "1")),
                    stable_after=int(os.environ.get("STABLE_AFTER", "100")))
    statuskey = f"{prefix}/progress/{phase}/{index:05d}.json"
    with ShardLease(store, f"{prefix}/leases/{phase}/{index:05d}.json") as lease:
        previous, _ = store.read(statuskey)
        if previous and previous.get("estado") == "completado" and previous.get("sha256") == shard["sha256"] and previous.get("guardados") == len(rows):
            LOG.info("Lote ya completado: fase=%s tarea=%s", phase, index)
            return
        def interrupted(*_):
            lease.stop.set()
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        status = dict(task=index, fase=phase, inicio=utc_now(), estado="en_curso", sha256=shard["sha256"])
        store.put(statuskey, json.dumps(status), "application/json")
        records = []

        def snapshot():
            buf = io.StringIO(newline="")
            cols = ["ruc", "razon_social", "estado", "capital", "pdf", "nota", "fecha"]
            writer = csv.DictWriter(buf, cols)
            writer.writeheader()
            writer.writerows(records)
            store.put(f"{prefix}/reports/{phase}/{index:05d}.csv", buf.getvalue(), "text/csv")
            status.update(guardados=len(records), actualizado=utc_now())
            store.put(statuskey, json.dumps(status), "application/json")
            try:
                publish_exports(store, manifest_key, prefix, phase, force=status.get("estado") == "completado")
            except Exception:
                # El resultado por RUC y el CSV del lote ya están confirmados.
                # Un fallo del Excel se puede recuperar con collect.
                LOG.exception("No se pudo actualizar el consolidado; los resultados siguen guardados")
        try:
            result = process_rows(rows, store, gate, prefix, phase, lease.check,
                                  int(os.environ.get("MAX_RECORDS", "0")), records, snapshot)
            status.update(result, fin=utc_now())
        except Exception as exc:
            status.update(estado="detenido", error=str(exc), fin=utc_now())
            store.put(statuskey, json.dumps(status), "application/json")
            raise
        finally:
            snapshot()
    LOG.info("Tarea finalizada: %s", status)


if __name__ == "__main__":
    main()
