"""Persistencia GCS y regulación del tráfico compartida por todos los workers."""
import json
import random
import time
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage


class Halted(RuntimeError):
    pass


def split_gs(uri):
    u = urlsplit(uri)
    if u.scheme != "gs" or not u.netloc or not u.path.strip("/"):
        raise ValueError("Se requiere una ruta gs://bucket/objeto")
    key = u.path.lstrip("/")
    if ".." in key.split("/") or u.query or u.fragment:
        raise ValueError("Ruta GCS no válida")
    return u.netloc, key


class Store:
    def __init__(self, bucket):
        self.bucket = storage.Client().bucket(bucket)

    def read(self, key):
        blob = self.bucket.get_blob(key, timeout=30)
        if blob is None:
            return None, 0
        data = blob.download_as_bytes(if_generation_match=blob.generation, timeout=30)
        return json.loads(data), int(blob.generation)

    def write(self, key, value, generation):
        self.bucket.blob(key).upload_from_string(
            json.dumps(value, ensure_ascii=False), content_type="application/json",
            if_generation_match=generation, timeout=30)

    def bytes(self, key):
        return self.bucket.blob(key).download_as_bytes(timeout=60)

    def put(self, key, value, content_type="application/octet-stream"):
        self.bucket.blob(key).upload_from_string(value, content_type=content_type, timeout=60)

    def exists(self, key):
        return self.bucket.blob(key).exists(timeout=30)

    def uri(self, key):
        return f"gs://{self.bucket.name}/{key}"

    def list(self, prefix):
        return (blob.name for blob in self.bucket.list_blobs(prefix=prefix))


def open_storage(uri=None, bucket=None, backend=None):
    from cloudrun.settings import settings
    config = settings()
    scheme = urlsplit(uri).scheme if uri else None
    backend = backend or config.get("STORAGE_BACKEND", config.get("STORAGE_TYPE", "gcs"))
    if scheme == "gs" or (not scheme and backend != "sftp"):
        if uri:
            name, key = split_gs(uri)
        else:
            name, key = bucket or config.get("BUCKET"), None
            if not name:
                raise ValueError("Falta el bucket de Google Cloud")
        return Store(name), key
    if scheme not in {None, "sftp"}:
        raise ValueError("Usa gs://bucket/objeto o sftp:///objeto")
    if uri and urlsplit(uri).netloc:
        raise ValueError("La ruta lógica SFTP debe ser sftp:///objeto; el servidor se configura en el entorno")
    from cloudrun.sftp_store import SFTPStore, validate_key
    key = validate_key(urlsplit(uri).path.lstrip("/")) if uri else None
    return SFTPStore(config), key


def update(store, key, change):
    """Lectura y modificación condicional; jamás sobrescribe un cambio concurrente."""
    for _ in range(60):
        try:
            old, generation = store.read(key)
            new = change(old)
            if new is None:
                return old
            store.write(key, new, generation)
            return new
        except PreconditionFailed:
            time.sleep(random.uniform(.03, .15))
    raise RuntimeError("No se pudo coordinar el acceso a GCS; se detiene sin consultar al IESS")


def retry_after_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(0., float(value))
    except (TypeError, ValueError):
        try:
            return max(0., parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0.


class RateGate:
    def __init__(self, store, key="control/iess.json", initial=4., minimum=1.,
                 stable_after=100, clock=time.time, sleep=time.sleep):
        if not .5 <= minimum <= initial <= 60 or stable_after < 10:
            raise ValueError("Intervalos: 0.5 <= mínimo <= inicial <= 60; ventana >= 10")
        self.store, self.key, self.clock, self.sleep = store, key, clock, sleep
        self.initial, self.minimum, self.stable_after = initial, minimum, stable_after

    def initial_state(self):
        return dict(interval=self.initial, minimum=self.minimum,
                    stable_after=self.stable_after, healthy=0, failures=0,
                    next_at=0., pause_until=0., halted=False, reason="")

    def acquire(self, check=lambda: None):
        while True:
            check()
            now = self.clock()
            decision = {}

            def reserve(old):
                state = dict(old or self.initial_state())
                if state["halted"]:
                    raise Halted(state.get("reason", "Pausa compartida"))
                wait = max(state["next_at"], state["pause_until"]) - now
                if wait > 0:
                    decision["wait"] = wait
                    return None
                state["next_at"] = now + state["interval"] + random.uniform(0, .3)
                decision["wait"] = 0
                return state

            update(self.store, self.key, reserve)
            if not decision["wait"]:
                return
            self.sleep(min(decision["wait"], 2.))

    def success(self):
        def change(old):
            s = dict(old or self.initial_state())
            if s["halted"]:
                return None
            s["healthy"] += 1
            if s["healthy"] >= s["stable_after"]:
                s["interval"] = max(s["minimum"], s["interval"] * .85)
                s["healthy"] = 0
                s["failures"] = 0
            return s
        update(self.store, self.key, change)

    def failure(self, reason, retry_after=0., permanent=False):
        now = self.clock()

        def change(old):
            s = dict(old or self.initial_state())
            s["healthy"] = 0
            s["failures"] += 1
            s["interval"] = min(60., s["interval"] * 2)
            s["pause_until"] = max(s["pause_until"], now + max(60., retry_after))
            s["reason"] = reason
            if permanent or s["failures"] >= 3:
                s["halted"] = True
            return s
        return update(self.store, self.key, change)

    def halt(self, reason):
        return self.failure(reason, permanent=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
