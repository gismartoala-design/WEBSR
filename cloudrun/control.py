"""Consulta, pausa y reanuda el regulador global; requiere credenciales ADC."""
import argparse
import json
import time
from cloudrun.shared import RateGate, open_storage, update


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["status", "pause", "resume", "set-rate"])
    p.add_argument("--bucket")
    p.add_argument("--storage", choices=["gcs", "sftp"])
    p.add_argument("--object", default="control/iess.json")
    p.add_argument("--minimum", type=float, default=1.)
    a = p.parse_args()
    if not .5 <= a.minimum <= 60:
        p.error("El intervalo mínimo debe estar entre 0.5 y 60 segundos")
    store, _ = open_storage(bucket=a.bucket, backend=a.storage)
    gate = RateGate(store, a.object, initial=max(4., a.minimum), minimum=a.minimum)
    if a.action == "status":
        value, _ = store.read(a.object)
    elif a.action == "pause":
        value = gate.halt("Pausa manual del operador")
    else:
        def change(old):
            s = dict(old or gate.initial_state())
            if a.action == "resume":
                if s["pause_until"] > time.time():
                    raise ValueError("La espera compartida aún no termina; respeta Retry-After antes de reanudar")
                s.update(halted=False, reason="", failures=0, healthy=0,
                         interval=max(4., s["minimum"]), next_at=time.time())
            else:
                s.update(minimum=a.minimum, interval=max(s["interval"], a.minimum), healthy=0)
            return s
        value = update(store, a.object, change)
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
