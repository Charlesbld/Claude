"""Moteur V2 — demande forecast, dimensionnement Erlang C, comparaison réel/forecast.

Lit la base SQLite et produit la maille pivot (bucket_utc × level) à partir du
forecast (PAX × contact rate), puis le besoin en agents via Erlang C.

Chaîne :  PAX_forecast(mois,region,supply) × contact_rate_forecast(…,task)
          → contacts mensuels → jour (profil dow) → bucket (intraday local→UTC)
          → charge → Erlang C → ETP requis (roster = agents / (1 − shrinkage)).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import db, erlang
from .timespine import BUCKET_MINUTES, BUCKETS_PER_DAY, build_time_spine

INTERVAL_SECONDS = BUCKET_MINUTES * 60  # 900


# --- helpers temps -----------------------------------------------------------
def month_local_dates(month: str) -> pd.DatetimeIndex:
    p = pd.Period(month, freq="M")
    return pd.date_range(p.start_time, p.end_time.normalize(), freq="D")


def spine_for_month(month: str, pad_days: int = 1) -> pd.DatetimeIndex:
    p = pd.Period(month, freq="M")
    start = (p.start_time - pd.Timedelta(days=pad_days)).normalize()
    end = (p.end_time.normalize() + pd.Timedelta(days=pad_days + 1))
    return build_time_spine(start, end)


def _slot_to_utc(dates: pd.DatetimeIndex, tz: str) -> pd.DataFrame:
    slots = np.arange(BUCKETS_PER_DAY)
    df = pd.MultiIndex.from_product([pd.DatetimeIndex(dates), slots],
                                    names=["date", "slot_local"]).to_frame(index=False)
    local = df["date"] + df["slot_local"] * pd.Timedelta(minutes=BUCKET_MINUTES)
    aware = local.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    df["bucket_utc"] = aware.dt.tz_convert("UTC")
    df["dow"] = df["date"].dt.dayofweek
    return df.dropna(subset=["bucket_utc"])


# --- demande forecast --------------------------------------------------------
def load_tables(db_path=db.DB_PATH) -> dict:
    names = ["region", "supply", "task_type", "group_map", "pax_forecast",
             "contact_rate_forecast", "param_aht", "service_params",
             "profile_dow", "profile_intraday"]
    return {n: db.read_table(n, db_path) for n in names}


def build_forecast_demand(month: str, db_path=db.DB_PATH, levels=None, regions=None,
                          supplies=None) -> pd.DataFrame:
    """Demande forecast à la maille fine (bucket_utc × level × task × group)."""
    t = load_tables(db_path)
    paxf = t["pax_forecast"][t["pax_forecast"]["month"] == month]
    crf = t["contact_rate_forecast"][t["contact_rate_forecast"]["month"] == month]
    gmap = t["group_map"][(t["group_map"]["month"] == month) & (t["group_map"]["active"] == 1)]

    base = (paxf.merge(crf, on=["month", "region_id", "supply_id"])
            .merge(t["task_type"][["task_type_id", "level"]], on="task_type_id")
            .merge(gmap[["region_id", "supply_id", "group_id"]], on=["region_id", "supply_id"])
            .merge(t["param_aht"], on="task_type_id"))
    base["monthly_contacts"] = base["pax"] * base["contact_rate"]
    if levels:
        base = base[base["level"].isin(levels)]
    if regions:
        base = base[base["region_id"].isin(regions)]
    if supplies:
        base = base[base["supply_id"].isin(supplies)]
    if base.empty:
        return base.assign(bucket_utc=pd.Series(dtype="datetime64[ns, UTC]"), contacts=0.0, workload_hours=0.0)

    # mensuel -> jour (profil dow normalisé sur les dates du mois)
    dow_w = t["profile_dow"].set_index("dow")["weight"].astype(float)
    dates = month_local_dates(month)
    shape = pd.Series(dow_w.reindex(dates.dayofweek).to_numpy(), index=dates)
    shape = shape / shape.sum()
    shape_df = pd.DataFrame({"date": shape.index, "shape": shape.to_numpy()})

    # jour -> bucket par région (heure locale -> UTC)
    intraday = t["profile_intraday"].copy()
    intraday["weight"] = intraday.groupby("dow")["weight"].transform(lambda x: x / x.sum())
    tz_by_region = dict(zip(t["region"]["region_id"], t["region"]["timezone"]))

    out = []
    for region_id, grp in base.groupby("region_id"):
        slot = _slot_to_utc(dates, tz_by_region[region_id]).merge(intraday, on=["dow", "slot_local"])
        slot = slot.merge(shape_df, on="date")
        slot["w"] = slot["shape"] * slot["weight"]   # poids jour × poids intraday
        # produit cartésien (group×task de la région) × créneaux, pondéré
        g = grp[["supply_id", "group_id", "task_type_id", "level", "monthly_contacts", "aht_seconds"]]
        exp = g.merge(slot[["bucket_utc", "w"]], how="cross")
        exp["contacts"] = exp["monthly_contacts"] * exp["w"]
        exp["region_id"] = region_id
        out.append(exp.groupby(
            ["bucket_utc", "level", "task_type_id", "group_id", "region_id", "supply_id", "aht_seconds"],
            as_index=False)["contacts"].sum())
    demand = pd.concat(out, ignore_index=True)
    demand["workload_hours"] = demand["contacts"] * demand["aht_seconds"] / 3600.0
    return demand.sort_values(["bucket_utc", "level", "task_type_id", "group_id"]).reset_index(drop=True)


# --- Erlang C : ETP requis par (bucket, level, group_id) ---------------------
def required_by_group(demand: pd.DataFrame, db_path=db.DB_PATH) -> pd.DataFrame:
    """ETP requis par (bucket_utc × level × group_id) — Erlang C par groupe commercial."""
    sp = db.read_table("service_params", db_path).set_index("level")
    pooled = demand.groupby(["bucket_utc", "level", "group_id"], as_index=False).agg(
        contacts=("contacts", "sum"),
        workload_hours=("workload_hours", "sum"),
        workload_seconds=("workload_hours", lambda s: s.sum() * 3600.0),
    )
    pooled["aht_eff"] = np.where(pooled["contacts"] > 0,
                                 pooled["workload_seconds"] / pooled["contacts"], 0.0)
    frames = []
    for (level, group_id), sub in pooled.groupby(["level", "group_id"]):
        if level not in sp.index:
            continue
        p = sp.loc[level]
        sub = sub.copy()
        sub["agents_online"] = erlang.required_agents_series(
            sub["contacts"], sub["aht_eff"], float(p["sl_target"]), float(p["sl_seconds"]),
            float(p["max_occupancy"]), INTERVAL_SECONDS)
        sub["required_fte"] = sub["agents_online"] / (1.0 - float(p["shrinkage"]))
        sub["shrinkage"] = float(p["shrinkage"])
        frames.append(sub)
    if not frames:
        return pd.DataFrame(columns=["bucket_utc", "level", "group_id", "contacts",
                                     "workload_hours", "aht_eff", "agents_online",
                                     "required_fte", "shrinkage"])
    return pd.concat(frames, ignore_index=True).drop(columns="workload_seconds")


def required_by_level(demand: pd.DataFrame, db_path=db.DB_PATH) -> pd.DataFrame:
    """Backward-compat alias: pools required_by_group across groups for tests."""
    by_group = required_by_group(demand, db_path)
    if by_group.empty:
        return by_group.drop(columns=["group_id"], errors="ignore")
    # re-aggregate across groups: sum contacts/workload, average shrinkage
    sp = db.read_table("service_params", db_path).set_index("level")
    pooled = by_group.groupby(["bucket_utc", "level"], as_index=False).agg(
        contacts=("contacts", "sum"),
        workload_hours=("workload_hours", "sum"),
        workload_seconds=("workload_hours", lambda s: s.sum() * 3600.0),
        shrinkage=("shrinkage", "first"),
        aht_eff=("aht_eff", "mean"),
    )
    # re-run Erlang C on the pooled demand (cross-group pooling gives better SLA)
    frames = []
    for level, sub in pooled.groupby("level"):
        if level not in sp.index:
            continue
        p = sp.loc[level]
        sub = sub.copy()
        sub["agents_online"] = erlang.required_agents_series(
            sub["contacts"], sub["aht_eff"], float(p["sl_target"]), float(p["sl_seconds"]),
            float(p["max_occupancy"]), INTERVAL_SECONDS)
        sub["required_fte"] = sub["agents_online"] / (1.0 - float(p["shrinkage"]))
        frames.append(sub)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop(columns="workload_seconds", errors="ignore")


# --- comparaison réel vs forecast -------------------------------------------
def actuals_vs_forecast(db_path=db.DB_PATH) -> pd.DataFrame:
    """Réel vs forecast sur TOUS les mois forecastés.

    L'univers est le forecast complet (contact_rate_forecast × pax_forecast) ; le
    réel (PAX, tâches) est ajouté en jointure gauche → les mois futurs n'ont que
    le forecast (réel = NaN).
    """
    paxr = db.read_table("pax_real", db_path).rename(columns={"pax": "pax_real"})
    paxf = db.read_table("pax_forecast", db_path).rename(columns={"pax": "pax_forecast"})
    tr = db.read_table("tasks_real", db_path).rename(columns={"tasks": "tasks_real"})
    crf = db.read_table("contact_rate_forecast", db_path).rename(columns={"contact_rate": "cr_forecast"})
    gmap = db.read_table("group_map", db_path)

    df = (crf.merge(paxf, on=["month", "region_id", "supply_id"], how="left")
          .merge(gmap[["month", "region_id", "supply_id", "group_id"]],
                 on=["month", "region_id", "supply_id"], how="left")
          .merge(paxr, on=["month", "region_id", "supply_id"], how="left")
          .merge(tr, on=["month", "region_id", "supply_id", "task_type_id"], how="left"))
    df["cr_real"] = np.where(df["pax_real"].fillna(0) > 0, df["tasks_real"] / df["pax_real"], np.nan)
    df["tasks_forecast"] = df["pax_forecast"] * df["cr_forecast"]
    return df.sort_values(["month", "region_id", "supply_id", "task_type_id"]).reset_index(drop=True)
