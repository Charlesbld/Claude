"""Optimiseur de répartition + couverture (couches D' et E, V2).

Le problème : choisir combien d'agents staffer par équipe et par créneau pour
**couvrir l'ETP requis (Erlang C) au coût total minimal**, dans les fenêtres de
disponibilité des équipes. Résolu en programmation linéaire en nombres entiers
(PuLP/CBC), **une fois par jour de semaine**, puis réutilisé sur l'horizon.

Variables : x[équipe, shift] = nb d'agents sur ce shift (shift = bloc contigu de
durée fixe dans la dispo de l'équipe, cyclique sur la journée UTC de 96 buckets).
Contrainte : Σ agents·productivité ≥ ETP requis, sur chaque créneau, chaque level,
             et chaque groupe commercial.
Objectif   : Σ agents · durée · coût_horaire  (minimal).

L'allocation produite (dow × slot_utc × équipe → agents) est stockée en base et
**éditable** ; la couverture se recalcule ensuite à partir de l'allocation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pulp

from . import db
from .model import build_forecast_demand, month_local_dates, required_by_group
from .timespine import BUCKET_HOURS, BUCKETS_PER_DAY


def _hhmm_to_slot(value: str) -> int:
    h, m = str(value).split(":")
    return (int(h) * 60 + int(m)) // 15


def _rep_dates(month: str) -> dict[int, pd.Timestamp]:
    """Première date de chaque jour de semaine présent dans le mois."""
    out = {}
    for d in month_local_dates(month):
        out.setdefault(int(d.dayofweek), d)
    return out


def _avail_utc_slots(team_id, dow, rep_date, tz, availability) -> set[int]:
    """Ensemble des slots UTC (0..95) où l'équipe peut travailler ce jour-là."""
    from .model import _slot_to_utc

    wins = availability[(availability["team_id"] == team_id) & (availability["dow"] == dow)]
    if wins.empty:
        return set()
    smap = _slot_to_utc(pd.DatetimeIndex([rep_date]), tz)
    local_to_utc = {int(r.slot_local): int(r.bucket_utc.hour * 4 + r.bucket_utc.minute // 15)
                    for r in smap.itertuples()}
    slots: set[int] = set()
    for w in wins.itertuples():
        s = _hhmm_to_slot(w.start_local)
        e = _hhmm_to_slot(w.end_local)
        if e <= s:
            e += BUCKETS_PER_DAY
        for ls in range(s, e):
            u = local_to_utc.get(ls % BUCKETS_PER_DAY)
            if u is not None:
                slots.add(u)
    return slots


def _candidate_shifts(avail: set[int], shift_lengths, step: int = 4) -> list[tuple]:
    """Shifts contigus (cycliques) entièrement inclus dans la dispo.

    Les démarrages sont posés sur une grille (par défaut horaire, step=4 buckets)
    pour limiter la symétrie de l'IP — sinon CBC explose en branch & bound.
    """
    starts = sorted(avail)[::step]
    shifts = []
    for L in shift_lengths:
        for start in starts:
            cover = tuple((start + i) % BUCKETS_PER_DAY for i in range(L))
            if all(c in avail for c in cover):
                shifts.append((f"{start:02d}_{L}", L, frozenset(cover)))
    return shifts


def optimize_allocation(month: str, db_path=db.DB_PATH, percentile: float = 1.0,
                        shift_lengths=(24, 32), start_step: int = 4,
                        time_limit: int = 20, gap_rel: float = 0.02,
                        write: bool = True) -> pd.DataFrame:
    """Résout l'IP par (groupe, level, dow) et renvoie l'allocation (dow, slot_utc, team, agents)."""
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit, gapRel=gap_rel)
    demand = build_forecast_demand(month, db_path)
    required = required_by_group(demand, db_path)
    required["dow"] = required["bucket_utc"].dt.dayofweek
    required["slot_utc"] = required["bucket_utc"].dt.hour * 4 + required["bucket_utc"].dt.minute // 15
    rep = (required.groupby(["dow", "slot_utc", "level", "group_id"])["required_fte"]
           .quantile(percentile).reset_index())

    teams = db.read_table("team", db_path)
    availability = db.read_table("team_availability", db_path)
    team_group = db.read_table("team_group", db_path)
    prod = dict(zip(teams["team_id"], teams["productivity"]))
    cost_h = dict(zip(teams["team_id"], teams["hourly_cost"]))
    tlevel = dict(zip(teams["team_id"], teams["level"]))
    tz = dict(zip(teams["team_id"], teams["timezone"]))
    cap = dict(zip(teams["team_id"], teams["max_agents"]))
    rep_dates = _rep_dates(month)

    # teams per (group_id, level)
    def teams_for(group_id, level):
        tids = team_group[team_group["group_id"] == group_id]["team_id"].tolist()
        return [t for t in tids if tlevel.get(t) == level]

    alloc_rows = []
    for dow, rep_date in rep_dates.items():
        groups = rep[rep["dow"] == dow]["group_id"].unique()
        # candidate shifts per team (computed once per dow)
        cand = {}
        for team in teams["team_id"]:
            avail = _avail_utc_slots(team, dow, rep_date, tz[team], availability)
            cand[team] = _candidate_shifts(avail, shift_lengths, start_step)
        cover_idx = {team: {slot: [] for slot in range(BUCKETS_PER_DAY)} for team in cand}
        for team, shifts in cand.items():
            for sid, L, cover in shifts:
                for slot in cover:
                    cover_idx[team][slot].append((sid, L))

        # --- Build a single IP per dow with shared variables across all groups ---
        # Key insight: x[(team, sid)] represents total physical agents from a team
        # on a shift. A team can serve multiple groups simultaneously (their capacity
        # is split / shared across groups in expand_allocation + build_coverage).
        #
        # The old per-(group,level) IP allowed the same team to have cap[team] agents
        # allocated to group A AND cap[team] agents to group B, violating physical limits.
        #
        # Fix: one global IP per dow where x[(team, sid)] is shared. The total agents
        # per (team, slot) = sum of x[(team, sid)] for all shifts covering that slot.
        # This sum must be <= cap[team] (the physical headcount limit).
        #
        # Coverage constraints: for each (group, level, slot), require that the teams
        # assigned to that group collectively cover the ETP requirement.

        # Collect all (group_id, level) combos active this dow
        gl_pairs = []
        for group_id in groups:
            levels = rep[(rep["dow"] == dow) & (rep["group_id"] == group_id)]["level"].unique()
            for level in levels:
                team_ids = teams_for(group_id, level)
                if team_ids:
                    gl_pairs.append((group_id, int(level), team_ids))

        if not gl_pairs:
            continue

        # Collect all teams active this dow (across all groups)
        all_active_teams: set[str] = set()
        for _gid, _lvl, tids in gl_pairs:
            all_active_teams.update(tids)

        prob = pulp.LpProblem(f"alloc_{dow}", pulp.LpMinimize)
        # x[(team, sid)] = total physical agents on this shift (shared across groups)
        x: dict[tuple, pulp.LpVariable] = {}
        for team in all_active_teams:
            ub = int(cap[team]) if pd.notna(cap[team]) else None
            for sid, L, _cover in cand[team]:
                x[(team, sid)] = pulp.LpVariable(
                    f"x_{team}_{sid}", lowBound=0, upBound=ub, cat="Integer")

        if not x:
            continue

        # Objective: minimize total cost
        prob += pulp.lpSum(
            var * L * BUCKET_HOURS * cost_h[team]
            for (team, sid), var in x.items()
            for (_s, L, _c) in cand[team] if _s == sid
        )

        # Coverage constraints: per (group, level, slot)
        # A team's effective contribution to a group at a slot =
        #   agents_at_slot * productivity (capacity shared across groups).
        # Since expand_allocation merges with team_group (one row per group),
        # the coverage is computed at the group level using the shared agents.
        for group_id, level, team_ids in gl_pairs:
            req_d = {int(r.slot_utc): r.required_fte
                     for r in rep[(rep["dow"] == dow) & (rep["group_id"] == group_id)
                                  & (rep["level"] == level)].itertuples()}
            for slot in range(BUCKETS_PER_DAY):
                need = req_d.get(slot, 0.0)
                if need > 0:
                    terms = [x[(team, sid)] * prod[team]
                             for team in team_ids
                             for (sid, _L) in cover_idx[team][slot]
                             if (team, sid) in x]
                    if terms:
                        prob += pulp.lpSum(terms) >= need

        # Global per-slot capacity constraints: for each (team, slot),
        # sum of agents across all shifts covering that slot <= cap[team].
        # This is the cross-group constraint that was missing in the original code.
        for team in all_active_teams:
            if not pd.notna(cap[team]):
                continue
            cap_val = int(cap[team])
            for slot in range(BUCKETS_PER_DAY):
                terms = [x[(team, sid)]
                         for (sid, _L) in cover_idx[team][slot]
                         if (team, sid) in x]
                if terms:
                    # This replaces the per-group constraint with one global constraint
                    prob += pulp.lpSum(terms) <= cap_val

        prob.solve(solver)

        # Extract allocation: agents per (team, slot) = sum of x[(team, sid)]
        # for all shifts covering that slot.
        # When the LP is infeasible (demand > capacity), CBC may return values that
        # violate the cap constraints. We enforce the cap explicitly after solving.
        for team in all_active_teams:
            cap_val = int(cap[team]) if pd.notna(cap[team]) else None
            for slot in range(BUCKETS_PER_DAY):
                agents = sum((x[(team, sid)].value() or 0)
                             for (sid, _L) in cover_idx[team][slot]
                             if (team, sid) in x)
                if cap_val is not None:
                    agents = min(agents, cap_val)
                rounded = int(round(agents))
                if rounded > 0:
                    alloc_rows.append({"dow": dow, "slot_utc": slot,
                                       "team_id": team, "agents": rounded})

    alloc = pd.DataFrame(alloc_rows, columns=["dow", "slot_utc", "team_id", "agents"])
    if write:
        db.write_table("allocation", alloc, db_path)
    return alloc


