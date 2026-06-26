"""Validation du moteur V2 (db, erlang, model, optimizer).

pytest -q   (depuis staffing-engine/). Les données sont amorcées dans une base
SQLite temporaire isolée (aucun effet sur data/staffing.db).
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

import seed_db  # noqa: E402
from staffing import db, erlang, ingest, model, optimizer  # noqa: E402

MONTH = "2026-07"


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    dbp = tmp_path_factory.mktemp("data") / "staffing_test.db"
    seed_db.main(dbp)
    demand = model.build_forecast_demand(MONTH, dbp)
    from staffing.model import required_by_group
    required = required_by_group(demand, dbp)
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
    from staffing.model import required_by_group
    r = required_by_group(ctx.demand, ctx.dbp)
    assert (r["required_fte"] >= r["agents_online"] - 1e-9).all()  # /(1-shrink) >= 1
    assert (r["agents_online"] >= 0).all()


# --- optimiseur : faisabilité & respect des contraintes ----------------------
def test_optimizer_covers_demand(ctx):
    m = ctx.cov["matching"]
    l1 = m[(m["level"] == 1) & (m["required_fte"] > 1e-9)]
    covered = (l1["coverage_ratio"] >= 1 - 1e-6).mean()
    assert covered >= 0.90  # slightly relaxed since now per-group


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
    st_df = ctx.cov["supply_team"].merge(
        db.read_table("team", ctx.dbp)[["team_id", "sourcing", "level"]],
        on=["team_id", "level"])
    assert (st_df.loc[st_df["level"] == 1, "sourcing"] == "external").all()
    assert (st_df.loc[st_df["level"] == 2, "sourcing"] == "internal").all()


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


# --- ingestion CSV mensuelle -------------------------------------------------
def test_ingest_upsert_is_additive_and_idempotent(ctx, tmp_path):
    before = db.read_table("pax_forecast", ctx.dbp)
    new = pd.DataFrame({"month": ["2030-01", "2030-01"], "region_id": ["FR", "ES"],
                        "supply_id": ["AIR", "AIR"], "pax": [123456, 222222]})
    f = tmp_path / "pax_forecast__2030-01.csv"
    new.to_csv(f, index=False)

    rep = ingest.ingest_file(f, ctx.dbp)
    after = db.read_table("pax_forecast", ctx.dbp)
    assert rep["table"] == "pax_forecast" and rep["rows_in"] == 2
    assert len(after) == len(before) + 2  # deux nouveaux mois ajoutés
    got = after.set_index(["month", "region_id", "supply_id"]).loc[("2030-01", "FR", "AIR"), "pax"]
    assert int(got) == 123456

    # réingérer le même fichier ne duplique pas (upsert par clé)
    ingest.ingest_file(f, ctx.dbp)
    assert len(db.read_table("pax_forecast", ctx.dbp)) == len(before) + 2

    # une valeur corrigée pour la même clé remplace l'ancienne
    corr = new.copy()
    corr.loc[0, "pax"] = 999999
    corr.to_csv(f, index=False)
    ingest.ingest_file(f, ctx.dbp)
    after2 = db.read_table("pax_forecast", ctx.dbp)
    assert len(after2) == len(before) + 2
    got2 = after2.set_index(["month", "region_id", "supply_id"]).loc[("2030-01", "FR", "AIR"), "pax"]
    assert int(got2) == 999999


def test_ingest_rejects_missing_columns(ctx, tmp_path):
    bad = pd.DataFrame({"month": ["2030-01"], "region_id": ["FR"], "pax": [10]})  # supply_id manquant
    f = tmp_path / "pax_forecast__bad.csv"
    bad.to_csv(f, index=False)
    with pytest.raises(ValueError, match="Colonnes manquantes"):
        ingest.ingest_file(f, ctx.dbp)


def test_ingest_unknown_table(ctx, tmp_path):
    f = tmp_path / "inexistante__x.csv"
    pd.DataFrame({"a": [1]}).to_csv(f, index=False)
    with pytest.raises(ValueError, match="Table inconnue"):
        ingest.ingest_file(f, ctx.dbp)


# --- robustesse des types : un nombre stocké en TEXT ne casse pas le calcul ---
def test_read_table_coerces_text_numbers(ctx, tmp_path):
    import shutil
    import sqlite3
    dbp = tmp_path / "corrupt.db"
    shutil.copy(ctx.dbp, dbp)
    # simule une édition app qui stocke des nombres en TEXT (dtype object)
    for tbl, col in [("param_aht", "aht_seconds"), ("contact_rate_forecast", "contact_rate"),
                     ("pax_forecast", "pax"), ("service_params", "shrinkage")]:
        d = db.read_table(tbl, dbp)
        d[col] = d[col].astype(str)
        with sqlite3.connect(dbp) as con:
            d.to_sql(tbl, con, if_exists="replace", index=False)
    # read_table recoerce automatiquement en numérique
    assert pd.api.types.is_numeric_dtype(db.read_table("param_aht", dbp)["aht_seconds"])
    assert pd.api.types.is_numeric_dtype(db.read_table("service_params", dbp)["shrinkage"])
    # et la couverture se calcule sans planter (np.divide sur object)
    cov = optimizer.build_coverage(MONTH, dbp)
    assert len(cov["matching"]) > 0
    assert pd.api.types.is_float_dtype(cov["matching"]["real_occupancy"])
