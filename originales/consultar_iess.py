#!/usr/bin/env python3
"""Consulta mora patronal IESS. Fase 1 companias, fase 2 personas naturales."""
import argparse, csv, random, re, sys, time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pdfplumber
from openpyxl import Workbook

BASE = "https://www.iess.gob.ec"
FORM = BASE + "/empleador-web/pages/morapatronal/certificadoCumplimientoPublico.jsf"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

FASES = {"companias": "rucs/rucs_companias.csv",
         "personas": "rucs/rucs_personas.csv"}
SALIDA = Path("salida"); SALIDA.mkdir(exist_ok=True)
(SALIDA / "pdf").mkdir(exist_ok=True)

def sesion():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Origin": BASE,
                      "Accept-Language": "es-EC,es;q=0.9"})
    return s

def abrir(s):
    r = s.get(FORM, timeout=30); r.raise_for_status()
    f = BeautifulSoup(r.text, "lxml").find("form", id="frmCertificadoCumplimiento")
    if not f: raise RuntimeError("formulario no encontrado")
    return f.get("action") or FORM

def pedir(s, ruc, action):
    return s.post(action, timeout=60, headers={"Referer": FORM}, data={
        "frmCertificadoCumplimiento": "frmCertificadoCumplimiento",
        "frmCertificadoCumplimiento:j_id9": ruc,
        "frmCertificadoCumplimiento:j_id11": "CONSULTAR"})

def interpretar(r, ruc):
    out = {"ruc": ruc, "estado": "", "capital": "", "pdf": "", "nota": ""}
    if "pdf" in r.headers.get("Content-Type", "").lower() or r.content[:4] == b"%PDF":
        ruta = SALIDA / "pdf" / f"{ruc}.pdf"; ruta.write_bytes(r.content)
        out["pdf"] = str(ruta)
        with pdfplumber.open(ruta) as pdf:
            t = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if "NO REGISTRA" in t.upper():
            out["estado"] = "AL_DIA"
        else:
            m = re.search(r"(?:USD|\$)\s*([\d.]+,\d{2})", t)
            out["estado"] = "EN_MORA"
            out["capital"] = m.group(1).replace(".", "").replace(",", ".") if m else ""
            if not m: out["nota"] = "monto no detectado"
        return out
    t = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True).upper()
    if any(k in t for k in ("NO SE ENCUENTRA", "NO EXISTE", "NO REGISTRA")):
        out["estado"] = "NO_REGISTRADO"
    elif "CEDULA" in t and "INGRESE" in t:
        out["estado"] = "FORM_VACIO"
    else:
        out["estado"] = "DESCONOCIDO"; out["nota"] = t[:150]
    return out

def fase(nombre, ruta, delay, limite):
    chk = SALIDA / f"checkpoint_{nombre}.csv"
    hechos = set()
    if chk.exists():
        with open(chk, newline="", encoding="utf-8") as f:
            hechos = {x["ruc"] for x in csv.DictReader(f)}
        print(f"[{nombre}] reanudando, {len(hechos)} ya hechos")

    cols = ["ruc", "razon_social", "estado", "capital", "pdf", "nota", "fecha"]
    nuevo = not chk.exists()
    with open(chk, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols)
        if nuevo: w.writeheader()
        s = sesion(); action = abrir(s); n = 0
        with open(ruta, newline="", encoding="utf-8-sig") as ent:
            for fila in csv.DictReader(ent):
                ruc = fila["ruc"].strip()
                if ruc in hechos: continue
                if limite and n >= limite: break
                n += 1
                res = None
                for i in range(3):
                    try:
                        res = interpretar(pedir(s, ruc, action), ruc); break
                    except Exception as e:
                        print(f"  ! {ruc}: {e}", file=sys.stderr)
                        time.sleep(3 * (i + 1))
                        try: s = sesion(); action = abrir(s)
                        except Exception: pass
                res = res or {"ruc": ruc, "estado": "FALLO_RED", "capital": "",
                              "pdf": "", "nota": ""}
                res["razon_social"] = fila.get("razon_social", "")
                res["fecha"] = datetime.now().isoformat(timespec="seconds")
                w.writerow(res); fh.flush()
                print(f"[{nombre} {n}] {ruc} -> {res['estado']}")
                time.sleep(delay + random.uniform(0, .6))

    wb = Workbook(); ws = wb.active; ws.title = nombre
    ws.append(["RUC", "Razon social", "Estado", "Capital USD", "Fecha", "PDF"])
    c = 0
    with open(chk, newline="", encoding="utf-8") as fh:
        for x in csv.DictReader(fh):
            if x["estado"] != "EN_MORA": continue
            c += 1
            ws.append([x["ruc"], x["razon_social"], "EN MORA",
                       float(x["capital"]) if x["capital"] else None,
                       x["fecha"], x["pdf"]])
    for col, wd in zip("ABCDEF", (16, 45, 12, 14, 20, 30)):
        ws.column_dimensions[col].width = wd
    ws.freeze_panes = "A2"
    destino = SALIDA / f"deudores_{nombre}.xlsx"; wb.save(destino)
    print(f"[{nombre}] {c} deudores -> {destino}\n")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fase", choices=["companias", "personas", "ambas"], default="ambas")
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--limite", type=int)
    a = p.parse_args()
    for f in (["companias", "personas"] if a.fase == "ambas" else [a.fase]):
        if Path(FASES[f]).exists(): fase(f, FASES[f], a.delay, a.limite)
        else: print(f"falta {FASES[f]}")

if __name__ == "__main__":
    main()