"""Moteur de staffing / capacity planning au pas de 15 min (UTC).

Architecture en 6 couches (cf. README) :

    A. referentials  — référentiels (calendrier, supply, region, group, tâches, équipes)
    B. params        — paramètres (AHT, shrinkage, occupation cible, coûts, SLA)
    C. demand        — pipeline de charge  (mensuel -> jour -> bucket -> ETP requis)
    D. supply        — pipeline de capacité (shifts -> capacité dispo par bucket)
    E. matching      — rapprochement requis vs dispo (couverture, écarts, occupation réelle)
    F. reporting     — restitution (heatmap, synthèse coûts/ETP, trous)

La maille pivot du modèle est : 1 bucket de 15 min (UTC) x 1 segment (level + type
de tâche). Demande et offre produisent toutes deux cette maille pour pouvoir se joindre.
"""

from . import timespine, referentials, params, demand, supply, matching, reporting

__all__ = [
    "timespine",
    "referentials",
    "params",
    "demand",
    "supply",
    "matching",
    "reporting",
]
