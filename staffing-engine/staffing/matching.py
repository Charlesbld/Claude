"""E — Rapprochement.

  E1  Couverture : ETP requis (C) vs capacité dispo (D) par bucket x level.
      La contrainte L1<->externe / L2<->interne est garantie en amont (la
      capacité d'un level ne provient que d'équipes de ce level). Au sein d'un
      level, la capacité se MUTUALISE entre régions et types de tâche (pooling) :
      on agrège donc les deux côtés à la maille (bucket_utc, level).
  E2  Écarts (sur/sous-staffing), coût, et OCCUPATION RÉELLE recalculée ici,
      à comparer à la cible (B4).

Identité de cohérence : si capacité == requis, alors occupation_réelle == cible.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .params import Params
from .timespine import BUCKET_HOURS

EPS = 1e-9


def build_matching(demand: pd.DataFrame, supply: pd.DataFrame, params: Params,
                   spine: pd.DatetimeIndex) -> pd.DataFrame:
    """Joint demande et offre à la maille (bucket_utc, level) et calcule les
    indicateurs de couverture."""
    levels = sorted(set(demand["level"]).union(supply["level"]))

    # Agrégation de la demande (pooling intra-level : somme sur tâches & groups).
    dem = (
        demand.groupby(["bucket_utc", "level"], as_index=False)[
            ["contacts", "workload_hours", "required_fte"]
        ].sum()
    )
    # Agrégation de l'offre (pooling intra-level : somme sur les équipes).
    sup = (
        supply.groupby(["bucket_utc", "level"], as_index=False)[
            ["scheduled_headcount", "effective_capacity", "cost"]
        ].sum()
    )

    # Grille complète bucket x level pour ne manquer aucun trou de couverture.
    grid = pd.MultiIndex.from_product([spine, levels], names=["bucket_utc", "level"]).to_frame(index=False)
    m = grid.merge(dem, on=["bucket_utc", "level"], how="left").merge(sup, on=["bucket_utc", "level"], how="left")
    measure_cols = ["contacts", "workload_hours", "required_fte", "scheduled_headcount", "effective_capacity", "cost"]
    m[measure_cols] = m[measure_cols].fillna(0.0)

    # Paramètres de service par level (B2/B4).
    lv = params.levels_frame()
    m = m.merge(lv, on="level", how="left")

    # --- E2 : écarts -------------------------------------------------------------
    m["required_fte_target"] = m["required_fte"] * (1.0 + m["safety_margin"])
    m["gap_fte"] = m["effective_capacity"] - m["required_fte"]                # >0 sur-staff, <0 sous-staff
    m["gap_fte_target"] = m["effective_capacity"] - m["required_fte_target"]
    m["understaffed"] = m["gap_fte"] < -EPS

    # Taux de couverture (capacité / requis). Pas de demande -> NaN ou +inf.
    req = m["required_fte"].to_numpy()
    cap = m["effective_capacity"].to_numpy()
    coverage = np.divide(cap, req, out=np.full_like(cap, np.nan), where=req > EPS)
    coverage = np.where((req <= EPS) & (cap > EPS), np.inf, coverage)
    m["coverage_ratio"] = coverage

    # --- E2 : occupation RÉELLE en sortie ----------------------------------------
    # occ_réelle = charge / (capacité_effective x pas_horaire x (1 - shrinkage))
    avail_prod_hours = cap * BUCKET_HOURS * (1.0 - m["shrinkage"].to_numpy())
    wl = m["workload_hours"].to_numpy()
    occ = np.divide(wl, avail_prod_hours, out=np.zeros_like(wl), where=avail_prod_hours > EPS)
    occ = np.where((avail_prod_hours <= EPS) & (wl > EPS), np.inf, occ)  # charge non couverte
    m["real_occupancy"] = occ
    m["over_max_occupancy"] = m["real_occupancy"] > m["max_occupancy"]

    return m.sort_values(["level", "bucket_utc"]).reset_index(drop=True)
