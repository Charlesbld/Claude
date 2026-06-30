"""A1 — Calendrier / time spine.

Colonne vertébrale temporelle du modèle :
  * un index UTC au pas de 15 min (le "time spine") ;
  * des calendriers PAR PAYS (jours ouvrés / week-ends / fériés).

Tout est stocké en UTC. `BUSINESS_TZ` (Europe/Paris) ne sert qu'à l'affichage.
"""
from __future__ import annotations

import pandas as pd

# --- Constantes de maille ----------------------------------------------------
BUCKET_MINUTES = 15
BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES  # 96
BUCKET_HOURS = BUCKET_MINUTES / 60.0         # 0.25 h
FREQ = f"{BUCKET_MINUTES}min"
BUSINESS_TZ = "Europe/Paris"


def build_time_spine(start, end, tz: str = "UTC") -> pd.DatetimeIndex:
    """Index 15 min, tz-aware, sur l'intervalle semi-ouvert [start, end).

    >>> idx = build_time_spine("2026-06-16", "2026-06-17")
    >>> len(idx)
    96
    """
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize(tz)
    if end.tzinfo is None:
        end = end.tz_localize(tz)
    return pd.date_range(start, end, freq=FREQ, inclusive="left", name="bucket_utc")


def country_calendar(country_code: str, start, end) -> pd.DataFrame:
    """Calendrier journalier d'un pays : drapeaux week-end / férié / jour ouvré.

    Renvoie une ligne par date avec : country_code, date, dow, is_weekend,
    is_holiday, is_business_day.
    """
    import holidays as holidays_lib

    dates = pd.date_range(
        pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D"
    )
    years = range(dates[0].year, dates[-1].year + 1)
    try:
        hol = holidays_lib.country_holidays(country_code, years=years)
        hol_dates = pd.to_datetime(list(hol.keys())) if len(hol) else pd.DatetimeIndex([])
    except (NotImplementedError, KeyError):
        hol_dates = pd.DatetimeIndex([])

    df = pd.DataFrame({"date": dates})
    df["country_code"] = country_code
    df["dow"] = df["date"].dt.dayofweek
    df["is_weekend"] = df["dow"] >= 5
    df["is_holiday"] = df["date"].isin(hol_dates)
    df["is_business_day"] = ~(df["is_weekend"] | df["is_holiday"])
    return df


def build_calendars(regions: pd.DataFrame, start, end) -> pd.DataFrame:
    """Concatène les calendriers de tous les pays présents dans `regions`.

    `regions` doit contenir les colonnes region_id et country_code.
    """
    frames = []
    for country in sorted(regions["country_code"].unique()):
        frames.append(country_calendar(country, start, end))
    return pd.concat(frames, ignore_index=True)
