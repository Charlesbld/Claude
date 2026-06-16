#!/usr/bin/env python3
"""Exécute la chaîne complète A -> F et écrit les sorties dans data/processed/.

Construit toujours la demande et l'offre sur l'horizon complet (UTC) — ce qui
garantit une conversion de fuseau correcte — puis applique le PÉRIMÈTRE du run
(levels / régions / dates) en filtrage de sortie.

Exemples :
  python scripts/run_pipeline.py                      # run complet (A->F, tous levels)
  python scripts/run_pipeline.py --mvp                # MVP : Level 1, une journée type
  python scripts/run_pipeline.py --levels 1 --regions FR --start 2026-06-16 --end 2026-06-17
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from staffing import demand as demand_mod  # noqa: E402
from staffing import matching as matching_mod  # noqa: E402
from staffing import reporting  # noqa: E402
from staffing import supply as supply_mod  # noqa: E402
from staffing.params import load_params  # noqa: E402
from staffing.referentials import load_referentials  # noqa: E402
from staffing.timespine import build_time_spine  # noqa: E402

RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Moteur de staffing — pipeline A->F")
    p.add_argument("--levels", nargs="*", type=int, help="Levels à conserver (ex. 1 2)")
    p.add_argument("--regions", nargs="*", help="Régions à conserver côté demande (ex. FR ES)")
    p.add_argument("--start", help="Borne basse UTC du périmètre (YYYY-MM-DD)")
    p.add_argument("--end", help="Borne haute UTC du périmètre (exclue, YYYY-MM-DD)")
    p.add_argument("--mvp", action="store_true",
                   help="Raccourci : Level 1, journée type (mardi 2026-06-16)")
    return p.parse_args()


def apply_scope(demand, supply, matching, *, levels, regions, start, end):
    if levels:
        demand = demand[demand["level"].isin(levels)]
        supply = supply[supply["level"].isin(levels)]
        matching = matching[matching["level"].isin(levels)]
    if regions:
        demand = demand[demand["region_id"].isin(regions)]
    if start:
        ts = pd.Timestamp(start, tz="UTC")
        demand = demand[demand["bucket_utc"] >= ts]
        supply = supply[supply["bucket_utc"] >= ts]
        matching = matching[matching["bucket_utc"] >= ts]
    if end:
        ts = pd.Timestamp(end, tz="UTC")
        demand = demand[demand["bucket_utc"] < ts]
        supply = supply[supply["bucket_utc"] < ts]
        matching = matching[matching["bucket_utc"] < ts]
    return demand.reset_index(drop=True), supply.reset_index(drop=True), matching.reset_index(drop=True)


def main() -> None:
    args = parse_args()
    PROCESSED.mkdir(parents=True, exist_ok=True)

    # --- A / B : référentiels et paramètres ----------------------------------
    refs = load_referentials(RAW)
    params = load_params(RAW, ROOT / "config.yaml")
    h = params.config["horizon"]
    spine = build_time_spine(h["start"], h["end"])
    print(f"Time spine : {len(spine)} buckets de 15 min, {spine.min()} -> {spine.max()} (UTC)")

    # --- C / D : demande et offre (horizon complet) --------------------------
    demand = demand_mod.build_demand(refs, params, RAW, spine)
    supply = supply_mod.build_supply(refs, RAW, spine)

    # --- Validation 1 : conservation du volume -------------------------------
    groups = refs.group_monthly[refs.group_monthly["active"].astype(bool)]
    base = groups.merge(refs.task_type[["task_type_id", "level"]], how="cross")
    base["rate"] = params.contact_rate_with_fallback(base[["task_type_id", "group_id"]]).to_numpy()
    expected = (base["passengers"] * base["rate"]).sum()
    got = demand["contacts"].sum()
    print(f"\n[Validation] Conservation du volume : attendu={expected:,.0f} contacts | "
          f"obtenu={got:,.0f} ({100 * got / expected:.3f} %)")

    # --- Validation 2 : contrainte level/sourcing ----------------------------
    bad = supply.loc[((supply["level"] == 1) & (supply["sourcing"] != "external"))
                     | ((supply["level"] == 2) & (supply["sourcing"] != "internal"))]
    print(f"[Validation] Contrainte L1<->externe / L2<->interne : "
          f"{'OK' if bad.empty else 'VIOLÉE'}")

    # --- E : rapprochement ---------------------------------------------------
    matching = matching_mod.build_matching(demand, supply, params, spine)
    dup = matching.duplicated(["bucket_utc", "level"]).sum()
    print(f"[Validation] Unicité de la maille (bucket_utc, level) : "
          f"{'OK' if dup == 0 else f'{dup} doublons'}")

    # --- Périmètre du run ----------------------------------------------------
    scope = dict(params.config.get("scope") or {})
    if args.mvp:
        scope.update({"levels": [1], "start": "2026-06-16", "end": "2026-06-17"})
    if args.levels:
        scope["levels"] = args.levels
    if args.regions:
        scope["regions"] = args.regions
    if args.start:
        scope["start"] = args.start
    if args.end:
        scope["end"] = args.end

    demand, supply, matching = apply_scope(
        demand, supply, matching,
        levels=scope.get("levels"), regions=scope.get("regions"),
        start=scope.get("start"), end=scope.get("end"),
    )
    print(f"\nPérimètre du run : {scope}")

    # --- F : restitution (KPI de tête) ---------------------------------------
    kpis = reporting.summary_kpis(matching)
    with pd.option_context("display.float_format", lambda v: f"{v:,.2f}"):
        print("\n[Synthèse par level]")
        print(kpis.to_string(index=False))
        gaps = reporting.gap_intervals(matching, top=5)
        if not gaps.empty:
            print("\n[Top 5 des trous de couverture]")
            print(gaps.to_string(index=False))

    # --- Écriture des sorties ------------------------------------------------
    demand.to_parquet(PROCESSED / "demand.parquet", index=False)
    supply.to_parquet(PROCESSED / "supply.parquet", index=False)
    matching.to_parquet(PROCESSED / "matching.parquet", index=False)
    meta = {"scope": scope, "horizon": h, "n_buckets": len(spine),
            "generated_at": pd.Timestamp.now("UTC").isoformat()}
    (PROCESSED / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    print(f"\n[OK] Sorties écrites dans {PROCESSED} (demand / supply / matching .parquet + run_meta.json)")


if __name__ == "__main__":
    main()
