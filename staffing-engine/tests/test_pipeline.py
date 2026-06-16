"""Validation des calculs du moteur (couches A->E).

Lancer : pytest -q   (depuis staffing-engine/)
Les tests régénèrent les données synthétiques (déterministes) puis vérifient
les invariants structurants : conservation du volume, identité d'occupation,
unicité de la maille, contrainte level/sourcing, normalisation des profils,
détection des trous.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import generate_synthetic_data as gen  # noqa: E402
from staffing import demand as demand_mod  # noqa: E402
from staffing import matching as matching_mod  # noqa: E402
from staffing import reporting  # noqa: E402
from staffing import supply as supply_mod  # noqa: E402
from staffing.params import load_params  # noqa: E402
from staffing.referentials import load_referentials  # noqa: E402
from staffing.timespine import BUCKET_HOURS, BUCKETS_PER_DAY, build_time_spine, load_lag_curve  # noqa: E402

RAW = ROOT / "data" / "raw"
CONFIG = ROOT / "config.yaml"


@pytest.fixture(scope="module")
def pipe():
    gen.main()  # données synthétiques déterministes
    refs = load_referentials(RAW)
    params = load_params(RAW, CONFIG)
    h = params.config["horizon"]
    spine = build_time_spine(h["start"], h["end"])
    demand = demand_mod.build_demand(refs, params, RAW, spine)
    supply = supply_mod.build_supply(refs, RAW, spine)
    matching = matching_mod.build_matching(demand, supply, params, spine)
    return SimpleNamespace(refs=refs, params=params, spine=spine,
                           demand=demand, supply=supply, matching=matching)


# --- A1 ----------------------------------------------------------------------
def test_time_spine_grid():
    spine = build_time_spine("2026-06-16", "2026-06-17")
    assert len(spine) == BUCKETS_PER_DAY == 96
    assert str(spine.tz) == "UTC"
    assert (spine.to_series().diff().dropna() == pd.Timedelta(minutes=15)).all()


def test_lag_curve_normalized():
    s = load_lag_curve(RAW / "lag_curve.csv")
    assert s.sum() == pytest.approx(1.0)


# --- C : conservation du volume ---------------------------------------------
def test_volume_conservation(pipe):
    base = pipe.refs.group_monthly[pipe.refs.group_monthly["active"].astype(bool)].merge(
        pipe.refs.task_type[["task_type_id", "level"]], how="cross"
    )
    base["rate"] = pipe.params.contact_rate_with_fallback(base[["task_type_id", "group_id"]]).to_numpy()
    expected = (base["passengers"] * base["rate"]).sum()
    assert pipe.demand["contacts"].sum() == pytest.approx(expected, rel=1e-6)


def test_intraday_profiles_normalized():
    _, intraday, _ = demand_mod.load_profiles(RAW)
    sums = intraday.groupby("dow")["weight"].sum()
    assert np.allclose(sums.to_numpy(), 1.0)


# --- E : identité d'occupation ----------------------------------------------
def test_real_occupancy_equals_target_when_balanced(pipe):
    """Si capacité == ETP requis, l'occupation réelle doit égaler la cible."""
    params = pipe.params
    target = params.level_cfg(1)["target_occupancy"]
    shrink = params.level_cfg(1)["shrinkage"]
    t0 = pipe.spine[100]
    workload = 5.0  # heures de charge sur le bucket
    required = workload / BUCKET_HOURS / target / (1.0 - shrink)

    demand = pd.DataFrame([{
        "bucket_utc": t0, "level": 1, "task_type_id": "GEN_INQUIRY", "group_id": "AIR_FR",
        "region_id": "FR", "supply_id": "AIR", "contacts": 1.0,
        "workload_hours": workload, "required_fte": required,
    }])
    supply = pd.DataFrame([{
        "bucket_utc": t0, "level": 1, "team_id": "EXT_CASA", "sourcing": "external",
        "scheduled_headcount": required, "effective_capacity": required, "cost": 1.0,
    }])
    m = matching_mod.build_matching(demand, supply, params, pd.DatetimeIndex([t0], name="bucket_utc"))
    row = m.iloc[0]
    assert row["real_occupancy"] == pytest.approx(target, rel=1e-9)
    assert row["coverage_ratio"] == pytest.approx(1.0, rel=1e-9)
    assert abs(row["gap_fte"]) < 1e-9


# --- maille & contraintes ----------------------------------------------------
def test_grain_uniqueness(pipe):
    assert pipe.matching.duplicated(["bucket_utc", "level"]).sum() == 0
    assert pipe.supply.duplicated(["bucket_utc", "team_id"]).sum() == 0
    assert pipe.demand.duplicated(
        ["bucket_utc", "level", "task_type_id", "group_id"]
    ).sum() == 0


def test_level_sourcing_constraint(pipe):
    sup = pipe.supply
    assert (sup.loc[sup["level"] == 1, "sourcing"] == "external").all()
    assert (sup.loc[sup["level"] == 2, "sourcing"] == "internal").all()


def test_no_negative_measures(pipe):
    for col in ["contacts", "workload_hours", "required_fte"]:
        assert (pipe.demand[col] >= -1e-9).all()
    for col in ["scheduled_headcount", "effective_capacity", "cost"]:
        assert (pipe.supply[col] >= -1e-9).all()


# --- F3 : détection des trous -----------------------------------------------
def test_gap_intervals_detects_known_run(pipe):
    spine = pipe.spine[:10]
    # 4 buckets contigus en sous-staffing (requis 10, capacité 6).
    rows = []
    for i, b in enumerate(spine):
        understaffed = 2 <= i <= 5
        rows.append({
            "bucket_utc": b, "level": 1, "required_fte": 10.0,
            "effective_capacity": 6.0 if understaffed else 12.0,
            "gap_fte": (6.0 if understaffed else 12.0) - 10.0,
            "understaffed": understaffed,
        })
    m = pd.DataFrame(rows)
    intervals = reporting.gap_intervals(m, top=10)
    assert len(intervals) == 1
    iv = intervals.iloc[0]
    assert iv["n_buckets"] == 4
    assert iv["max_deficit_fte"] == pytest.approx(4.0)
    assert iv["total_deficit_fte_hours"] == pytest.approx(4 * 4.0 * BUCKET_HOURS)
