#!/usr/bin/env python3
"""Amorce la base SQLite (data/staffing.db) avec des données synthétiques V2.

Modèle réel vs forecast :
  * PAX forecast sur 12 mois (2026) + PAX réels jan→juin (avec écart volontaire) ;
  * tâches réelles jan→juin (⇒ contact rate réel) vs contact rate forecast éditable ;
  * AHT par task_type, équipes sans headcount fixe + fenêtres de disponibilité.

Déterministe (seed). Lancer : python scripts/seed_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from staffing import db  # noqa: E402
from staffing.timespine import BUCKETS_PER_DAY  # noqa: E402

SEED = 42
MONTHS = [f"2026-{m:02d}" for m in range(1, 13)]
REAL_MONTHS = [f"2026-{m:02d}" for m in range(1, 7)]  # jan→juin : actuals connus

REGIONS = [("FR", "France", "FR", "Europe/Paris", 1.00),
           ("ES", "Espagne", "ES", "Europe/Madrid", 0.70),
           ("IT", "Italie", "IT", "Europe/Rome", 0.60),
           ("GB", "Royaume-Uni", "GB", "Europe/London", 0.50)]
SUPPLIES = [("AIR", "Avion", 0.45), ("RAIL", "Train", 0.30), ("BUS", "Bus", 0.15), ("FERRY", "Ferry", 0.10)]
REGION_SUPPLIES = {"FR": ["AIR", "RAIL", "BUS", "FERRY"], "ES": ["AIR", "RAIL", "BUS"],
                   "IT": ["AIR", "RAIL", "FERRY"], "GB": ["AIR", "FERRY"]}
TASK_TYPES = [("GEN_INQUIRY", "Demande générale", 1, 0.080, 300),
              ("MODERATION", "Modération", 1, 0.015, 150),
              ("COMPLEX_CASE", "Dossier complexe (escalade)", 2, 0.012, 720)]
TEAMS = [  # team_id, label, level, sourcing, country, tz, productivity, hourly_cost, max_agents
    ("EXT_MANILA", "BPO Manille", 1, "external", "PH", "Asia/Manila", 0.92, 16.0, 200),
    ("EXT_CASA", "BPO Casablanca", 1, "external", "MA", "Africa/Casablanca", 0.95, 18.0, 200),
    ("EXT_TANA", "BPO Antananarivo", 1, "external", "MG", "Indian/Antananarivo", 0.90, 14.0, 200),
    ("EXT_TUNIS", "BPO Tunis", 1, "external", "TN", "Africa/Tunis", 0.93, 17.0, 150),
    ("INT_PARIS", "Équipe interne Paris", 2, "internal", "FR", "Europe/Paris", 1.05, 45.0, 60)]
# Fenêtres de disponibilité (heure locale) — larges, l'optimiseur place les agents dedans.
AVAILABILITY = [("EXT_MANILA", "06:00", "22:00"), ("EXT_CASA", "08:00", "00:00"),
                ("EXT_TANA", "09:00", "01:00"), ("EXT_TUNIS", "00:00", "08:00"),
                ("INT_PARIS", "08:00", "20:00")]
SERVICE_PARAMS = [  # cible Erlang C : 95% en ≤120s pour L1
    {"level": 1, "sl_target": 0.95, "sl_seconds": 120, "shrinkage": 0.30, "max_occupancy": 0.92},
    {"level": 2, "sl_target": 0.90, "sl_seconds": 300, "shrinkage": 0.28, "max_occupancy": 0.88}]
MONTH_FACTOR = [0.75, 0.78, 0.88, 0.95, 1.05, 1.25, 1.45, 1.40, 1.10, 0.95, 0.85, 1.05]  # saisonnalité
REGION_REAL_BIAS = {"FR": 1.00, "ES": 1.03, "IT": 0.96, "GB": 0.82}  # GB : forecast trop haut


def _intraday() -> pd.DataFrame:
    s = np.arange(BUCKETS_PER_DAY)
    rows = []
    for dow in range(7):
        wk = dow >= 5
        mu_m, mu_e = (44, 78) if wk else (40, 74)
        sig_m, sig_e = (10, 11) if wk else (8, 9)
        w = ((0.5 if wk else 0.6) * np.exp(-((s - mu_m) ** 2) / (2 * sig_m ** 2))
             + (0.85 if wk else 1.0) * np.exp(-((s - mu_e) ** 2) / (2 * sig_e ** 2))
             + 0.04 * (s >= 28) * (s <= 90))
        w = w / w.sum()
        rows += [{"dow": dow, "slot_local": int(i), "weight": float(w[i])} for i in s]
    return pd.DataFrame(rows)


def main(db_path=None) -> None:
    db_path = db_path or db.DB_PATH
    rng = np.random.default_rng(SEED)
    region_scale = {r: sc for r, _, _, _, sc in REGIONS}
    supply_share = {s: sh for s, _, sh in SUPPLIES}
    mfac = dict(zip(MONTHS, MONTH_FACTOR))

    region = pd.DataFrame([(r, lbl, cc, tz) for r, lbl, cc, tz, _ in REGIONS],
                          columns=["region_id", "region_label", "country_code", "timezone"])
    supply = pd.DataFrame([(s, lbl) for s, lbl, _ in SUPPLIES], columns=["supply_id", "supply_label"])
    task_type = pd.DataFrame([(t, lbl, lvl) for t, lbl, lvl, _, _ in TASK_TYPES],
                             columns=["task_type_id", "task_type_label", "level"])

    # group_map : (month, region, supply) actifs -> group_id
    gm = []
    for month in MONTHS:
        for reg, sups in REGION_SUPPLIES.items():
            for sup in sups:
                gm.append({"month": month, "region_id": reg, "supply_id": sup,
                           "group_id": f"{sup}_{reg}", "active": 1})
    group_map = pd.DataFrame(gm)

    # PAX forecast (12 mois) + PAX réels (jan→juin, avec biais régional)
    paxf, paxr = [], []
    for month in MONTHS:
        for reg, sups in REGION_SUPPLIES.items():
            for sup in sups:
                base = 500_000 * region_scale[reg] * supply_share[sup] * mfac[month]
                f = base * rng.uniform(0.97, 1.03)
                paxf.append({"month": month, "region_id": reg, "supply_id": sup, "pax": int(f)})
                if month in REAL_MONTHS:
                    real = f * REGION_REAL_BIAS[reg] * rng.uniform(0.93, 1.07)
                    paxr.append({"month": month, "region_id": reg, "supply_id": sup, "pax": int(real)})
    pax_forecast = pd.DataFrame(paxf)
    pax_real = pd.DataFrame(paxr)

    # Contact rate forecast (planif., ~constant) + tâches réelles (rate réel qui dérive)
    crf, tr = [], []
    base_rate = {t: br for t, _, _, br, _ in TASK_TYPES}
    pax_real_idx = pax_real.set_index(["month", "region_id", "supply_id"])["pax"].to_dict()
    for month in MONTHS:
        m_idx = MONTHS.index(month)
        for reg, sups in REGION_SUPPLIES.items():
            for sup in sups:
                for task, _, _lvl, br, _ in TASK_TYPES:
                    rate_fc = br * rng.uniform(0.9, 1.1)  # hypothèse de prévision
                    crf.append({"month": month, "region_id": reg, "supply_id": sup,
                                "task_type_id": task, "contact_rate": round(rate_fc, 5)})
                    if month in REAL_MONTHS:
                        drift = 1.0 + (0.02 * m_idx if task == "GEN_INQUIRY" else 0.0)  # dérive produit
                        rate_real = br * drift * rng.uniform(0.92, 1.12)
                        pax = pax_real_idx[(month, reg, sup)]
                        tr.append({"month": month, "region_id": reg, "supply_id": sup,
                                   "task_type_id": task, "tasks": int(pax * rate_real)})
    contact_rate_forecast = pd.DataFrame(crf)
    tasks_real = pd.DataFrame(tr)

    param_aht = pd.DataFrame([(t, aht) for t, _, _, _, aht in TASK_TYPES],
                             columns=["task_type_id", "aht_seconds"])
    team = pd.DataFrame(TEAMS, columns=["team_id", "team_label", "level", "sourcing",
                                        "country_code", "timezone", "productivity", "hourly_cost", "max_agents"])
    avail = []
    for tid, start, end in AVAILABILITY:
        for dow in range(7):
            if tid == "INT_PARIS" and dow >= 5:  # interne : pas de week-end
                continue
            avail.append({"team_id": tid, "dow": dow, "start_local": start, "end_local": end})
    team_availability = pd.DataFrame(avail)
    profile_dow = pd.DataFrame({"dow": range(7), "weight": [0.85, 0.80, 0.85, 0.95, 1.30, 1.45, 1.15]})

    db.seed({
        "region": region, "supply": supply, "task_type": task_type, "group_map": group_map,
        "pax_real": pax_real, "pax_forecast": pax_forecast, "tasks_real": tasks_real,
        "contact_rate_forecast": contact_rate_forecast, "param_aht": param_aht,
        "service_params": pd.DataFrame(SERVICE_PARAMS), "team": team,
        "team_availability": team_availability, "profile_dow": profile_dow,
        "profile_intraday": _intraday(),
        "allocation": pd.DataFrame(columns=["dow", "slot_utc", "team_id", "agents"]),
    }, db_path)
    print(f"[OK] Base SQLite amorcée : {db_path}")
    for name in db.list_tables(db_path):
        print(f"   - {name:24s} {len(db.read_table(name, db_path)):>6d} lignes")


if __name__ == "__main__":
    main()
