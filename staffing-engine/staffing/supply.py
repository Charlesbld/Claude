"""D — Offre (capacité).

  D1  Catalogue de shifts / disponibilités : créneaux de chaque équipe (heure
      LOCALE) convertis en UTC au pas de 15 min (gère décalage + heure d'été,
      shifts traversant minuit).
  D2  Capacité = effectif x productivité x dispo -> capacité dispo par bucket x
      level x équipe.

Conventions de mesure (cohérentes avec la couche C) :
  * scheduled_headcount  = bodies réellement planifiés sur le bucket (paie).
  * effective_capacity   = scheduled_headcount x productivité (ETP "efficaces",
    comparables à l'ETP requis de C). Le shrinkage n'est PAS appliqué ici : il
    est déjà porté côté demande (gross-up de l'ETP requis).
  * cost                 = scheduled_headcount x 0.25 h x coût_horaire.
"""
from __future__ import annotations

import pandas as pd

from .referentials import Referentials
from .timespine import BUCKET_HOURS, FREQ


def _parse_hhmm(value: str) -> pd.Timedelta:
    hh, mm = str(value).split(":")
    return pd.Timedelta(hours=int(hh), minutes=int(mm))


def build_supply(refs: Referentials, data_dir, spine: pd.DatetimeIndex) -> pd.DataFrame:
    """Construit la capacité à la maille fine (bucket_utc x level x équipe)."""
    from pathlib import Path

    shifts = pd.read_csv(Path(data_dir) / "shifts.csv")
    tz_by_team = dict(zip(refs.team["team_id"], refs.team["timezone"]))

    horizon_dates = pd.date_range(
        spine.min().tz_convert(None).normalize(),
        spine.max().tz_convert(None).normalize(),
        freq="D",
    )

    # --- D1 : déploiement des shifts sur le time spine UTC -------------------
    records = []
    for sh in shifts.itertuples(index=False):
        tz = tz_by_team[sh.team_id]
        start_td = _parse_hhmm(sh.start_local)
        end_td = _parse_hhmm(sh.end_local)
        crosses_midnight = end_td <= start_td
        dates = horizon_dates[horizon_dates.dayofweek == int(sh.dow)]
        for d in dates:
            start_local = d + start_td
            end_local = d + end_td + (pd.Timedelta(days=1) if crosses_midnight else pd.Timedelta(0))
            start_utc = start_local.tz_localize(tz, nonexistent="shift_forward", ambiguous=False).tz_convert("UTC")
            end_utc = end_local.tz_localize(tz, nonexistent="shift_forward", ambiguous=False).tz_convert("UTC")
            buckets = pd.date_range(start_utc, end_utc, freq=FREQ, inclusive="left")
            if len(buckets):
                records.append(
                    pd.DataFrame(
                        {
                            "bucket_utc": buckets,
                            "team_id": sh.team_id,
                            "scheduled_headcount": float(sh.headcount),
                        }
                    )
                )

    if not records:
        raise ValueError("Aucun shift déployé : vérifiez shifts.csv et l'horizon.")
    raw = pd.concat(records, ignore_index=True)
    raw = raw[raw["bucket_utc"].isin(spine)]

    # Shifts superposés -> on additionne les effectifs sur le bucket.
    cap = raw.groupby(["bucket_utc", "team_id"], as_index=False)["scheduled_headcount"].sum()

    # --- D2 : capacité = effectif x productivité (x dispo, déjà dans le bucket)
    attrs = refs.team[["team_id", "level", "sourcing", "productivity", "hourly_cost"]]
    cap = cap.merge(attrs, on="team_id", how="left")
    cap["effective_capacity"] = cap["scheduled_headcount"] * cap["productivity"]
    cap["cost"] = cap["scheduled_headcount"] * BUCKET_HOURS * cap["hourly_cost"]

    return cap.sort_values(["bucket_utc", "level", "team_id"]).reset_index(drop=True)