# --- Couverture à partir de l'allocation (optimisée ou éditée) ---------------
def expand_allocation(month: str, db_path=db.DB_PATH) -> pd.DataFrame:
    """Allocation (par dow/slot) déployée sur les buckets du mois, par équipe."""
    alloc = db.read_table("allocation", db_path)
    teams = db.read_table("team", db_path)
    team_group = db.read_table("team_group", db_path)
    from .model import required_by_group
    required = required_by_group(build_forecast_demand(month, db_path), db_path)
    buckets = pd.DataFrame({"bucket_utc": sorted(required["bucket_utc"].unique())})
    buckets["dow"] = buckets["bucket_utc"].dt.dayofweek
    buckets["slot_utc"] = buckets["bucket_utc"].dt.hour * 4 + buckets["bucket_utc"].dt.minute // 15
    if alloc.empty:
        return pd.DataFrame(columns=["bucket_utc", "team_id", "group_id", "level",
                                     "agents", "effective_capacity", "cost"])
    sup = (buckets.merge(alloc, on=["dow", "slot_utc"])
           .merge(teams[["team_id", "level", "productivity", "hourly_cost"]], on="team_id")
           .merge(team_group, on="team_id"))
    sup["effective_capacity"] = sup["agents"] * sup["productivity"]
    sup["cost"] = sup["agents"] * BUCKET_HOURS * sup["hourly_cost"]
    return sup[["bucket_utc", "team_id", "group_id", "level", "agents",
                "effective_capacity", "cost"]]


