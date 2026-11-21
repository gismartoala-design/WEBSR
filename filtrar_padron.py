#!/usr/bin/env python3
"""Filtra el padron del SRI: deja solo RUCs validos de Guayas, separados por tipo."""
import argparse, csv, re, sys
from pathlib import Path

import pandas as pd

ALIAS = {
    "ruc": ["numero_ruc", "num_ruc", "ruc", "numero de ruc"],
    "razon": ["razon_social", "razon social", "nombre", "contribuyente"],
    "prov": ["descripcion_provincia_est", "provincia", "cod_provincia", "prov"],
    "canton": ["descripcion_canton_est", "canton", "cod_canton"],
    "parroquia": ["descripcion_parroquia_est", "parroquia"],
    "estado": ["estado_contribuyente", "estado", "situacion"],
    "tipo": ["tipo_contribuyente"],
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

def validar(ruc, tipo=""):
    """Valida formato y usa la clasificación del padrón oficial del SRI.

    El SRI indica que no se deben rechazar sociedades ni extranjeros por
    algoritmos de dígito verificador. No se modifica el RUC del padrón.
    """
    if not re.fullmatch(r"[0-9]{13}", ruc) or ruc[10:] != "001":
        return None
    tipo = str(tipo).strip().upper()
    if tipo == "PERSONA NATURAL":
        return "NATURAL"
    if tipo == "SOCIEDAD":
        return "JURIDICA"
    if ruc[2] in "012345":
        return "NATURAL"
    if ruc[2] in "69":
        return "JURIDICA"
    return None

def leer(entrada):
    for f in entrada:
        if f.lower().endswith((".xlsx", ".xls")):
            yield pd.read_excel(f, dtype=str).fillna("")
        else:
            with open(f, encoding="utf-8-sig") as fh:
                sep = csv.Sniffer().sniff(fh.readline(), delimiters="|,;\t").delimiter
            yield from pd.read_csv(f, dtype=str, sep=sep, encoding="utf-8-sig",
                                   keep_default_na=False, chunksize=100_000)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("entrada", nargs="+")
    p.add_argument("--canton", default="GUAYAQUIL")
    p.add_argument("--todo-guayas", action="store_true")
    p.add_argument("--incluir-inactivos", action="store_true")
    p.add_argument("--salida", default="rucs")
    a = p.parse_args()

    partes = []
    total = invalidos = 0
    for df in leer(a.entrada):
        m = detectar(df)
        total += len(df)
        if "prov" not in m or (not a.todo_guayas and "canton" not in m):
            sys.exit("Faltan columnas de ubicación para aplicar el filtro solicitado")
        prov = df[m["prov"]].str.upper().str.strip()
        df = df[prov.isin(["GUAYAS", "09", "9"])].copy()
        if not a.todo_guayas:
            df = df[df[m["canton"]].str.upper().str.strip() == a.canton.upper()].copy()
        if not a.incluir_inactivos and "estado" in m:
            df = df[df[m["estado"]].str.upper().str.strip().isin(["ACTIVO", "ACTIVA"])].copy()
        df["ruc"] = df[m["ruc"]].str.strip()
        tipos = df[m["tipo"]] if "tipo" in m else [""] * len(df)
        df["tipo"] = [validar(ruc, tipo) for ruc, tipo in zip(df["ruc"], tipos)]
        invalidos += int(df["tipo"].isna().sum())
        df = df.dropna(subset=["tipo"])
        cols = {m["razon"]: "razon_social"}
        for k, v in (("prov", "provincia"), ("canton", "canton"),
                     ("parroquia", "parroquia"), ("estado", "estado")):
            if k in m: cols[m[k]] = v
        base = df.rename(columns=cols)
        partes.append(base[["ruc", "tipo", *cols.values()]])
    df = pd.concat(partes, ignore_index=True).drop_duplicates("ruc")
    Path(a.salida).mkdir(parents=True, exist_ok=True)
    for tipo, arch in (("JURIDICA", "rucs_companias.csv"), ("NATURAL", "rucs_personas.csv")):
        sub = df.loc[df["tipo"] == tipo].drop(columns="tipo")
        out = Path(a.salida) / arch
        sub.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"{tipo}: {len(sub)} -> {out}")
    print(f"\nLeidos {total} | fuera de filtro/duplicados {total - len(df) - invalidos}"
          f" | RUC con formato/tipo invalido descartados {invalidos}")

if __name__ == "__main__":
    main()
