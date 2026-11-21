#!/usr/bin/env python3
"""Consulta mora patronal IESS. Fase 1 companias, fase 2 personas naturales."""
import argparse, csv, random, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import unicodedata

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
ESTADOS_VALIDOS = {"AL_DIA", "EN_MORA", "NO_REGISTRADO", "RUC_RECHAZADO_IESS"}

class ErrorConexion(RuntimeError):
    pass

def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto).strip().upper()

def extraer_monto(texto):
    m = re.search(r"(?:USD|\$)\s*([0-9][0-9.,]*[.,][0-9]{2})(?![0-9])", texto)
    if not m:
        return ""
    valor = m.group(1)
    return valor[:-3].replace(".", "").replace(",", "") + "." + valor[-2:]

def sesion():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Origin": BASE,
                      "Accept-Language": "es-EC,es;q=0.9"})
    return s

def abrir(s):
    r = s.get(FORM, timeout=30); r.raise_for_status()
    f = BeautifulSoup(r.text, "lxml").find("form", id="frmCertificadoCumplimiento")
    if not f: raise RuntimeError("formulario no encontrado")
    return {"url": urljoin(r.url, f.get("action") or FORM),
            "datos": {e["name"]: e.get("value", "")
                      for e in f.select('input[type="hidden"][name]')}}

def pedir(s, ruc, action):
    r = s.post(action["url"], timeout=60, headers={"Referer": FORM}, data={
        **action["datos"],
        "frmCertificadoCumplimiento": "frmCertificadoCumplimiento",
        "frmCertificadoCumplimiento:j_id9": ruc,
        "frmCertificadoCumplimiento:j_id11": "CONSULTAR"})
    r.raise_for_status()
    if "pdf" not in r.headers.get("Content-Type", "").lower() and r.content[:4] != b"%PDF":
        f = BeautifulSoup(r.text, "lxml").find("form", id="frmCertificadoCumplimiento")
        if f:
            action["url"] = urljoin(r.url, f.get("action") or FORM)
            action["datos"] = {e["name"]: e.get("value", "")
                               for e in f.select('input[type="hidden"][name]')}
    return r

def interpretar(r, ruc, guardar_pdf="mora"):
    out = {"ruc": ruc, "estado": "", "capital": "", "pdf": "", "nota": ""}
    if "pdf" in r.headers.get("Content-Type", "").lower() or r.content[:4] == b"%PDF":
        from io import BytesIO
        with pdfplumber.open(BytesIO(r.content)) as pdf:
            t = "\n".join(p.extract_text() or "" for p in pdf.pages)
        texto = normalizar(t)
        if ruc not in re.sub(r"\s+", "", texto):
            out["estado"] = "DESCONOCIDO"
            out["nota"] = "El certificado no contiene el RUC consultado"
        elif re.search(r"NO REGISTRA(?: OBLIGACIONES PATRONALES)? EN MORA|NO REGISTRA OBLIGACIONES PATRONALES EN MORA", texto):
            out["estado"] = "AL_DIA"
        elif re.search(r"(?:SI )?REGISTRA OBLIGACIONES PATRONALES EN MORA", texto):
            out["estado"] = "EN_MORA"
            out["capital"] = extraer_monto(texto)
            if not out["capital"]: out["nota"] = "monto no detectado"
        else:
            out["estado"] = "DESCONOCIDO"
            out["nota"] = texto[:150]
        if guardar_pdf == "todos" or out["estado"] in {"EN_MORA", "DESCONOCIDO"}:
            ruta = SALIDA / "pdf" / f"{ruc}.pdf"
            ruta.write_bytes(r.content)
            out["pdf"] = str(ruta)
        return out
    soup = BeautifulSoup(r.text, "lxml")
    t = normalizar(soup.get_text(" ", strip=True))
    if "EL RUC INGRESADO NO ES CORRECTO" in t:
        out["estado"] = "RUC_RECHAZADO_IESS"
        out["nota"] = "El RUC consta en el padrón SRI pero el IESS responde: El RUC ingresado no es correcto"
    elif re.search(r"(?:NO SE ENCUENTRA|NO EXISTE|NO REGISTRA).{0,80}(?:REGISTRAD|EMPLEADOR|AFILIAD)", t):
        out["estado"] = "NO_REGISTRADO"
    elif "CEDULA" in t and "INGRESE" in t:
        out["estado"] = "FORM_VACIO"
    else:
        out["estado"] = "DESCONOCIDO"; out["nota"] = t[:150]
    if out["estado"] not in ESTADOS_VALIDOS:
        (SALIDA / f"respuesta_{ruc}.html").write_text(r.text, encoding="utf-8")
    return out

