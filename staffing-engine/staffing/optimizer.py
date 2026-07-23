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

import logging
import numpy as np
import pandas as pd
import pulp

logger = logging.getLogger(__name__)

from . import db
from .model import (build_forecast_demand, month_local_dates, required_by_group,
                    required_by_level, required_by_task, required_by_level_task,
                    team_is_eligible)
from .timespine import BUCKET_HOURS, BUCKETS_PER_DAY

# Levels whose teams share a mutualized queue (any agent handles any group on a slot).
# For these levels the LP has ONE coverage constraint per slot (pooled across groups)
# instead of N per-group constraints — avoids crediting the same physical agents
# to each group independently (M1 double-counting fix).
# Level 1 (external) teams are group-specific → keep per-group constraints.
MUTUALIZED_LEVELS: frozenset[int] = frozenset({2})

# Module-level warning accumulator — cleared and repopulated by each
# optimize_allocation call. Thread-safe for Streamlit's single-threaded model.
_run_warnings: list[str] = []


def get_last_run_warnings() -> list[str]:
    """Return LP non-Optimal warnings from the most recent optimize_allocation call."""
    return list(_run_warnings)


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
    """Résout l'IP par dow et renvoie l'allocation (dow, slot_utc, team, task_type, agents).

    La couverture est désormais calculée PAR TYPE DE TÂCHE (pas seulement par groupe) :
    une équipe ne peut être allouée qu'aux tâches pour lesquelles elle est éligible
    (table team_task — rétro-compatible : une équipe sans ligne y est éligible à
    toutes les tâches de ses groupes).

    Niveaux L1 (externe) : contrainte de couverture par (group, task, slot) — Erlang C par tâche.
    Niveaux L2 mutualisés (MUTUALIZED_LEVELS) : contrainte par (task, slot), poolée sur tous les
    groupes pour cette tâche → un seul calcul Erlang C par tâche, pas de double-comptage.
    """
    global _run_warnings
    _run_warnings = []

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit, gapRel=gap_rel)
    demand = build_forecast_demand(month, db_path)
    required = required_by_task(demand, db_path)        # L1 : par (groupe, tâche)
    required_pool = required_by_level_task(demand, db_path)  # L2 mutualisé : poolé par (level, tâche)
    required["dow"] = required["bucket_utc"].dt.dayofweek
    required["slot_utc"] = required["bucket_utc"].dt.hour * 4 + required["bucket_utc"].dt.minute // 15
    required_pool["dow"] = required_pool["bucket_utc"].dt.dayofweek
    required_pool["slot_utc"] = required_pool["bucket_utc"].dt.hour * 4 + required_pool["bucket_utc"].dt.minute // 15
    rep = (required[~required["level"].isin(MUTUALIZED_LEVELS)]
           .groupby(["dow", "slot_utc", "level", "group_id", "task_type_id"])["required_fte"]
           .quantile(percentile).reset_index())
    rep_pool = (required_pool[required_pool["level"].isin(MUTUALIZED_LEVELS)]
                .groupby(["dow", "slot_utc", "level", "task_type_id"])["required_fte"]
                .quantile(percentile).reset_index())

    teams = db.read_table("team", db_path)
    availability = db.read_table("team_availability", db_path)
    team_group = db.read_table("team_group", db_path)
    team_task = db.read_table("team_task", db_path)
    prod = dict(zip(teams["team_id"], teams["productivity"]))
    cost_h = dict(zip(teams["team_id"], teams["hourly_cost"]))
    tlevel = dict(zip(teams["team_id"], teams["level"]))
    tz = dict(zip(teams["team_id"], teams["timezone"]))
    cap = dict(zip(teams["team_id"], teams["max_agents"]))
    rep_dates = _rep_dates(month)

    # teams per (group_id, level), filtrées par éligibilité à task_type_id
    def teams_for(group_id, level, task_type_id):
        tids = team_group[team_group["group_id"] == group_id]["team_id"].tolist()
        return [t for t in tids if tlevel.get(t) == level
                and team_is_eligible(t, task_type_id, team_task)]

    alloc_rows = []
    for dow, rep_date in rep_dates.items():
        rep_d = rep[rep["dow"] == dow]
        rep_pool_d = rep_pool[rep_pool["dow"] == dow]
        # candidate shifts per team (computed once par dow)
        cand = {}
        for team in teams["team_id"]:
            avail = _avail_utc_slots(team, dow, rep_date, tz[team], availability)
            cand[team] = _candidate_shifts(avail, shift_lengths, start_step)
        cover_idx = {team: {slot: [] for slot in range(BUCKETS_PER_DAY)} for team in cand}
        for team, shifts in cand.items():
            for sid, L, cover in shifts:
                for slot in cover:
                    cover_idx[team][slot].append((sid, L))

        # --- One IP per dow ---
        # x[(team, sid, task_type_id)] = agents physiques sur ce shift, affectés à cette tâche.
        # Capacité globale : Σ_task Σ_shifts_couvrant_slot x[team,sid,task] <= cap[team].
        #
        # L1 : contrainte par (group, task, slot) — Erlang C par tâche.
        # L2 mutualisé (MUTUALIZED_LEVELS) : contrainte par (task, slot), poolée multi-groupe.

        # L1 (non-mutualisé) : triplets (group, task, équipes éligibles)
        gtl_triples = []
        for (group_id, task_type_id), sub in rep_d.groupby(["group_id", "task_type_id"]):
            for level in sub["level"].unique():
                team_ids = teams_for(group_id, int(level), task_type_id)
                if team_ids:
                    gtl_triples.append((group_id, task_type_id, int(level), team_ids))

        # Mutualized levels (L2) : par (level, task_type_id), équipes éligibles à cette tâche
        # parmi TOUTES celles qui servent au moins un groupe à ce level.
        mutualized_teams: dict[tuple[int, str], set[str]] = {}
        for (level, task_type_id), _sub in rep_pool_d.groupby(["level", "task_type_id"]):
            tids: set[str] = set()
            for gid in team_group["group_id"].unique():
                tids.update(teams_for(gid, int(level), task_type_id))
            if tids:
                mutualized_teams[(int(level), task_type_id)] = tids

        if not gtl_triples and not mutualized_teams:
            continue

        # All teams active this dow
        all_active_teams: set[str] = set()
        for _gid, _task, _lvl, tids in gtl_triples:
            all_active_teams.update(tids)
        for tids in mutualized_teams.values():
            all_active_teams.update(tids)

        prob = pulp.LpProblem(f"alloc_{dow}", pulp.LpMinimize)
        # x[(team, sid, task_type_id)] = agents sur ce shift affectés à cette tâche
        x: dict[tuple, pulp.LpVariable] = {}
        # task_types pertinents pour chaque équipe ce dow (union des tâches de ses group/level pairs)
        team_tasks: dict[str, set[str]] = {}
        for _gid, task_type_id, _lvl, tids in gtl_triples:
            for t in tids:
                team_tasks.setdefault(t, set()).add(task_type_id)
        for (_lvl, task_type_id), tids in mutualized_teams.items():
            for t in tids:
                team_tasks.setdefault(t, set()).add(task_type_id)

        for team in all_active_teams:
            ub = int(cap[team]) if pd.notna(cap[team]) else None
            for sid, L, _cover in cand[team]:
                for task_type_id in team_tasks.get(team, ()):
                    x[(team, sid, task_type_id)] = pulp.LpVariable(
                        f"x_{team}_{sid}_{task_type_id}", lowBound=0, upBound=ub, cat="Integer")

        if not x:
            continue

        # Objective: minimize total cost
        L_by_sid: dict[tuple, int] = {}
        for team in all_active_teams:
            for sid, L, _c in cand[team]:
                L_by_sid[(team, sid)] = L
        prob += pulp.lpSum(
            var * L_by_sid[(team, sid)] * BUCKET_HOURS * cost_h[team]
            for (team, sid, _task), var in x.items()
        )

        # L1 coverage: une contrainte par (group, task, slot) — cible Erlang C par tâche.
        for group_id, task_type_id, level, team_ids in gtl_triples:
            req_d = {int(r.slot_utc): r.required_fte
                     for r in rep_d[(rep_d["group_id"] == group_id)
                                    & (rep_d["task_type_id"] == task_type_id)
                                    & (rep_d["level"] == level)].itertuples()}
            for slot in range(BUCKETS_PER_DAY):
                need = req_d.get(slot, 0.0)
                if need > 0:
                    terms = [x[(team, sid, task_type_id)] * prod[team]
                             for team in team_ids
                             for (sid, _L) in cover_idx[team][slot]
                             if (team, sid, task_type_id) in x]
                    if terms:
                        prob += pulp.lpSum(terms) >= need

        # L2 mutualized coverage: une contrainte par (level, task, slot), poolée multi-groupe.
        for (lvl, task_type_id), tids in mutualized_teams.items():
            req_d = {int(r.slot_utc): r.required_fte
                     for r in rep_pool_d[(rep_pool_d["level"] == lvl)
                                         & (rep_pool_d["task_type_id"] == task_type_id)].itertuples()}
            for slot in range(BUCKETS_PER_DAY):
                need = req_d.get(slot, 0.0)
                if need > 0:
                    terms = [x[(team, sid, task_type_id)] * prod[team]
                             for team in tids
                             for (sid, _L) in cover_idx[team][slot]
                             if (team, sid, task_type_id) in x]
                    if terms:
                        prob += pulp.lpSum(terms) >= need

        # Capacité globale par (team, slot) : la somme des agents sur TOUTES les tâches
        # et TOUS les shifts couvrant ce créneau ne peut dépasser cap[team]. C'est ce qui
        # empêche le double-comptage entre tâches (un agent physique ne fait qu'une tâche
        # à la fois).
        for team in all_active_teams:
            if not pd.notna(cap[team]):
                continue
            cap_val = int(cap[team])
            for slot in range(BUCKETS_PER_DAY):
                terms = [x[(team, sid, task_type_id)]
                         for (sid, _L) in cover_idx[team][slot]
                         for task_type_id in team_tasks.get(team, ())
                         if (team, sid, task_type_id) in x]
                if terms:
                    prob += pulp.lpSum(terms) <= cap_val

        prob.solve(solver)

        lp_status = pulp.LpStatus[prob.status]
        if lp_status != "Optimal":
            day_names = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
            msg = f"{day_names[dow]} ({lp_status})"
            _run_warnings.append(msg)
            logger.warning(
                "LP %s: statut=%s — allocation possiblement incomplète pour dow=%d "
                "(capacité insuffisante ou fenêtres de disponibilité trop courtes ?)",
                prob.name, lp_status, dow,
            )

        # Extraction : agents par (team, slot, task) = somme des x[(team, sid, task)]
        # pour tous les shifts couvrant ce créneau. On applique le plafond cap[team]
        # globalement (toutes tâches confondues) après résolution, au cas où CBC
        # renvoie une solution qui viole légèrement la contrainte (infaisabilité).
        for team in all_active_teams:
            cap_val = int(cap[team]) if pd.notna(cap[team]) else None
            for slot in range(BUCKETS_PER_DAY):
                per_task = {}
                for task_type_id in team_tasks.get(team, ()):
                    agents = sum((x[(team, sid, task_type_id)].value() or 0)
                                 for (sid, _L) in cover_idx[team][slot]
                                 if (team, sid, task_type_id) in x)
                    if agents > 0:
                        per_task[task_type_id] = agents
                total = sum(per_task.values())
                if cap_val is not None and total > cap_val and total > 0:
                    scale = cap_val / total
                    per_task = {k: v * scale for k, v in per_task.items()}
                for task_type_id, agents in per_task.items():
                    rounded = int(round(agents))
                    if rounded > 0:
                        alloc_rows.append({"dow": dow, "slot_utc": slot, "team_id": team,
                                           "task_type_id": task_type_id, "agents": rounded})

    alloc = pd.DataFrame(alloc_rows, columns=["dow", "slot_utc", "team_id", "task_type_id", "agents"])
    if write:
        db.write_table("allocation", alloc, db_path)
    return alloc


