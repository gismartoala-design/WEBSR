#!/usr/bin/env python3
"""Ejecuta compañías y luego personas con logs y estado de ejecución."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
ESTADO = BASE / "salida" / "estado_ejecucion.json"

def fecha():
    return datetime.now(ZoneInfo("America/Guayaquil")).isoformat(timespec="seconds")

def main():
    os.chdir(BASE)
    (BASE / "salida").mkdir(exist_ok=True)
    (BASE / "logs").mkdir(exist_ok=True)
    with (BASE / "salida" / "ejecucion.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Ya hay una ejecución de compañías/personas activa")
        estado = {"pid": os.getpid(), "inicio": fecha(), "estado": "en_curso"}

        def guardar(**cambios):
            estado.update(cambios, actualizado=fecha())
            tmp = ESTADO.with_suffix(".tmp")
            tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=2) + "\n")
            tmp.replace(ESTADO)

        for fase in ["companias", "personas"]:
            comando = [sys.executable, "-u", "consultar_iess.py", "--fase", fase]
            guardar(fase=fase, estado="en_curso", comando=comando)
            with (BASE / "logs" / f"{fase}.log").open("a", buffering=1) as log:
                for intento in range(1, 7):
                    log.write(f"\n[{fecha()}] Inicio {fase}; intento {intento}\n")
                    codigo = subprocess.call(comando, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT)
                    log.write(f"[{fecha()}] Fin del proceso; código {codigo}\n")
                    if codigo != 75 or intento == 6:
                        break
                    espera = min(60 * 2 ** (intento - 1), 900)
                    guardar(estado="esperando_reconexion", intento=intento,
                            espera_segundos=espera)
                    time.sleep(espera)
                    guardar(estado="en_curso")
            if codigo:
                guardar(estado="detenido_por_error", codigo_salida=codigo, fin=fecha())
                return codigo
        guardar(estado="completado", fase="finalizada", fin=fecha(), codigo_salida=0)
        return 0

if __name__ == "__main__":
    sys.exit(main())
