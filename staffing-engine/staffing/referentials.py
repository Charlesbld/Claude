"""A2–A7 — Référentiels.

Chargement et validation légère des dimensions :

  A2  dim_supply        — modes de transport (avion, train, bus, ferry)
  A3  dim_region        — régions (France, Espagne…) + pays + fuseau
  A4  dim_group_monthly — mapping Supply x Region au grain MENSUEL (snapshot/mois)
  A5  dim_task_type     — types de tâches + level (1 = externe, 2 = interne)
  A6  dim_team          — ressources (sourcing, level, fuseau, effectif, prod, coût)
  A7  team_skills       — routage équipe x type de tâche (très mince ; le level suffit)

NB : "Supply" (A2) = mode de transport, à ne pas confondre avec l'OFFRE de
capacité (couche D). Les deux ne partagent aucune colonne de mesure.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass
class Referentials:
    supply: pd.DataFrame        # supply_id, supply_label
    region: pd.DataFrame        # region_id, region_label, country_code, timezone
    group_monthly: pd.DataFrame # month, group_id, supply_id, region_id, passengers, active
    task_type: pd.DataFrame     # task_type_id, task_type_label, level
    team: pd.DataFrame          # team_id, ..., level, sourcing, timezone, headcount, productivity, hourly_cost
    team_skills: pd.DataFrame   # team_id, task_type_id, can_handle

    def task_level_map(self) -> dict:
        return dict(zip(self.task_type["task_type_id"], self.task_type["level"]))


def _read(data_dir: Path, name: str) -> pd.DataFrame:
    path = Path(data_dir) / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Référentiel manquant : {path}. Lancez d'abord scripts/generate_synthetic_data.py."
        )
    return pd.read_csv(path)


def load_referentials(data_dir) -> Referentials:
    data_dir = Path(data_dir)
    refs = Referentials(
        supply=_read(data_dir, "dim_supply"),
        region=_read(data_dir, "dim_region"),
        group_monthly=_read(data_dir, "dim_group_monthly"),
        task_type=_read(data_dir, "dim_task_type"),
        team=_read(data_dir, "dim_team"),
        team_skills=_read(data_dir, "team_skills"),
    )
    _validate(refs)
    return refs


def _validate(refs: Referentials) -> None:
    # Contrainte structurante : Level 1 <-> externe, Level 2 <-> interne.
    bad = refs.team[
        ((refs.team["level"] == 1) & (refs.team["sourcing"] != "external"))
        | ((refs.team["level"] == 2) & (refs.team["sourcing"] != "internal"))
    ]
    if not bad.empty:
        raise ValueError(
            "Contrainte level/sourcing violée (L1=externe, L2=interne) pour : "
            f"{bad['team_id'].tolist()}"
        )

    # Cohérence des clés étrangères du group mensuel.
    for col, ref in [("supply_id", refs.supply["supply_id"]), ("region_id", refs.region["region_id"])]:
        unknown = set(refs.group_monthly[col]) - set(ref)
        if unknown:
            raise ValueError(f"dim_group_monthly.{col} inconnu : {sorted(unknown)}")
