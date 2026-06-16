"""C — Demande (charge).

Chaîne de décomposition de la demande, du mensuel au bucket de 15 min :

  C1  Volume      : contacts mensuels = passagers(mois, group) x taux_contact(tâche, group)
  C2  Profils     : mensuel -> jour  via profil de départ CONVOLUÉ avec la courbe de délai (A1)
                    jour    -> bucket via profil intraday (par jour de semaine), en heure
                    locale puis converti en UTC.
  C3  Charge      : charge brute = contacts x AHT (heures-agent, AUCUNE occupation)
                    ETP requis  = charge brute / occupation_cible / (1 - shrinkage)

Sortie : DataFrame à la maille fine (bucket_utc, level, task_type_id, group_id,
region_id, supply_id) avec les mesures contacts / workload_hours / required_fte.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .params import Params
from .referentials import Referentials
from .timespine import BUCKET_HOURS, BUCKET_MINUTES, BUCKETS_PER_DAY


# --- Profils (C2) ------------------------------------------------------------
def load_profiles(data_dir):
    """Charge les profils de répartition. Renvoie (dep_dow, intraday, lag_pmf)."""
    from .timespine import load_lag_curve

    data_dir = Path(data_dir)
    dep = pd.read_csv(data_dir / "profile_departure_dow.csv")
    dep_dow = dep.set_index("dow")["weight"].astype(float)

    intraday = pd.read_csv(data_dir / "profile_intraday.csv")
    # Normalisation défensive : somme des poids = 1 par jour de semaine.
    intraday["weight"] = intraday.groupby("dow")["weight"].transform(lambda x: x / x.sum())

    lag_pmf = load_lag_curve(data_dir / "lag_curve.csv")
    return dep_dow, intraday, lag_pmf


# --- C2 : mensuel -> jour (convolution départ x lag) -------------------------
def _daily_shape(month_dates: pd.DatetimeIndex, dep_dow: pd.Series, lag_pmf: pd.Series) -> pd.Series:
    """Forme journalière (normalisée) des ARRIVÉES dans un mois.

    Pour une date d'arrivée ``a`` : shape[a] = Σ_lag P(lag) * poids_départ(jds(a - lag)).
    Les départs `lag` jours plus tôt (de jour de semaine variable) génèrent, après
    application de la courbe de délai, les contacts qui arrivent le jour ``a``.
    """
    shape = np.zeros(len(month_dates), dtype=float)
    for lag, p in lag_pmf.items():
        dep_dates = month_dates - pd.Timedelta(days=int(lag))
        weights = dep_dow.reindex(dep_dates.dayofweek).to_numpy()
        shape += float(p) * weights
    s = pd.Series(shape, index=month_dates)
    return s / s.sum()


def _shape_table(months, dates_by_month, dep_dow, lag_pmf) -> pd.DataFrame:
    frames = []
    for month in months:
        shape = _daily_shape(dates_by_month[month], dep_dow, lag_pmf)
        frames.append(
            pd.DataFrame({"month": month, "date": shape.index, "shape_weight": shape.to_numpy()})
        )
    return pd.concat(frames, ignore_index=True)


# --- C2 : jour -> bucket (intraday + conversion fuseau -> UTC) ---------------
def _slot_to_utc(dates: pd.DatetimeIndex, tz: str) -> pd.DataFrame:
    """Table (date, slot_local, dow, bucket_utc) pour une région.

    Convertit chaque créneau local de 15 min en bucket UTC (gère le décalage et
    l'heure d'été). Les éventuels instants inexistants/ambigus (transition DST)
    sont écartés.
    """
    slots = np.arange(BUCKETS_PER_DAY)
    mi = pd.MultiIndex.from_product([pd.DatetimeIndex(dates), slots], names=["date", "slot_local"])
    df = mi.to_frame(index=False)
    local_naive = df["date"] + df["slot_local"] * pd.Timedelta(minutes=BUCKET_MINUTES)
    local_aware = local_naive.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    df["bucket_utc"] = local_aware.dt.tz_convert("UTC")
    df["dow"] = df["date"].dt.dayofweek
    return df.dropna(subset=["bucket_utc"])


def build_demand(refs: Referentials, params: Params, data_dir, spine: pd.DatetimeIndex) -> pd.DataFrame:
    """Construit la demande à la maille fine (bucket_utc x segment x group)."""
    dep_dow, intraday, lag_pmf = load_profiles(data_dir)

    # --- C1 : ancrage mensuel des contacts -----------------------------------
    groups = refs.group_monthly.copy()
    groups = groups[groups["active"].astype(bool)]
    tasks = refs.task_type[["task_type_id", "level"]]
    base = groups.merge(tasks, how="cross")
    base["contact_rate"] = params.contact_rate_with_fallback(base[["task_type_id", "group_id"]]).to_numpy()
    base["monthly_contacts"] = base["passengers"] * base["contact_rate"]

    # --- C2 : mensuel -> jour ------------------------------------------------
    spine_dates = pd.date_range(spine.min().tz_convert(None).normalize(),
                                spine.max().tz_convert(None).normalize(), freq="D")
    months = sorted(base["month"].unique())
    dates_by_month = {}
    for month in months:
        period = pd.Period(month, freq="M")
        md = pd.date_range(period.start_time, period.end_time.normalize(), freq="D")
        dates_by_month[month] = md
    shape = _shape_table(months, dates_by_month, dep_dow, lag_pmf)

    daily = base.merge(shape, on="month")
    daily["contacts"] = daily["monthly_contacts"] * daily["shape_weight"]
    daily = daily[
        ["date", "month", "group_id", "supply_id", "region_id", "task_type_id", "level", "contacts"]
    ]

    # --- C2 : jour -> bucket (par région, en heure locale -> UTC) ------------
    tz_by_region = dict(zip(refs.region["region_id"], refs.region["timezone"]))
    out_frames = []
    for region_id, region_daily in daily.groupby("region_id"):
        tz = tz_by_region[region_id]
        region_dates = pd.DatetimeIndex(sorted(region_daily["date"].unique()))
        slot = _slot_to_utc(region_dates, tz).merge(intraday, on=["dow", "slot_local"])
        exp = region_daily.merge(slot[["date", "bucket_utc", "weight"]], on="date")
        exp["contacts"] = exp["contacts"] * exp["weight"]
        agg = exp.groupby(
            ["bucket_utc", "level", "task_type_id", "group_id", "region_id", "supply_id"],
            as_index=False,
        )["contacts"].sum()
        out_frames.append(agg)

    demand = pd.concat(out_frames, ignore_index=True)

    # Restreint à l'horizon (le report de fuseau peut sortir des bornes).
    demand = demand[demand["bucket_utc"].isin(spine)]

    # --- C3 : charge brute -> ETP requis -------------------------------------
    demand["aht_seconds"] = params.aht_with_fallback(demand[["task_type_id", "group_id"]]).to_numpy()
    demand["workload_hours"] = demand["contacts"] * demand["aht_seconds"] / 3600.0

    lv = params.levels_frame().set_index("level")
    demand["target_occupancy"] = demand["level"].map(lv["target_occupancy"])
    demand["shrinkage"] = demand["level"].map(lv["shrinkage"])
    # ETP requis = charge_brute / pas_horaire / occupation_cible / (1 - shrinkage)
    demand["required_fte"] = (
        demand["workload_hours"]
        / BUCKET_HOURS
        / demand["target_occupancy"]
        / (1.0 - demand["shrinkage"])
    )

    return demand.sort_values(["bucket_utc", "level", "task_type_id", "group_id"]).reset_index(drop=True)
