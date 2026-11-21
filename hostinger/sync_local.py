"""Actualiza CSV y Excel privados en Hostinger a partir del checkpoint local."""
import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cloudrun.collect import write_reports
from cloudrun.sftp_store import SFTPStore

BASE = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("hostinger.sync")


def local_active():
    path = BASE / "salida/ejecucion.lock"
    if not path.exists():
        return False
    with path.open("r") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def sync_once(store, state):
    summary = {}
    for phase in ["companias", "personas"]:
        checkpoint = BASE / "salida" / f"checkpoint_{phase}.csv"
        rows = {}
        if checkpoint.exists():
            raw = checkpoint.read_text(encoding="utf-8-sig")
            for row in csv.DictReader(io.StringIO(raw)):
                if None in row or any(value is None for value in row.values()):
                    continue
                if len(row.get("ruc", "")) == 13:
                    rows[row["ruc"]] = row
        for row in rows.values():
            if not row.get("pdf"):
                continue
            path = BASE / row["pdf"]
            if not path.is_file():
                raise RuntimeError("Falta un certificado local; no se publica un respaldo incompleto")
            data = path.read_bytes()
            key = f"archives/{phase}/{row['ruc']}.pdf"
            digest = hashlib.sha256(data).hexdigest()
            if state.get(key) != digest:
                store.put(key, data, "application/pdf")
                state[key] = digest
            row["pdf"] = store.uri(key)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            write_reports(rows, phase, output)
            for name in [f"checkpoint_{phase}.csv", f"deudores_{phase}.xlsx"]:
                store.put(f"exports/local/{phase}/{name}", (output / name).read_bytes())
        summary[phase] = dict(consultados=len(rows), estados=dict(Counter(row["estado"] for row in rows.values())))
    summary["actualizado"] = datetime.now(ZoneInfo("America/Guayaquil")).isoformat(timespec="seconds")
    store.put("exports/local/resumen.json", json.dumps(summary, ensure_ascii=False, indent=2), "application/json")
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=int, default=60)
    a = p.parse_args()
    if a.interval < 30: p.error("El intervalo de exportación debe ser al menos 30 segundos")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cache = BASE / ".local"; cache.mkdir(exist_ok=True)
    with (cache / "hostinger-sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        statepath = cache / "hostinger-sync-state.json"
        state = json.loads(statepath.read_text()) if statepath.exists() else {}
        store = None
        failures = 0
        try:
            while True:
                try:
                    if store is None:
                        store = SFTPStore()
                        identity = store.uri("exports/local/resumen.json")
                        if state.get("_destination") != identity:
                            state = {"_destination": identity}
                    active = local_active()
                    summary = sync_once(store, state)
                    tmp = statepath.with_suffix(".tmp"); tmp.write_text(json.dumps(state)); tmp.replace(statepath)
                    LOG.info("Sincronizado: compañías=%s personas=%s", summary["companias"]["consultados"], summary["personas"]["consultados"])
                    failures = 0
                    if not a.watch or not active:
                        break
                except Exception:
                    if not a.watch or failures >= 5:
                        raise
                    LOG.exception("No se confirmó la sincronización; se reintentará")
                    failures += 1
                    if store is not None:
                        try: store.close()
                        except Exception: pass
                    store = None
                time.sleep(a.interval)
        finally:
            if store is not None: store.close()


if __name__ == "__main__":
    main()
