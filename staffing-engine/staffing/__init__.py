"""Moteur de staffing / capacity planning au pas de 15 min (UTC) — V2.

Couches :
    timespine — A1 : time spine UTC 15 min, calendriers, fuseaux
    db        — couche SQLite + registre des tables (entrée & éditables)
    model     — demande forecast → buckets, + table réel vs forecast
    erlang    — dimensionnement Erlang C (ETP requis par bucket × level)
    optimizer — répartition d'agents au coût minimal (IP) + couverture
    reporting — restitution (heatmap, trous, synthèses)

Maille pivot : 1 bucket de 15 min (UTC) × 1 level (capacité mutualisée intra-level).
"""

from . import db, erlang, model, optimizer, reporting, timespine

__all__ = ["timespine", "db", "model", "erlang", "optimizer", "reporting"]
