"""Helpers partagés par les pages de l'application (chargement, cache, filtres)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from staffing import db, ingest, model, optimizer, reporting  # noqa: E402
from staffing.timespine import BUSINESS_TZ  # noqa: E402

DB_PATH = db.DB_PATH


# --- amorçage ----------------------------------------------------------------
def ensure_db():
    if not db.db_exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_db.py")], cwd=str(ROOT), check=True)
        # Sur Cloud, le disque est éphémère : à chaque redéploiement on réamorce la
        # base puis on ré-ingère les CSV déposés dans data/incoming/ (workflow mensuel).
        try:
            for r in ingest.ingest_dir():
                print(f"[ingest] {r['file']} → {r['table']} ({r['total_after']} lignes)")
        except Exception as exc:  # ne jamais bloquer le démarrage de l'app
            st.warning(f"Ingestion de data/incoming/ ignorée : {exc}")


def db_version() -> float:
    """mtime de la base : sert de clé de cache (invalidé à chaque écriture)."""
    return DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0


# --- lecture / écriture ------------------------------------------------------
@st.cache_data(show_spinner=False)
def _read(name: str, _v: float) -> pd.DataFrame:
    return db.read_table(name)


def table(name: str) -> pd.DataFrame:
    return _read(name, db_version())


def save_table(name: str, df: pd.DataFrame):
    db.write_table(name, df)
    st.cache_data.clear()


# --- ingestion CSV (workflow mensuel) ----------------------------------------
def ingest_incoming() -> list[dict]:
    """Ré-ingère tous les fichiers de data/incoming/ et invalide le cache."""
    reports = ingest.ingest_dir()
    st.cache_data.clear()
    return reports


def ingest_upload(table: str, buffer, label: str = "") -> dict:
    """Ingère un CSV uploadé dans la table choisie et invalide le cache."""
    rep = ingest.ingest_buffer(table, buffer, label=label)
    st.cache_data.clear()
    return rep


# --- calculs (cachés sur le mois + version base) -----------------------------
@st.cache_data(show_spinner="Calcul de la demande forecast…")
def demand(month: str, _v: float) -> pd.DataFrame:
    return model.build_forecast_demand(month)


@st.cache_data(show_spinner="Dimensionnement Erlang C par groupe…")
def required(month: str, _v: float) -> pd.DataFrame:
    from staffing.model import required_by_group
    return required_by_group(demand(month, _v))


@st.cache_data(show_spinner="Calcul de la couverture…")
def coverage(month: str, _v: float) -> dict:
    return optimizer.build_coverage(month)


@st.cache_data(show_spinner=False)
def actuals(_v: float) -> pd.DataFrame:
    return model.actuals_vs_forecast()


def run_optimizer(month: str, **kw):
    optimizer.optimize_allocation(month, **kw)
    st.cache_data.clear()


# --- petites aides -----------------------------------------------------------
def months() -> list[str]:
    return sorted(table("pax_forecast")["month"].unique())


def month_selector(label="Mois de planification", default="2026-07", key="month"):
    ms = months()
    idx = ms.index(default) if default in ms else 0
    return st.selectbox(label, ms, index=idx, key=key)


def kpi_row(items: list[tuple[str, str]]):
    cols = st.columns(len(items))
    for c, (label, value) in zip(cols, items):
        c.metric(label, value)


def eur(x) -> str:
    return f"{x:,.0f} €"
