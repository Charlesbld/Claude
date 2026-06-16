#!/usr/bin/env python3
"""Génère des données synthétiques RÉALISTES pour tester la chaîne A->F.

Produit dans data/raw/ tous les référentiels (A2–A7), paramètres (B1, taux de
contact) et profils (C2) du modèle. Déterministe (seed dans config.yaml).

Univers généré :
  * 4 régions (FR, ES, IT, GB), 4 modes de transport (avion/train/bus/ferry) ;
  * 4 types de tâche : 2 en Level 1 (externe), 2 en Level 2 (interne) ;
  * 7 équipes (4 BPO externes "follow-the-sun" + 3 équipes internes UE) ;
  * ancrage mensuel sur juin 2026.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from staffing.params import load_config  # noqa: E402
from staffing.timespine import BUCKETS_PER_DAY  # noqa: E402

RAW = ROOT / "data" / "raw"

# --- Référentiels statiques --------------------------------------------------
REGIONS = [
    ("FR", "France", "FR", "Europe/Paris", 1.00),
    ("ES", "Espagne", "ES", "Europe/Madrid", 0.70),
    ("IT", "Italie", "IT", "Europe/Rome", 0.60),
    ("GB", "Royaume-Uni", "GB", "Europe/London", 0.50),
]
SUPPLIES = [("AIR", "Avion", 0.45), ("RAIL", "Train", 0.30), ("BUS", "Bus", 0.15), ("FERRY", "Ferry", 0.10)]
REGION_SUPPLIES = {
    "FR": ["AIR", "RAIL", "BUS", "FERRY"],
    "ES": ["AIR", "RAIL", "BUS"],
    "IT": ["AIR", "RAIL", "FERRY"],
    "GB": ["AIR", "FERRY"],
}
TASK_TYPES = [
    ("GEN_INQUIRY", "Demande générale", 1, 0.080, 300),
    ("MODERATION", "Modération", 1, 0.015, 150),
    ("COMPLEX_CASE", "Dossier complexe", 2, 0.012, 720),
    ("REFUND_CLAIM", "Réclamation / remboursement", 2, 0.008, 600),
]
TEAMS = [
    # team_id, label, level, sourcing, country, tz, headcount, productivity, hourly_cost
    ("EXT_MANILA", "BPO Manille", 1, "external", "PH", "Asia/Manila", 80, 0.92, 16.0),
    ("EXT_CASA", "BPO Casablanca", 1, "external", "MA", "Africa/Casablanca", 70, 0.95, 18.0),
    ("EXT_TANA", "BPO Antananarivo", 1, "external", "MG", "Indian/Antananarivo", 55, 0.90, 14.0),
    ("EXT_TUNIS", "BPO Tunis", 1, "external", "TN", "Africa/Tunis", 40, 0.93, 17.0),
    ("INT_PARIS", "Équipe interne Paris", 2, "internal", "FR", "Europe/Paris", 60, 1.05, 45.0),
    ("INT_BARCELONA", "Équipe interne Barcelone", 2, "internal", "ES", "Europe/Madrid", 45, 1.00, 38.0),
    ("INT_LISBON", "Équipe interne Lisbonne", 2, "internal", "PT", "Europe/Lisbon", 35, 1.00, 32.0),
]
# Catalogue de shifts : (team, [dows], start_local, end_local, headcount).
# Les week-ends L1 sont allégés ; le L2 ne couvre que les heures de bureau
# -> trous nocturnes / week-end volontaires pour exercer la couche F3.
WEEKDAYS, WEEKEND = [0, 1, 2, 3, 4], [5, 6]
ALLDAYS = WEEKDAYS + WEEKEND
SHIFTS = [
    ("EXT_MANILA", WEEKDAYS, "06:00", "14:00", 30), ("EXT_MANILA", WEEKDAYS, "14:00", "22:00", 30),
    ("EXT_MANILA", WEEKEND, "06:00", "14:00", 18), ("EXT_MANILA", WEEKEND, "14:00", "22:00", 18),
    ("EXT_CASA", WEEKDAYS, "08:00", "16:00", 28), ("EXT_CASA", WEEKDAYS, "16:00", "00:00", 28),
    ("EXT_CASA", WEEKEND, "08:00", "16:00", 16), ("EXT_CASA", WEEKEND, "16:00", "00:00", 16),
    ("EXT_TANA", WEEKDAYS, "09:00", "17:00", 22), ("EXT_TANA", WEEKDAYS, "17:00", "01:00", 22),
    ("EXT_TANA", WEEKEND, "09:00", "17:00", 13),
    ("EXT_TUNIS", WEEKDAYS, "00:00", "08:00", 18),
    ("INT_PARIS", WEEKDAYS, "09:00", "18:00", 24), ("INT_PARIS", [5], "10:00", "16:00", 8),
    ("INT_BARCELONA", WEEKDAYS, "09:00", "18:00", 20), ("INT_BARCELONA", [6], "10:00", "16:00", 7),
    ("INT_LISBON", WEEKDAYS, "08:00", "17:00", 16),
]
DEPARTURE_DOW = [0.85, 0.80, 0.85, 0.95, 1.30, 1.45, 1.15]  # lun..dim : pics fin de semaine
LAG_CURVE = {  # distribution du délai départ -> contact (jours ; négatif = pré-voyage)
    -3: 0.4, -2: 0.8, -1: 1.6, 0: 4.5, 1: 6.0, 2: 5.0, 3: 4.0, 4: 3.2, 5: 2.6,
    6: 2.1, 7: 1.8, 8: 1.4, 9: 1.1, 10: 0.9, 11: 0.7, 12: 0.55, 13: 0.45, 14: 0.35,
}


def _intraday_profile() -> pd.DataFrame:
    """Profil intraday bimodal (pic matin ~10:00 et soir ~18:30) par jour de semaine."""
    s = np.arange(BUCKETS_PER_DAY)
    rows = []
    for dow in range(7):
        weekend = dow >= 5
        mu_m, mu_e = (44, 78) if weekend else (40, 74)        # créneaux plus tardifs le week-end
        sig_m, sig_e = (10, 11) if weekend else (8, 9)
        morn = (0.5 if weekend else 0.6) * np.exp(-((s - mu_m) ** 2) / (2 * sig_m ** 2))
        eve = (0.85 if weekend else 1.0) * np.exp(-((s - mu_e) ** 2) / (2 * sig_e ** 2))
        base = 0.04 * (s >= 28) * (s <= 90)                   # plancher diurne (07:00–22:30)
        w = morn + eve + base
        w = w / w.sum()
        for slot in s:
            rows.append({"dow": dow, "slot_local": int(slot), "weight": float(w[slot])})
    return pd.DataFrame(rows)


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    cfg = load_config(ROOT / "config.yaml")
    rng = np.random.default_rng(cfg["random_seed"])
    month = cfg["horizon"]["anchor_month"]

    # A2 / A3
    pd.DataFrame([(s, lbl) for s, lbl, _ in SUPPLIES], columns=["supply_id", "supply_label"]).to_csv(
        RAW / "dim_supply.csv", index=False)
    pd.DataFrame([(r, lbl, cc, tz) for r, lbl, cc, tz, _ in REGIONS],
                 columns=["region_id", "region_label", "country_code", "timezone"]).to_csv(
        RAW / "dim_region.csv", index=False)

    # A5
    pd.DataFrame([(t, lbl, lvl) for t, lbl, lvl, _, _ in TASK_TYPES],
                 columns=["task_type_id", "task_type_label", "level"]).to_csv(
        RAW / "dim_task_type.csv", index=False)

    # A4 : group mensuel (Supply x Region) + passagers
    region_scale = {r: sc for r, _, _, _, sc in REGIONS}
    supply_share = {s: sh for s, _, sh in SUPPLIES}
    grp_rows = []
    for region, supplies in REGION_SUPPLIES.items():
        for supply in supplies:
            pax = 500_000 * region_scale[region] * supply_share[supply] * rng.uniform(0.85, 1.15)
            grp_rows.append({
                "month": month, "group_id": f"{supply}_{region}", "supply_id": supply,
                "region_id": region, "passengers": int(round(pax)), "active": 1,
            })
    groups = pd.DataFrame(grp_rows)
    groups.to_csv(RAW / "dim_group_monthly.csv", index=False)

    # A6 / A7
    pd.DataFrame(TEAMS, columns=["team_id", "team_label", "level", "sourcing", "country_code",
                                 "timezone", "headcount", "productivity", "hourly_cost"]).to_csv(
        RAW / "dim_team.csv", index=False)
    skills = []
    task_by_level = {1: ["GEN_INQUIRY", "MODERATION"], 2: ["COMPLEX_CASE", "REFUND_CLAIM"]}
    for tid, _, lvl, *_ in TEAMS:
        for task in task_by_level[lvl]:
            skills.append({"team_id": tid, "task_type_id": task, "can_handle": 1})
    pd.DataFrame(skills).to_csv(RAW / "team_skills.csv", index=False)

    # B1 (AHT) + taux de contact, par (task_type, group)
    aht_rows, rate_rows = [], []
    for task, _, _lvl, base_rate, base_aht in TASK_TYPES:
        for gid in groups["group_id"]:
            rate_rows.append({"task_type_id": task, "group_id": gid,
                              "contact_rate": round(base_rate * rng.uniform(0.85, 1.15), 5)})
            aht_rows.append({"task_type_id": task, "group_id": gid,
                             "aht_seconds": int(round(base_aht * rng.uniform(0.85, 1.15)))})
    pd.DataFrame(aht_rows).to_csv(RAW / "param_aht.csv", index=False)
    pd.DataFrame(rate_rows).to_csv(RAW / "param_contact_rate.csv", index=False)

    # D1 : shifts (déplié par jour de semaine)
    shift_rows = []
    for team, dows, start, end, hc in SHIFTS:
        for dow in dows:
            shift_rows.append({"team_id": team, "dow": dow, "start_local": start,
                               "end_local": end, "headcount": hc})
    pd.DataFrame(shift_rows).to_csv(RAW / "shifts.csv", index=False)

    # C2 : profils
    pd.DataFrame({"dow": range(7), "weight": DEPARTURE_DOW}).to_csv(
        RAW / "profile_departure_dow.csv", index=False)
    pd.DataFrame({"lag_days": list(LAG_CURVE), "weight": list(LAG_CURVE.values())}).to_csv(
        RAW / "lag_curve.csv", index=False)
    _intraday_profile().to_csv(RAW / "profile_intraday.csv", index=False)

    files = sorted(p.name for p in RAW.glob("*.csv"))
    print(f"[OK] {len(files)} fichiers générés dans {RAW} :")
    for f in files:
        print(f"   - {f}")


if __name__ == "__main__":
    main()
