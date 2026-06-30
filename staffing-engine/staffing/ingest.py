"""Ingestion de fichiers CSV (workflow mensuel) dans la base SQLite.

Principe : on dépose des CSV dans ``data/incoming/`` (un fichier par table,
nommé d'après la table cible). Le nom **avant** ``__`` ou ``.csv`` identifie la
table ; ce qui suit ``__`` est un libellé libre (souvent le mois) :

    pax_real.csv                → table pax_real
    pax_real__2026-07.csv       → table pax_real (libellé « 2026-07 »)
    contact_rate_forecast__juil.csv → table contact_rate_forecast

Chaque fichier est **upserté** par la clé de la table : les lignes dont la clé
existe déjà sont **remplacées**, les nouvelles sont **ajoutées**, le reste est
**conservé**. C'est idempotent (réingérer le même fichier ne duplique rien) et
cumulatif (pousser un nouveau mois l'ajoute à l'historique).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import db

INCOMING = db.DB_PATH.parent / "incoming"
TEMPLATES = db.DB_PATH.parent / "templates"

# Colonnes à garder en texte (le reste est converti en numérique).
STRING_COLS = {
    "month", "region_id", "supply_id", "task_type_id", "team_id", "group_id",
    "country_code", "timezone", "sourcing", "region_label", "supply_label",
    "task_type_label", "team_label", "start_local", "end_local",
}


def expected_columns(spec: db.TableSpec) -> list[str]:
    """Colonnes attendues d'une table = dimensions + mesures (dédoublonnées, ordonnées)."""
    return list(dict.fromkeys([*spec.dims, *spec.measures]))


def table_from_filename(path) -> str:
    """Déduit la table cible du nom de fichier (partie avant « __ » ou « .csv »)."""
    return Path(path).stem.split("__")[0]


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if c in STRING_COLS:
            df[c] = df[c].astype(str).str.strip()
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _make_key(df: pd.DataFrame, keys: list[str]) -> pd.Series:
    return df[keys].astype(str).agg("|".join, axis=1)


def upsert(existing: pd.DataFrame, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Remplace dans ``existing`` les lignes dont la clé apparaît dans ``new``, puis concatène."""
    new = new.drop_duplicates(subset=keys, keep="last")
    if existing.empty:
        return new.reset_index(drop=True)
    new_keys = set(_make_key(new, keys))
    kept = existing[~_make_key(existing, keys).isin(new_keys)]
    return pd.concat([kept, new], ignore_index=True)


def _prepare(table: str, raw: pd.DataFrame) -> pd.DataFrame:
    """Valide les colonnes et normalise les types d'un DataFrame pour une table donnée."""
    if table not in db.TABLES:
        raise ValueError(
            f"Table inconnue « {table} ». Tables valides : {', '.join(db.TABLES)}.")
    spec = db.TABLES[table]
    cols = expected_columns(spec)
    raw = raw.rename(columns=lambda c: str(c).strip())
    missing = [c for c in cols if c not in raw.columns]
    if missing:
        raise ValueError(
            f"Colonnes manquantes pour « {table} » : {missing}. Attendu : {cols}.")
    return _coerce_types(raw[cols])


def _count_coerce_nans(raw: pd.DataFrame, prepared: pd.DataFrame) -> dict[str, int]:
    """Compte les valeurs qui ont été converties en NaN par coerce_types (données invalides)."""
    counts = {}
    for col in prepared.columns:
        if col in STRING_COLS:
            continue
        nans_after = prepared[col].isna().sum()
        nans_before = raw[col].isna().sum() if col in raw.columns else 0
        if nans_after > nans_before:
            counts[col] = int(nans_after - nans_before)
    return counts


def ingest_frame(table: str, raw: pd.DataFrame, db_path=db.DB_PATH, label: str = "") -> dict:
    """Upsert d'un DataFrame dans une table ; renvoie un rapport d'ingestion."""
    new = _prepare(table, raw)  # valide la table et les colonnes, normalise les types
    coerce_warnings = _count_coerce_nans(raw, new)
    spec = db.TABLES[table]
    existing = db.read_table(table, db_path) if table in db.list_tables(db_path) else pd.DataFrame()
    before = len(existing)
    merged = upsert(existing, new, spec.keys)
    db.write_table(table, merged, db_path)
    replaced = before - (len(merged) - len(new))
    return {"file": label or f"<{table}>", "table": table, "rows_in": len(new),
            "rows_replaced": int(replaced), "total_after": len(merged),
            "coerce_warnings": coerce_warnings}


def ingest_file(path, db_path=db.DB_PATH) -> dict:
    """Ingestion d'un fichier CSV (table déduite du nom)."""
    path = Path(path)
    table = table_from_filename(path)
    return ingest_frame(table, pd.read_csv(path), db_path, label=path.name)


def ingest_buffer(table: str, buffer, db_path=db.DB_PATH, label: str = "") -> dict:
    """Ingestion d'un CSV uploadé (objet fichier), table choisie explicitement."""
    return ingest_frame(table, pd.read_csv(buffer), db_path, label=label)


def ingest_dir(folder=INCOMING, db_path=db.DB_PATH) -> list[dict]:
    """Ingestion de tous les ``*.csv`` d'un dossier (triés par nom)."""
    folder = Path(folder)
    if not folder.exists():
        return []
    return [ingest_file(p, db_path) for p in sorted(folder.glob("*.csv"))]


def template(table: str, db_path=db.DB_PATH, with_rows: bool = True) -> pd.DataFrame:
    """Modèle CSV d'une table : ses colonnes, pré-rempli avec les données actuelles si demandé."""
    if table not in db.TABLES:
        raise ValueError(f"Table inconnue « {table} ».")
    cols = expected_columns(db.TABLES[table])
    if with_rows and table in db.list_tables(db_path):
        cur = db.read_table(table, db_path)
        keep = [c for c in cols if c in cur.columns]
        return cur[keep]
    return pd.DataFrame(columns=cols)
