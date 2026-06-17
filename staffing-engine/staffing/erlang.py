"""Erlang C — dimensionnement par objectif de service.

Remplace le simple gross-up par occupation : à partir du volume de contacts sur
un bucket de 15 min et de l'AHT, on calcule le **nombre d'agents** nécessaire
pour tenir la cible de service `sl_target` (% de contacts pris en ≤ `sl_seconds`),
sous contrainte d'occupation maximale.

Le résultat est le nb d'agents "en ligne" ; on le convertit ensuite en effectif
à planifier (roster) en divisant par (1 − shrinkage).
"""
from __future__ import annotations

import math
from functools import lru_cache


def erlang_b(n: int, a: float) -> float:
    """Probabilité de blocage Erlang B (récurrence stable)."""
    b = 1.0
    for k in range(1, n + 1):
        b = (a * b) / (k + a * b)
    return b


def prob_wait(n: int, a: float) -> float:
    """Probabilité d'attente (Erlang C) pour n agents et charge offerte a (erlangs)."""
    if n <= a:
        return 1.0
    b = erlang_b(n, a)
    return n * b / (n - a * (1.0 - b))


def service_level(n: int, a: float, aht_seconds: float, sl_seconds: float) -> float:
    """P(attente ≤ sl_seconds) avec n agents."""
    if n <= a:
        return 0.0
    pw = prob_wait(n, a)
    return 1.0 - pw * math.exp(-(n - a) * sl_seconds / aht_seconds)


@lru_cache(maxsize=200_000)
def agents_required(calls: float, aht_seconds: float, interval_seconds: int,
                    sl_target: float, sl_seconds: float, max_occupancy: float) -> int:
    """Nb minimal d'agents (en ligne) pour tenir la cible de service.

    calls = nb de contacts sur l'intervalle ; charge offerte a = calls·AHT/intervalle.
    """
    if calls <= 0 or aht_seconds <= 0:
        return 0
    a = calls * aht_seconds / interval_seconds  # erlangs
    n = max(1, math.floor(a) + 1)
    while True:
        if (a / n) <= max_occupancy and service_level(n, a, aht_seconds, sl_seconds) >= sl_target:
            return n
        n += 1
        if n > 100_000:  # garde-fou
            return n


def required_agents_series(calls, aht_seconds, sl_target, sl_seconds, max_occupancy,
                           interval_seconds: int = 900):
    """Version vectorisée (par lignes) renvoyant une liste d'entiers.

    `calls` et `aht_seconds` sont des séries/iterables alignés. L'AHT est arrondi
    à la seconde pour profiter du cache (beaucoup de buckets partagent les mêmes
    couples (calls arrondi, AHT)).
    """
    out = []
    for c, aht in zip(calls, aht_seconds):
        c_r = round(float(c), 2)
        aht_r = round(float(aht))
        out.append(agents_required(c_r, aht_r, interval_seconds,
                                   float(sl_target), float(sl_seconds), float(max_occupancy)))
    return out