# --- Couverture à partir de l'allocation (optimisée ou éditée) ---------------
def expand_allocation(month: str, db_path=db.DB_PATH) -> pd.DataFrame:
    """Allocation (par dow/slot) déployée sur les buckets du mois, par équipe × tâche.

    Chaque équipe est croisée avec TOUS les groupes qu'elle sert (team_group) : pour un
    level mutualisé, la capacité pour une tâche donnée est disponible à chaque groupe qui
    en a besoin depuis le pool partagé (cohérent avec le fix M1 — pas une duplication
    physique, juste une vue "capacité disponible depuis le pool" par groupe).
    """
    alloc = db.read_table("allocation", db_path)
    teams = db.read_table("team", db_path)
    team_group = db.read_table("team_group", db_path)
    required = required_by_task(build_forecast_demand(month, db_path), db_path)
    buckets = pd.DataFrame({"bucket_utc": sorted(required["bucket_utc"].unique())})
    buckets["dow"] = buckets["bucket_utc"].dt.dayofweek
    buckets["slot_utc"] = buckets["bucket_utc"].dt.hour * 4 + buckets["bucket_utc"].dt.minute // 15
    if alloc.empty:
        return pd.DataFrame(columns=["bucket_utc", "team_id", "group_id", "level", "task_type_id",
                                     "agents", "effective_capacity", "cost"])
    sup = (buckets.merge(alloc, on=["dow", "slot_utc"])
           .merge(teams[["team_id", "level", "productivity", "hourly_cost"]], on="team_id")
           .merge(team_group, on="team_id"))
    sup["effective_capacity"] = sup["agents"] * sup["productivity"]
    sup["cost"] = sup["agents"] * BUCKET_HOURS * sup["hourly_cost"]
    return sup[["bucket_utc", "team_id", "group_id", "level", "task_type_id", "agents",
                "effective_capacity", "cost"]]


def build_coverage(month: str, db_path=db.DB_PATH) -> dict:
    """Rapproche ETP requis (Erlang C, par tâche) et capacité allouée. Renvoie matching + supply/équipe."""
    demand = build_forecast_demand(month, db_path)
    required = required_by_task(demand, db_path)
    supply_team = expand_allocation(month, db_path)

    sup = (supply_team.groupby(["bucket_utc", "level", "group_id", "task_type_id"], as_index=False)
           [["effective_capacity", "agents", "cost"]].sum()
           if not supply_team.empty else
           pd.DataFrame(columns=["bucket_utc", "level", "group_id", "task_type_id",
                                  "effective_capacity", "agents", "cost"]))
    m = required.merge(sup, on=["bucket_utc", "level", "group_id", "task_type_id"], how="left")
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
    return {"matching": m.sort_values(["level", "group_id", "task_type_id", "bucket_utc"]).reset_index(drop=True),
            "supply_team": supply_team, "demand": demand}
