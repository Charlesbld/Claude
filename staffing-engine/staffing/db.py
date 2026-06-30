"""Couche de données SQLite (persistance des tables éditables).

Choix V2 : toutes les tables du modèle vivent dans une base SQLite unique
(`data/staffing.db`), versionnable. Les CSV (PAX, tâches réelles…) sont
*ingérés* dans cette base ; l'appli édite ensuite directement les tables.

`TABLES` est le registre central : il décrit chaque table (libellé, clés,
dimensions, mesures, éditable ou non) et pilote l'explorateur type BI et les
éditeurs de l'application.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "staffing.db"


@dataclass(frozen=True)
class TableSpec:
    name: str
    label: str
    keys: list[str]
    dims: list[str]
    measures: list[str]
    editable: bool = False
    group: str = "Référentiels"
    note: str = ""
    derived: bool = False  # recalculée par le moteur (non stockée en base)


def _t(*a, **k):  # raccourci
    return TableSpec(*a, **k)


# --- Registre des tables -----------------------------------------------------
TABLES: dict[str, TableSpec] = {
    "region": _t("region", "Régions (A3)", ["region_id"],
                 ["region_id", "region_label", "country_code", "timezone"], [],
                 editable=True, note="Change rarement ; un pays + fuseau par région."),
    "supply": _t("supply", "Supply / modes de transport (A2)", ["supply_id"],
                 ["supply_id", "supply_label"], [], editable=True),
    "task_type": _t("task_type", "Types de tâche (A5)", ["task_type_id"],
                    ["task_type_id", "task_type_label", "level"], [], editable=True,
                    note="level 1 = externe, level 2 = interne (escalades)."),
    "group": _t("group", "Groupes commerciaux (A7)", ["group_id"],
                ["group_id", "group_label"], [], editable=True, group="Référentiels",
                note="Groupes de monétisation (ex. DIRECT_AIR, OTA, RAIL). Renommez le libellé librement."),
    "group_map": _t("group_map", "Mapping Group = Supply×Region par mois (A4)",
                    ["month", "region_id", "supply_id"],
                    ["month", "region_id", "supply_id", "group_id"], ["active"],
                    editable=True, group="Référentiels",
                    note="Pivot éditable par mois : quels (region×supply) sont actifs et leur group."),
    "pax_real": _t("pax_real", "PAX réels (indicatif)", ["month", "region_id", "supply_id"],
                   ["month", "region_id", "supply_id"], ["pax"], editable=True, group="Demande",
                   note="Passagers réels ingérés par CSV chaque mois. Sert à titre indicatif (comparaison)."),
    "pax_forecast": _t("pax_forecast", "PAX forecast (planification)", ["month", "region_id", "supply_id"],
                       ["month", "region_id", "supply_id"], ["pax"], editable=True, group="Demande",
                       note="Passagers forecastés (2–3 fois/an). Base du dimensionnement."),
    "tasks_real": _t("tasks_real", "Tâches réelles (actuals)",
                     ["month", "region_id", "supply_id", "task_type_id"],
                     ["month", "region_id", "supply_id", "task_type_id"], ["tasks"],
                     editable=True, group="Demande",
                     note="Volumes de tâches réels par pays×supply×task. Contact rate réel = tâches / PAX réels."),
    "contact_rate_forecast": _t("contact_rate_forecast", "Contact rate forecast (éditable)",
                                ["month", "region_id", "supply_id", "task_type_id"],
                                ["month", "region_id", "supply_id", "task_type_id"], ["contact_rate"],
                                editable=True, group="Demande",
                                note="Taux de contact retenu pour la prévision, par pays×supply×task et par mois."),
    "param_aht": _t("param_aht", "AHT par type de tâche (B1)", ["task_type_id"],
                    ["task_type_id", "group_id"], ["aht_seconds"], editable=True, group="Paramètres",
                    note="Temps de traitement moyen par task_type. group_id optionnel (NULL = tous les groupes)."),
    "service_params": _t("service_params", "Objectifs de service / Erlang C (B4)", ["level"],
                         ["level", "group_id"], ["sl_target", "sl_seconds", "shrinkage", "max_occupancy"],
                         editable=True, group="Paramètres",
                         note="Cible Erlang C (sl_target en ≤ sl_seconds), shrinkage, occupation max. "
                              "group_id optionnel : si NULL, s'applique à tous les groupes (fallback)."),
    "team": _t("team", "Équipes (A6)", ["team_id"],
               ["team_id", "team_label", "level", "sourcing", "country_code", "timezone"],
               ["productivity", "hourly_cost", "max_agents"], editable=True, group="Offre",
               note="Plus de headcount fixe : l'effectif est décidé par l'optimiseur. max_agents = plafond optionnel."),
    "team_availability": _t("team_availability", "Disponibilités d'équipe (D1)",
                            ["team_id", "dow", "start_local", "end_local"],
                            ["team_id", "dow", "start_local", "end_local"], [],
                            editable=True, group="Offre",
                            note="Fenêtres où une équipe PEUT travailler (heure locale). L'optimiseur place les agents dedans."),
    "team_group": _t("team_group", "Affectation équipes → groupes (A8)", ["team_id", "group_id"],
                     ["team_id", "group_id"], [], editable=True, group="Offre",
                     note="Chaque équipe se spécialise sur un ou plusieurs groupes commerciaux. "
                          "Une ligne = l'équipe peut traiter les contacts de ce groupe."),
    "profile_dow": _t("profile_dow", "Profil jour de semaine (mensuel→jour)", ["dow"],
                      ["dow"], ["weight"], editable=True, group="Profils",
                      note="Poids relatif des contacts par jour de semaine (remplace départ×lag)."),
    "profile_intraday": _t("profile_intraday", "Profil intraday (jour→bucket)", ["dow", "slot_local"],
                           ["dow", "slot_local"], ["weight"], editable=True, group="Profils",
                           note="Répartition des contacts sur les 96 créneaux locaux, par jour de semaine."),
    "allocation": _t("allocation", "Répartition d'agents (optimiseur, éditable)",
                     ["dow", "slot_utc", "team_id"],
                     ["dow", "slot_utc", "team_id"], ["agents"], editable=True, group="Offre",
                     note="Nb d'agents par équipe × créneau UTC × jour de semaine. Rempli par l'optimiseur, ajustable à la main."),
}

EDITABLE = [n for n, s in TABLES.items() if s.editable]

# Colonnes conservées en texte ; toute autre colonne connue (mesures + dims
# comme dow/slot/level/active) est forcée en numérique.
TEXT_COLUMNS = {
    "month", "region_id", "supply_id", "task_type_id", "team_id", "group_id",
    "country_code", "timezone", "sourcing", "region_label", "supply_label",
    "task_type_label", "team_label", "group_label", "start_local", "end_local",
}


def coerce_types(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Force le type numérique sur les colonnes mesures/dims numériques d'une table connue.

    Évite qu'une édition dans l'app ou un import stocke un nombre en TEXT (dtype
    object), ce qui ferait planter les calculs en aval (ex. np.divide sur object).
    Sans effet sur une table inconnue du registre.
    """
    spec = TABLES.get(name)
    if spec is None:
        return df
    numeric = {*spec.measures, *(c for c in spec.dims if c not in TEXT_COLUMNS)}
    out = df.copy()
    for c in numeric & set(out.columns):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path))


