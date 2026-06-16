"""B — Paramètres.

  B1  AHT par TYPE DE TÂCHE x GROUP (fallback : moyenne par type de tâche)
  B2  productivité / shrinkage / occupation cible          (productivité -> A6 dim_team)
  B3  coûts horaires par équipe                            (-> A6 dim_team)
  B4  objectifs de service : SLA, occupation cible/max, marge de sécurité

Le YAML porte B2/B4 par level ; les CSV portent B1 (AHT) et le taux de contact
(rattaché ici car il complète l'ancrage de volume de la couche C).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml


@dataclass
class Params:
    config: dict
    aht: pd.DataFrame           # task_type_id, group_id, aht_seconds
    contact_rate: pd.DataFrame  # task_type_id, group_id, contact_rate

    # --- B2 / B4 : accès par level -------------------------------------------
    def level_cfg(self, level: int) -> dict:
        return self.config["levels"][int(level)]

    def levels_frame(self) -> pd.DataFrame:
        """Paramètres par level sous forme tabulaire (pour jointures vectorisées)."""
        rows = []
        for lvl, cfg in self.config["levels"].items():
            rows.append(
                {
                    "level": int(lvl),
                    "target_occupancy": cfg["target_occupancy"],
                    "max_occupancy": cfg["max_occupancy"],
                    "shrinkage": cfg["shrinkage"],
                    "safety_margin": cfg["safety_margin"],
                    "sla_seconds": cfg["sla_seconds"],
                }
            )
        return pd.DataFrame(rows)

    # --- B1 : AHT avec fallback ----------------------------------------------
    def aht_with_fallback(self, keys: pd.DataFrame) -> pd.Series:
        """AHT (secondes) pour des paires (task_type_id, group_id).

        Fallback en cascade : (tâche, group) -> moyenne par tâche -> moyenne globale.
        """
        merged = keys.merge(self.aht, on=["task_type_id", "group_id"], how="left")
        per_task = self.aht.groupby("task_type_id")["aht_seconds"].mean()
        global_mean = self.aht["aht_seconds"].mean()
        fallback_task = merged["task_type_id"].map(per_task)
        return merged["aht_seconds"].fillna(fallback_task).fillna(global_mean)

    def contact_rate_with_fallback(self, keys: pd.DataFrame) -> pd.Series:
        merged = keys.merge(self.contact_rate, on=["task_type_id", "group_id"], how="left")
        per_task = self.contact_rate.groupby("task_type_id")["contact_rate"].mean()
        global_mean = self.contact_rate["contact_rate"].mean()
        fallback_task = merged["task_type_id"].map(per_task)
        return merged["contact_rate"].fillna(fallback_task).fillna(global_mean)


def load_config(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # Normalise les clés de level en int (YAML peut les lire en str).
    cfg["levels"] = {int(k): v for k, v in cfg["levels"].items()}
    return cfg


def load_params(data_dir, config_path) -> Params:
    data_dir = Path(data_dir)
    cfg = load_config(config_path)
    aht = pd.read_csv(data_dir / "param_aht.csv")
    contact_rate = pd.read_csv(data_dir / "param_contact_rate.csv")
    return Params(config=cfg, aht=aht, contact_rate=contact_rate)
