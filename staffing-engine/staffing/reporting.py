"""F — Restitution.

  F1  Heatmap de couverture (time-of-day x jour), par level.
  F2  Synthèse coûts & ETP (par level, ventilée par group / région).
  F3  Liste des trous : intervalles sous-staffés prioritaires (runs contigus).

Les fonctions renvoient des DataFrames "propres" prêts à tracer ; l'app
Streamlit s'occupe du rendu. L'heure d'affichage est convertie dans
`business_timezone` (par défaut Europe/Paris) ; les données restent en UTC.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .timespine import BUCKET_HOURS, BUSINESS_TZ

QUARTER = pd.Timedelta(minutes=15)


# --- F1 : heatmap ------------------------------------------------------------
def heatmap_frame(matching: pd.DataFrame, level: int, value: str = "coverage_ratio",
                  tz: str = BUSINESS_TZ) -> pd.DataFrame:
    """Frame tidy (date_local, tod, tod_min, value) pour un level donné."""
    sub = matching[matching["level"] == level].copy()
    local = sub["bucket_utc"].dt.tz_convert(tz)
    sub["date_local"] = local.dt.date
    sub["tod"] = local.dt.strftime("%H:%M")
    sub["tod_min"] = local.dt.hour * 60 + local.dt.minute
    return sub[["date_local", "tod", "tod_min", value]].rename(columns={value: "value"})


def heatmap_pivot(matching: pd.DataFrame, level: int, value: str = "coverage_ratio",
                  tz: str = BUSINESS_TZ) -> pd.DataFrame:
    """Matrice time-of-day (lignes) x jour (colonnes) pour la heatmap."""
    tidy = heatmap_frame(matching, level, value, tz)
    order = tidy[["tod", "tod_min"]].drop_duplicates().sort_values("tod_min")["tod"].tolist()
    piv = tidy.pivot_table(index="tod", columns="date_local", values="value", aggfunc="mean")
    return piv.reindex(order)


# --- F2 : synthèse coûts & ETP ----------------------------------------------
def cost_fte_summary(demand: pd.DataFrame, supply: pd.DataFrame, matching: pd.DataFrame) -> dict:
    """Renvoie trois tables : par level, requis par group/région, coût par équipe."""
    by_level = (
        matching.groupby("level")
        .agg(
            total_cost=("cost", "sum"),
            workload_hours=("workload_hours", "sum"),
            required_fte_peak=("required_fte", "max"),
            required_fte_avg=("required_fte", "mean"),
            capacity_peak=("effective_capacity", "max"),
            capacity_avg=("effective_capacity", "mean"),
            buckets_understaffed=("understaffed", "sum"),
        )
        .reset_index()
    )
    by_level["hours_understaffed"] = by_level["buckets_understaffed"] * BUCKET_HOURS
    # heures-agent requises = Σ ETP_requis * pas_horaire (ETP requis = agents concurrents/bucket)
    by_level["required_agent_hours"] = (
        matching.groupby("level")["required_fte"].sum().reindex(by_level["level"]).to_numpy() * BUCKET_HOURS
    )

    required_by_group = (
        demand.groupby(["level", "region_id", "supply_id", "group_id"])
        .agg(workload_hours=("workload_hours", "sum"), contacts=("contacts", "sum"))
        .reset_index()
    )
    # required_agent_hours ≈ workload_hours / (1 - shrinkage), but here we use
    # workload_hours as a proxy for ranking (demand doesn't carry required_fte directly).
    required_by_group["required_agent_hours"] = required_by_group["workload_hours"]
    required_by_group = required_by_group.sort_values("required_agent_hours", ascending=False)

    cost_by_team = (
        supply.groupby(["level", "sourcing", "team_id"])
        .agg(cost=("cost", "sum"),
             agent_hours=("agents", lambda s: s.sum() * BUCKET_HOURS),
             capacity_hours=("effective_capacity", lambda s: s.sum() * BUCKET_HOURS))
        .reset_index()
        .sort_values("cost", ascending=False)
    )
    return {"by_level": by_level, "required_by_group": required_by_group, "cost_by_team": cost_by_team}


# --- F3 : liste des trous ----------------------------------------------------
def gap_intervals(matching: pd.DataFrame, top: int | None = 20) -> pd.DataFrame:
    """Intervalles contigus sous-staffés, triés par déficit cumulé (ETP-heures)."""
    sub = matching[matching["understaffed"]].sort_values(["level", "bucket_utc"]).copy()
    if sub.empty:
        return pd.DataFrame(
            columns=["level", "start_utc", "end_utc", "duration_h", "n_buckets",
                     "max_deficit_fte", "avg_deficit_fte", "total_deficit_fte_hours"]
        )

    # Découpage en runs : rupture dès qu'un bucket n'est pas contigu au précédent.
    run_break = sub.groupby("level")["bucket_utc"].diff().ne(QUARTER)
    sub["run_id"] = run_break.groupby(sub["level"]).cumsum()

    g = sub.groupby(["level", "run_id"])
    intervals = g.agg(
        start_utc=("bucket_utc", "min"),
        end_utc=("bucket_utc", "max"),
        n_buckets=("bucket_utc", "size"),
        max_deficit_fte=("gap_fte", lambda s: -s.min()),
        total_deficit=("gap_fte", lambda s: -s.sum()),
    ).reset_index()

    intervals["end_utc"] = intervals["end_utc"] + QUARTER  # borne de fin exclusive
    intervals["duration_h"] = intervals["n_buckets"] * BUCKET_HOURS
    intervals["total_deficit_fte_hours"] = intervals["total_deficit"] * BUCKET_HOURS
    intervals["avg_deficit_fte"] = intervals["total_deficit"] / intervals["n_buckets"]
    intervals = intervals.drop(columns=["run_id", "total_deficit"]).sort_values(
        "total_deficit_fte_hours", ascending=False
    )
    return intervals.head(top) if top else intervals


# --- KPI de tête (printout + app) -------------------------------------------
def summary_kpis(matching: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for level, sub in matching.groupby("level"):
        covered = sub[sub["required_fte"] > 1e-9]
        rows.append(
            {
                "level": level,
                "total_cost": sub["cost"].sum(),
                "required_fte_peak": sub["required_fte"].max(),
                "capacity_peak": sub["effective_capacity"].max(),
                "pct_buckets_understaffed": 100.0 * sub["understaffed"].mean(),
                "hours_understaffed": sub["understaffed"].sum() * BUCKET_HOURS,
                "real_occupancy_p50": covered["real_occupancy"].replace([np.inf], np.nan).median(),
                "real_occupancy_p95": covered["real_occupancy"].replace([np.inf], np.nan).quantile(0.95),
            }
        )
    return pd.DataFrame(rows)
