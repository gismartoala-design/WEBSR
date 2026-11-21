"""Descarga los reportes parciales y genera un CSV y Excel consolidados."""
import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from cloudrun.shared import open_storage

FIELDS = ["ruc", "razon_social", "estado", "capital", "pdf", "nota", "fecha"]


def consolidate(manifest, baseline, reports):
    rows = {}
    for group in [baseline, *reports]:
        for row in group:
            if row["ruc"] in rows:
                raise ValueError(f"RUC duplicado entre reportes: {row['ruc']}")
            rows[row["ruc"]] = row
    if len(rows) > manifest["source_rows"]:
        raise ValueError("Los resultados exceden el padrón de este dataset")
    return rows


def write_reports(rows, phase, output):
    output.mkdir(parents=True, exist_ok=True)
    csvpath = output / f"checkpoint_{phase}.csv"
    with csvpath.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, FIELDS)
        writer.writeheader()
        writer.writerows(rows.values())
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(phase)
    ws.append(["RUC", "Razón social", "Estado", "Monto mora USD", "Fecha", "PDF"])
    for row in rows.values():
        if row["estado"] != "EN_MORA":
            continue
        values = [row["ruc"], row["razon_social"], "EN MORA",
                  float(row["capital"]) if row["capital"] else None, row["fecha"], row["pdf"]]
        cells = []
        for value in values:
            cell = WriteOnlyCell(ws, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        ws.append(cells)
    wb.save(output / f"deudores_{phase}.xlsx")


def collect_report(store, key, output, publish=False):
    manifest = json.loads(store.bytes(key))
    prefix, phase = key.rsplit("/", 1)[0], manifest["phase"]
    baseline = list(csv.DictReader(io.StringIO(store.bytes(f"{prefix}/{manifest['baseline']}").decode("utf-8-sig"))))
    for row in baseline:
        if row["pdf"] and not row["pdf"].startswith(("gs://", "sftp://")):
            row["pdf"] = store.uri(f"{prefix}/{row['pdf']}")
    reports = []
    for report in store.list(f"{prefix}/reports/{phase}/"):
        if report.endswith(".csv"):
            reports.append(list(csv.DictReader(io.StringIO(store.bytes(report).decode("utf-8-sig")))))
    rows = consolidate(manifest, baseline, reports)
    write_reports(rows, phase, output)
    summary = dict(fase=phase, resultados=len(rows), total=manifest["source_rows"],
                   pendiente_en_reportes=manifest["source_rows"] - len(rows),
                   estados=dict(Counter(r["estado"] for r in rows.values())))
    (output / f"resumen_{phase}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if publish:
        for filename in [f"checkpoint_{phase}.csv", f"deudores_{phase}.xlsx", f"resumen_{phase}.json"]:
            store.put(f"exports/cloudrun/{manifest['dataset']}/{phase}/{filename}", (output / filename).read_bytes())
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest-uri", required=True)
    p.add_argument("--output", type=Path, default=Path("cloudrun/downloads"))
    p.add_argument("--publish", action="store_true", help="Actualiza también los CSV y Excel privados en el servidor")
    a = p.parse_args()
    store, key = open_storage(a.manifest_uri)
    try:
        summary = collect_report(store, key, a.output, a.publish)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        if hasattr(store, "close"): store.close()


if __name__ == "__main__":
    main()
