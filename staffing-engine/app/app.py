#!/usr/bin/env python3
"""F — Restitution interactive (Streamlit).

Lance le tableau de bord de couverture / coûts / trous à partir des sorties du
pipeline (data/processed/*.parquet).

    cd staffing-engine
    python scripts/run_pipeline.py        # produit les parquet
    streamlit run app/app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from staffing import reporting  # noqa: E402
from staffing.timespine import BUSINESS_TZ  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"

st.set_page_config(page_title="Moteur de staffing — couverture 15 min", layout="wide")

# Configuration d'affichage par métrique de heatmap.
METRICS = {
    "coverage_ratio": dict(label="Taux de couverture (capacité / requis)", midpoint=1.0,
                           scale="RdYlGn", clip=(0.0, 3.0)),
    "gap_fte": dict(label="Écart d'ETP (capacité − requis)", midpoint=0.0,
                    scale="RdYlGn", clip=None),
    "real_occupancy": dict(label="Occupation réelle", midpoint=None,
                           scale="RdYlGn_r", clip=(0.0, 2.0)),
}


@st.cache_data
def load_data(mtime: float):
    matching = pd.read_parquet(PROCESSED / "matching.parquet")
    demand = pd.read_parquet(PROCESSED / "demand.parquet")
    supply = pd.read_parquet(PROCESSED / "supply.parquet")
    meta = json.loads((PROCESSED / "run_meta.json").read_text(encoding="utf-8"))
    return matching, demand, supply, meta


def fmt(n, suffix=""):
    return f"{n:,.0f}{suffix}"


# --- Amorçage : génère données + pipeline si absents (déploiement cloud) -----
@st.cache_resource(show_spinner="Initialisation : génération des données et calcul du pipeline…")
def ensure_outputs():
    import subprocess
    if (PROCESSED / "matching.parquet").exists():
        return
    if not (RAW / "dim_team.csv").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_synthetic_data.py")],
                       cwd=str(ROOT), check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "run_pipeline.py")],
                   cwd=str(ROOT), check=True)


# --- Chargement --------------------------------------------------------------
ensure_outputs()
if not (PROCESSED / "matching.parquet").exists():
    st.error("Échec de l'initialisation. Lancez `python scripts/run_pipeline.py` en local.")
    st.stop()

mtime = (PROCESSED / "matching.parquet").stat().st_mtime
matching, demand, supply, meta = load_data(mtime)

# --- Barre latérale ----------------------------------------------------------
st.sidebar.header("Filtres")
levels = sorted(matching["level"].unique())
level = st.sidebar.radio("Level", levels, format_func=lambda l: f"Level {l} ({'externe' if l == 1 else 'interne'})")
tz = st.sidebar.radio("Fuseau d'affichage", [BUSINESS_TZ, "UTC"], index=0)
metric = st.sidebar.selectbox("Métrique de la heatmap", list(METRICS), format_func=lambda m: METRICS[m]["label"])

local = matching["bucket_utc"].dt.tz_convert(tz)
all_dates = sorted(local.dt.date.unique())
if len(all_dates) > 1:
    d0, d1 = st.sidebar.select_slider("Plage de dates", options=all_dates,
                                      value=(all_dates[0], all_dates[-1]))
else:
    d0 = d1 = all_dates[0]

mask = (local.dt.date >= d0) & (local.dt.date <= d1)
m_lvl = matching[(matching["level"] == level) & mask].copy()
dem_lvl = demand[demand["level"] == level].copy()
sup_lvl = supply[supply["level"] == level].copy()

# --- En-tête -----------------------------------------------------------------
st.title("Moteur de staffing — couverture au pas de 15 min")
st.caption(f"Périmètre du run : {meta.get('scope')} · horizon {meta['horizon']['start']} → "
           f"{meta['horizon']['end']} · {meta['n_buckets']} buckets · affichage {tz}")

# --- KPI ---------------------------------------------------------------------
k = reporting.summary_kpis(m_lvl).iloc[0] if not m_lvl.empty else None
if k is not None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Coût total (€)", fmt(k["total_cost"]))
    c2.metric("ETP requis (pic)", f"{k['required_fte_peak']:.1f}")
    c3.metric("Capacité (pic)", f"{k['capacity_peak']:.1f}")
    c4.metric("Buckets sous-staffés", f"{k['pct_buckets_understaffed']:.0f} %")
    c5.metric("Heures sous-staffées", f"{k['hours_understaffed']:.0f} h")

# --- F1 : heatmap ------------------------------------------------------------
st.subheader(f"F1 — Heatmap : {METRICS[metric]['label']}")
piv = reporting.heatmap_pivot(m_lvl, level, value=metric, tz=tz)
cfg = METRICS[metric]
z = piv.copy()
if cfg["clip"]:
    z = z.clip(*cfg["clip"])
z = z.replace([float("inf"), float("-inf")], pd.NA)
fig = px.imshow(z, aspect="auto", color_continuous_scale=cfg["scale"],
                color_continuous_midpoint=cfg["midpoint"],
                labels=dict(x="Jour", y=f"Heure ({tz})", color=metric))
fig.update_yaxes(autorange="reversed")
fig.update_layout(height=600, margin=dict(l=10, r=10, t=10, b=10))
st.plotly_chart(fig, width="stretch")
st.caption("Vert = bien couvert / occupation saine · Rouge = sous-staffé (ou occupation excessive). "
           "Cases vides = pas de demande sur le bucket.")

# --- F2a : requis vs capacité dans le temps ----------------------------------
st.subheader("F2 — ETP requis vs capacité disponible")
ts = m_lvl.copy()
ts["heure"] = ts["bucket_utc"].dt.tz_convert(tz)
fig2 = go.Figure()
fig2.add_trace(go.Scatter(x=ts["heure"], y=ts["effective_capacity"], name="Capacité (ETP eff.)",
                          fill="tozeroy", line=dict(color="#2c7fb8")))
fig2.add_trace(go.Scatter(x=ts["heure"], y=ts["required_fte"], name="ETP requis",
                          line=dict(color="#d7301f", width=2)))
fig2.add_trace(go.Scatter(x=ts["heure"], y=ts["required_fte_target"], name="ETP requis + marge",
                          line=dict(color="#d7301f", width=1, dash="dot")))
fig2.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                   legend=dict(orientation="h", y=1.1))
st.plotly_chart(fig2, width="stretch")

# --- F2b : ventilations ------------------------------------------------------
summary = reporting.cost_fte_summary(dem_lvl, sup_lvl, m_lvl)
col_a, col_b = st.columns(2)
with col_a:
    st.markdown("**Heures-agent requises par région / group**")
    g = summary["required_by_group"]
    if not g.empty:
        fig3 = px.bar(g, x="group_id", y="required_agent_hours", color="region_id",
                      labels=dict(required_agent_hours="Heures-agent requises", group_id="Group"))
        fig3.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig3, width="stretch")
with col_b:
    st.markdown("**Coût par équipe**")
    t = summary["cost_by_team"]
    if not t.empty:
        fig4 = px.bar(t, x="team_id", y="cost", color="sourcing",
                      labels=dict(cost="Coût (€)", team_id="Équipe"))
        fig4.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig4, width="stretch")

# --- F3 : trous --------------------------------------------------------------
st.subheader("F3 — Trous de couverture prioritaires (intervalles sous-staffés)")
gaps = reporting.gap_intervals(m_lvl, top=20)
if gaps.empty:
    st.success("Aucun trou de couverture sur le périmètre sélectionné.")
else:
    show = gaps.copy()
    show["début"] = show["start_utc"].dt.tz_convert(tz).dt.strftime("%a %d/%m %H:%M")
    show["fin"] = show["end_utc"].dt.tz_convert(tz).dt.strftime("%a %d/%m %H:%M")
    show = show[["début", "fin", "duration_h", "max_deficit_fte", "avg_deficit_fte",
                 "total_deficit_fte_hours"]].rename(columns={
        "duration_h": "durée (h)", "max_deficit_fte": "déficit max (ETP)",
        "avg_deficit_fte": "déficit moyen (ETP)", "total_deficit_fte_hours": "déficit cumulé (ETP·h)"})
    st.dataframe(show.style.format({
        "durée (h)": "{:.2f}", "déficit max (ETP)": "{:.1f}",
        "déficit moyen (ETP)": "{:.1f}", "déficit cumulé (ETP·h)": "{:.1f}"}),
        width="stretch", hide_index=True)