def exportar(nombre, chk):
    wb = Workbook(); ws = wb.active; ws.title = nombre
    ws.append(["RUC", "Razon social", "Estado", "Monto mora USD", "Fecha", "PDF"])
    c = 0
    with open(chk, newline="", encoding="utf-8") as fh:
        for x in csv.DictReader(fh):
            if x["estado"] != "EN_MORA": continue
            c += 1
            # Los nombres del padrón se escriben como texto, nunca como fórmulas.
            from openpyxl.cell import WriteOnlyCell
            razon = WriteOnlyCell(ws, value=x["razon_social"]); razon.data_type = "s"
            ws.append([x["ruc"], razon, "EN MORA",
                       float(x["capital"]) if x["capital"] else None,
                       x["fecha"], x["pdf"]])
    for col, wd in zip("ABCDEF", (16, 45, 12, 14, 20, 30)):
        ws.column_dimensions[col].width = wd
    ws.freeze_panes = "A2"
    destino = SALIDA / f"deudores_{nombre}.xlsx"
    temporal = destino.with_suffix(".tmp.xlsx"); wb.save(temporal); temporal.replace(destino)
    print(f"[{nombre}] {c} deudores -> {destino}\n", flush=True)

def fase(nombre, ruta, delay, limite, guardar_pdf="mora"):
    chk = SALIDA / f"checkpoint_{nombre}.csv"
    hechos = set()
    if chk.exists():
        with open(chk, newline="", encoding="utf-8") as f:
            hechos = {x["ruc"] for x in csv.DictReader(f) if x["estado"] in ESTADOS_VALIDOS}
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
                        res = interpretar(pedir(s, ruc, action), ruc, guardar_pdf); break
                    except Exception as e:
                        print(f"  ! {ruc}: {e}", file=sys.stderr)
                        time.sleep(3 * (i + 1))
                        try: s = sesion(); action = abrir(s)
                        except Exception: pass
                if res is None:
                    raise ErrorConexion(f"Fallo de red persistente en {ruc}; se conserva el avance para reanudar")
                if res["estado"] not in ESTADOS_VALIDOS:
                    raise RuntimeError(f"Respuesta no verificada para {ruc}: {res['estado']} {res['nota']}; se detiene sin marcarlo como consultado")
                res["razon_social"] = fila.get("razon_social", "")
                res["fecha"] = datetime.now(ZoneInfo("America/Guayaquil")).isoformat(timespec="seconds")
                w.writerow(res); fh.flush()
                print(f"[{nombre} {n}] {ruc} -> {res['estado']}")
                if n % 100 == 0: exportar(nombre, chk)
                time.sleep(delay + random.uniform(0, .6))

    exportar(nombre, chk)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fase", choices=["companias", "personas", "ambas"], default="ambas")
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--limite", type=int)
    p.add_argument("--guardar-pdf", choices=["mora", "todos"], default="mora",
                   help="Por defecto conserva PDF de mora y respuestas desconocidas")
    a = p.parse_args()
    for f in (["companias", "personas"] if a.fase == "ambas" else [a.fase]):
        if not Path(FASES[f]).exists(): p.error(f"falta {FASES[f]}")
        try:
            fase(f, FASES[f], a.delay, a.limite, a.guardar_pdf)
        except (requests.RequestException, ErrorConexion) as exc:
            chk = SALIDA / f"checkpoint_{f}.csv"
            if chk.exists(): exportar(f, chk)
            print(f"Error de conexión: {exc}", file=sys.stderr, flush=True)
            sys.exit(75)
        except (Exception, KeyboardInterrupt):
            chk = SALIDA / f"checkpoint_{f}.csv"
            if chk.exists(): exportar(f, chk)
            raise

if __name__ == "__main__":
    main()
