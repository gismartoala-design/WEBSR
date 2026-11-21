"""Prepara lotes inmutables y una copia del avance local, sin consultar al IESS."""
import argparse
import csv
import fcntl
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
from datetime import datetime, timezone

TERMINAL = {"AL_DIA", "EN_MORA", "NO_REGISTRADO", "RUC_RECHAZADO_IESS"}


def assert_local_stopped(base):
    path = base / "salida" / "ejecucion.lock"
    if path.exists():
        with path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("La corrida local sigue activa. Ejecuta primero python -m cloudrun.pause_local") from exc


def prepare(base, output, dataset, size=1000):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", dataset) or not 1 <= size <= 2000:
        raise ValueError("Dataset no válido o tamaño de lote fuera de 1..2000")
    assert_local_stopped(base)
    if output.exists():
        raise ValueError("La carpeta de salida ya existe. Usa otro dataset; no se sobrescriben lotes")
    output.mkdir(parents=True)
    all_rucs = set()
    for phase in ("companias", "personas"):
        with (base / "rucs" / f"rucs_{phase}.csv").open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            columns, rows = reader.fieldnames, list(reader)
        rucs = {r["ruc"] for r in rows}
        if len(rucs) != len(rows) or all_rucs & rucs:
            raise ValueError("Hay RUC duplicados en el padrón")
        if not all(re.fullmatch(r"[0-9]{13}", ruc) for ruc in rucs):
            raise ValueError("El padrón contiene RUC con formato no válido")
        all_rucs.update(rucs)
        checkpoint = base / "salida" / f"checkpoint_{phase}.csv"
        done = {}
        if checkpoint.exists():
            with checkpoint.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row["ruc"] in rucs and row["estado"] in TERMINAL:
                        if row["ruc"] in done:
                            raise ValueError("Checkpoint duplicado; revisar antes de migrar")
                        if row.get("pdf"):
                            source = base / row["pdf"]
                            if not source.is_file():
                                raise ValueError(f"Falta el PDF de respaldo: {source}")
                            relative = f"pdf/{phase}/{row['ruc']}.pdf"
                            target = output / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
                            row["pdf"] = relative
                        done[row["ruc"]] = row
        baseline = f"baseline/checkpoint_{phase}.csv"
        (output / "baseline").mkdir(exist_ok=True)
        with (output / baseline).open("w", encoding="utf-8", newline="") as fh:
            fields = ["ruc", "razon_social", "estado", "capital", "pdf", "nota", "fecha"]
            writer = csv.DictWriter(fh, fields)
            writer.writeheader()
            writer.writerows(done.values())
        pending = [r for r in rows if r["ruc"] not in done]
        shards = []
        for index, start in enumerate(range(0, len(pending), size)):
            batch = pending[start:start + size]
            buf = io.StringIO(newline="")
            writer = csv.DictWriter(buf, columns)
            writer.writeheader()
            writer.writerows(batch)
            raw = buf.getvalue().encode("utf-8")
            relative = f"inputs/{phase}/{index:05d}.csv"
            p = output / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
            shards.append(dict(path=relative, rows=len(batch), sha256=hashlib.sha256(raw).hexdigest()))
        manifest = dict(version=1, dataset=dataset, phase=phase,
                        created=datetime.now(timezone.utc).isoformat(),
                        source_rows=len(rows), baseline_rows=len(done), baseline=baseline,
                        pending_rows=len(pending), shards=shards)
        (output / f"manifest_{phase}.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"{phase}: {len(done)} ya consultados; {len(pending)} pendientes; {len(shards)} lotes")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--dataset", required=True)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    output = a.output or a.base / "cloudrun" / "staging" / a.dataset
    prepare(a.base, output, a.dataset, a.batch_size)


if __name__ == "__main__":
    main()
