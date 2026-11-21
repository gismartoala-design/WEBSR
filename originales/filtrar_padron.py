#!/usr/bin/env python3
"""Filtra el padron del SRI: deja solo RUCs validos de Guayas, separados por tipo."""
import argparse, csv, re, sys
from pathlib import Path

import pandas as pd

ALIAS = {
    "ruc": ["numero_ruc", "num_ruc", "ruc", "numero de ruc"],
    "razon": ["razon_social", "razon social", "nombre", "contribuyente"],
    "prov": ["provincia", "cod_provincia", "prov"],
    "canton": ["canton", "cod_canton"],
    "parroquia": ["parroquia"],
    "estado": ["estado_contribuyente", "estado", "situacion"],
}

def detectar(df):
    mapa = {}
    norm = {re.sub(r"[^a-z]", "_", c.lower()).strip("_"): c for c in df.columns}
    for campo, alias in ALIAS.items():
        for a in alias:
            k = re.sub(r"[^a-z]", "_", a)
            if k in norm:
                mapa[campo] = norm[k]; break
    faltan = {"ruc", "razon"} - mapa.keys()
    if faltan:
        print("Columnas detectadas:", list(df.columns))
        sys.exit(f"No encontre columnas: {faltan}. Ajusta el diccionario ALIAS.")
    return mapa

def validar(ruc):
    """Modulo 11 segun tipo de RUC. Devuelve el tipo o None."""
    if not re.fullmatch(r"\d{13}", ruc) or ruc[10:] != "001":
        return None
    if not 1 <= int(ruc[:2]) <= 24:
        return None
    t = ruc[2]
    if t == "9":
        coef = [2, 1, 2, 1, 2, 1, 2, 1, 2]
        s = 0
        for i in range(9):
            p = int(ruc[i]) * coef[i]
            s += p - 9 if p > 9 else p
        return "NATURAL" if (10 - s % 10) % 10 == int(ruc[9]) else None
    if t == "6":
        coef = [4, 3, 2, 7, 6, 5, 4, 3, 2]
        s = sum(int(ruc[i]) * coef[i] for i in range(9))
        r = 11 - s % 11
        r = 0 if r == 11 else 1 if r == 10 else r
        return "JURIDICA" if r == int(ruc[9]) else None
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("entrada", nargs="+")
    p.add_argument("--canton", default="GUAYAQUIL")
    p.add_argument("--todo-guayas", action="store_true")
    p.add_argument("--incluir-inactivos", action="store_true")
    p.add_argument("--salida", default="rucs")
    a = p.parse_args()

    df = pd.concat([pd.read_csv(f, dtype=str, sep=None, engine="python",
                                encoding="utf-8-sig", on_bad_lines="skip")
                    if not f.lower().endswith((".xlsx", ".xls"))
                    else pd.read_excel(f, dtype=str)
                    for f in a.entrada], ignore_index=True)
    m = detectar(df)
    total = len(df)

    if not a.todo_guayas and "canton" in m:
        df = df[df[m["canton"]].str.upper().str.strip() == a.canton.upper()]
    elif "prov" in m:
        df = df[df[m["prov"]].astype(str).str.zfill(2) == "09"]

    if not a.incluir_inactivos and "estado" in m:
        df = df[df[m["estado"]].str.upper().str.strip().isin(["ACTIVO", "ACTIVA"])]

    df["ruc"] = df[m["ruc"]].astype(str).str.replace(r"\D", "", regex=True).str.zfill(13)
    df["tipo"] = df["ruc"].map(validar)
    invalidos = df["tipo"].isna().sum()
    df = df.dropna(subset=["tipo"]).drop_duplicates("ruc")

    cols = {"ruc": "ruc", "razon": "razon_social"}
    for k, v in (("prov", "provincia"), ("canton", "canton"),
                 ("parroquia", "parroquia"), ("estado", "estado")):
        if k in m: cols[m[k]] = v
    base = df[list(cols)].rename(columns=cols)

    Path(a.salida).mkdir(exist_ok=True)
    for tipo, arch in (("JURIDICA", "rucs_companias.csv"),
                       ("NATURAL", "rucs_personas.csv")):
        sub = base[base["tipo"] == tipo] if "tipo" in base else base
        sub = df[df["tipo"] == tipo]
        out = Path(a.salida) / arch
        sub[list(cols.values())].to_csv(out, index=False, encoding="utf-8-sig")
        print(f"{tipo}: {len(sub)} -> {out}")

    print(f"\nLeidos {total} | fuera de filtro/duplicados {total - len(df) - invalidos}"
          f" | RUC invalidos descartados {invalidos}")

if __name__ == "__main__":
    main()