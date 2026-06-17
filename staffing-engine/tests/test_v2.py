"""Validation du moteur V2 (db, erlang, model, optimizer).

pytest -q   (depuis staffing-engine/). Les données sont amorcées dans une base
SQLite temporaire isolée (aucun effet sur data/staffing.db).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import seed_db  # noqa: E402
from staffing import db, erlang, model, optimizer  # noqa: E402

MONTH = "2026-07"


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    dbp = tmp_path_factory.mktemp("data") / "staffing_test.db"
    seed_db.main(dbp)
    demand = model.build_forecast_demand(MONTH, dbp)
    required = model.required_by_level(demand, dbp)
    optimizer.optimize_allocation(MONTH, dbp, time_limit=15)
    cov = optimizer.build_coverage(MONTH, dbp)
    return SimpleNamespace(dbp=dbp, demand=demand, required=required, cov=cov)


# --- Erlang C ----------------------------------------------------------------
def test_erlang_monotonic_and_meets_sla():
    n = erlang.agents_required(120, 300, 900, 0.95, 120, 0.92)
    assert n >= 1
    # le SLA est tenu pour n, pas pour n-1
    assert erlang.service_level(n, 120 * 300 / 900, 300, 120) >= 0.95
    # plus de contacts -> au moins autant d'agents
    assert erlang.agents_required(240, 300, 900, 0.95, 120, 0.92) >= n


def test_erlang_zero():
    assert erlang.agents_required(0, 300, 900, 0.95, 120, 0.92) == 0


# --- conservation de la demande ----------------------------------------------
def test_volume_conservation(ctx):
    t = model.load_tables(ctx.dbp)
    paxf = t["pax_forecast"].query("month == @MONTH")
    crf = t["contact_rate_forecast"].query("month == @MONTH")
    base = paxf.merge(crf, on=["month", "region_id", "supply_id"])
    expected = (base["pax"] * base["contact_rate"]).sum()
    assert ctx.demand["contacts"].sum() == pytest.approx(expected, rel=1e-6)


# --- ETP requis --------------------------------------------------------------
def test_required_gross_up(ctx):
    r = ctx.required
    assert (r["required_fte"] >= r["agents_online"] - 1e-9).all()  # /(1-shrink) >= 1
    assert (r["agents_online"] >= 0).all()


# --- optimiseur : faisabilité & respect des contraintes ----------------------
def test_optimizer_covers_demand(ctx):
    m = ctx.cov["matching"]
    l1 = m[(m["level"] == 1) & (m["required_fte"] > 1e-9)]
    covered = (l1["coverage_ratio"] >= 1 - 1e-6).mean()
    assert covered >= 0.95  # la quasi-totalité des buckets L1 avec demande est couverte


def test_allocation_respects_availability(ctx):
    alloc = db.read_table("allocation", ctx.dbp)
    teams = db.read_table("team", ctx.dbp)
    avail = db.read_table("team_availability", ctx.dbp)
    tz = dict(zip(teams["team_id"], teams["timezone"]))
    rep = optimizer._rep_dates(MONTH)
    for row in alloc.itertuples():
        slots = optimizer._avail_utc_slots(row.team_id, row.dow, rep[row.dow], tz[row.team_id], avail)
        assert row.slot_utc in slots, f"{row.team_id} alloué hors dispo (dow {row.dow}, slot {row.slot_utc})"


def test_level_sourcing_in_supply(ctx):
    st = ctx.cov["supply_team"].merge(db.read_table("team", ctx.dbp)[["team_id", "sourcing", "level"]],
                                      on=["team_id", "level"])
    assert (st.loc[st["level"] == 1, "sourcing"] == "external").all()
    assert (st.loc[st["level"] == 2, "sourcing"] == "internal").all()


# --- réel vs forecast --------------------------------------------------------
def test_actuals_decomposition(ctx):
    av = model.actuals_vs_forecast(ctx.dbp)
    real = av.dropna(subset=["pax_real"])
    # cr_real = tâches / PAX réels
    np.testing.assert_allclose(real["cr_real"], real["tasks_real"] / real["pax_real"], rtol=1e-6)
    # tasks_forecast = PAX forecast × contact rate forecast
    np.testing.assert_allclose(av["tasks_forecast"], av["pax_forecast"] * av["cr_forecast"], rtol=1e-6)


# --- base SQLite -------------------------------------------------------------
def test_db_roundtrip(ctx):
    before = db.read_table("param_aht", ctx.dbp)
    bumped = before.copy()
    bumped.loc[0, "aht_seconds"] = 999
    db.write_table("param_aht", bumped, ctx.dbp)
    assert db.read_table("param_aht", ctx.dbp).loc[0, "aht_seconds"] == 999