def list_tables(db_path: Path | str = DB_PATH) -> list[str]:
    with connect(db_path) as con:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return sorted(r[0] for r in rows)


# NOTE : name est interpolé dans le SQL sans validation préalable. Tous les appelants
# actuels passent par le registre TABLES (chemin sûr), mais un `assert name in TABLES`
# en tête de ces deux fonctions serait une garde peu coûteuse si de nouveaux appelants
# s'ajoutaient en dehors du registre.
def read_table(name: str, db_path: Path | str = DB_PATH) -> pd.DataFrame:
    with connect(db_path) as con:
        df = pd.read_sql(f'SELECT * FROM "{name}"', con)
    return coerce_types(name, df)  # répare une base où un nombre serait stocké en TEXT


def write_table(name: str, df: pd.DataFrame, db_path: Path | str = DB_PATH) -> None:
    """Remplace intégralement une table (en normalisant les types numériques)."""
    with connect(db_path) as con:
        coerce_types(name, df).to_sql(name, con, if_exists="replace", index=False)


def seed(tables: dict[str, pd.DataFrame], db_path: Path | str = DB_PATH) -> None:
    for name, df in tables.items():
        write_table(name, df, db_path)


def db_exists(db_path: Path | str = DB_PATH) -> bool:
    return Path(db_path).exists() and "region" in list_tables(db_path)