def build_coverage(month: str, db_path=db.DB_PATH) -> dict:
    """Rapproche ETP requis (Erlang C) et capacité allouée. Renvoie matching + supply/équipe."""
    demand = build_forecast_demand(month, db_path)
    from .model import required_by_group
    required = required_by_group(demand, db_path)
    supply_team = expand_allocation(month, db_path)

    sup = (supply_team.groupby(["bucket_utc", "level", "group_id"], as_index=False)
           [["effective_capacity", "agents", "cost"]].sum()
           if not supply_team.empty else
           pd.DataFrame(columns=["bucket_utc", "level", "group_id",
                                  "effective_capacity", "agents", "cost"]))
    m = required.merge(sup, on=["bucket_utc", "level", "group_id"], how="left")
    for c in ["effective_capacity", "agents", "cost"]:
        m[c] = m[c].fillna(0.0)
    m["gap_fte"] = m["effective_capacity"] - m["required_fte"]
    m["understaffed"] = m["gap_fte"] < -1e-9
    req = m["required_fte"].to_numpy(dtype=float)
    capv = m["effective_capacity"].to_numpy(dtype=float)
    m["coverage_ratio"] = np.divide(capv, req, out=np.full_like(capv, np.nan), where=req > 1e-9)
    avail_prod = capv * BUCKET_HOURS * (1.0 - m["shrinkage"].to_numpy(dtype=float))
    wl = m["workload_hours"].to_numpy(dtype=float)
    occ = np.divide(wl, avail_prod, out=np.zeros_like(wl), where=avail_prod > 1e-9)
    occ = np.where((avail_prod <= 1e-9) & (wl > 1e-9), np.inf, occ)
    m["real_occupancy"] = occ
    return {"matching": m.sort_values(["level", "group_id", "bucket_utc"]).reset_index(drop=True),
            "supply_team": supply_team, "demand": demand}
