"""Detiene ordenadamente la corrida local antes de tomar la copia para la nube."""
import json
import os
from pathlib import Path
import signal
import time


def process_done(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state == "Z"
    except FileNotFoundError:
        return True


def pause(base):
    base = base.resolve()
    statusfile = base / "salida" / "estado_ejecucion.json"
    status = json.loads(statusfile.read_text())
    pid = int(status["pid"])
    if process_done(pid):
        print("El coordinador local ya no está activo")
        return
    args = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    if "ejecutar_consultas.py" not in args or Path(f"/proc/{pid}/cwd").resolve() != base:
        raise RuntimeError("El PID no corresponde al coordinador esperado; no se envían señales")
    # Primero congela el coordinador para impedir que reinicie al hijo o pase a
    # personas mientras el hijo termina y guarda el Excel.
    os.kill(pid, signal.SIGSTOP)
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        for child in children:
            childpid = int(child)
            cmd = Path(f"/proc/{childpid}/cmdline").read_bytes()
            if b"consultar_iess.py" not in cmd:
                raise RuntimeError("Proceso hijo inesperado; no se detiene")
        for child in children:
            os.kill(int(child), signal.SIGINT)
        deadline = time.monotonic() + 90
        while not all(process_done(int(c)) for c in children):
            if time.monotonic() >= deadline:
                raise RuntimeError("El hijo no terminó; revisar el log antes de preparar los lotes")
            time.sleep(.5)
        os.kill(pid, signal.SIGTERM)
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while not process_done(pid) and time.monotonic() < deadline:
        time.sleep(.2)
    if not process_done(pid):
        raise RuntimeError("El coordinador aún no terminó")
    status["estado"] = "detenido_para_migracion"
    tmp = statusfile.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(statusfile)
    print("Corrida local detenida. Ya puedes preparar y subir los lotes")


def main():
    pause(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
