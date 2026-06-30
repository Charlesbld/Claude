"""Pages de l'application Streamlit — visualisation, édition, glossaire (fichier unique).

Toutes les pages rendues via st.navigation sont définies ici :
  render_sources · render_demand_monthly · render_demand_profiles · render_demand_buckets
  render_erlang · render_coverage · render_forecast · render_monthly_report
  render_explorer
  render_ref_regions · render_ref_groups · render_ref_tasks
  render_teams_page · render_params_page · render_glossary
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import common as C
from staffing import db, reporting
from staffing.timespine import BUCKET_HOURS, BUSINESS_TZ

DIV = "RdYlGn"


def _sanitize_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Préfixe d'une apostrophe les cellules texte commençant par = + - @ (injection de formule Excel)."""
    out = df.copy()
    for col in out.select_dtypes(include="object").columns:
        mask = out[col].astype(str).str.match(r"^[=+\-@]")
        out.loc[mask, col] = "'" + out.loc[mask, col].astype(str)
    return out


# Mapping couleur level (dynamique : L1, L2, L3…)
_LEVEL_PALETTE = ["#3498DB", "#E67E22", "#2ECC71", "#9B59B6", "#E74C3C"]


def _level_color_map(levels) -> dict[str, str]:
    """Renvoie {\"L1\": couleur, \"L2\": couleur, …} pour n'importe quel ensemble de niveaux."""
    return {f"L{l}": _LEVEL_PALETTE[i % len(_LEVEL_PALETTE)]
            for i, l in enumerate(sorted(int(x) for x in set(levels)))}


# Dépendances FK par table — utilisé pour alerter en cas de renommage d'ID.
_FK_DEPS: dict[str, list[tuple[str, str]]] = {
    "region":    [("pax_forecast", "region_id"), ("pax_real", "region_id"),
                  ("contact_rate_forecast", "region_id"), ("tasks_real", "region_id"),
                  ("group_map", "region_id")],
    "supply":    [("pax_forecast", "supply_id"), ("pax_real", "supply_id"),
                  ("contact_rate_forecast", "supply_id"), ("tasks_real", "supply_id"),
                  ("group_map", "supply_id")],
    "task_type": [("contact_rate_forecast", "task_type_id"), ("tasks_real", "task_type_id"),
                  ("param_aht", "task_type_id")],
    "group":     [("group_map", "group_id"), ("team_group", "group_id"),
                  ("param_aht", "group_id")],
}


def _check_key_rename(old_df: pd.DataFrame, new_df: pd.DataFrame,
                      key_col: str, table_name: str) -> None:
    """Détecte les IDs supprimés ou renommés et avertit l'utilisateur des FK impactées."""
    old_ids = set(old_df[key_col].dropna().astype(str))
    new_ids = set(new_df[key_col].dropna().astype(str))
    removed = old_ids - new_ids
    if not removed:
        return
    deps = _FK_DEPS.get(table_name, [])
    dep_str = ", ".join(f"`{tbl}.{col}`" for tbl, col in deps) if deps else "tables liées"
    st.warning(
        f"⚠️ **IDs supprimés ou renommés** : {', '.join(f'`{x}`' for x in sorted(removed))}. "
        f"Ces valeurs sont utilisées comme clé étrangère dans : {dep_str}. "
        "Les lignes orphelines seront ignorées dans les calculs. "
        "Mettez à jour ces tables ou utilisez **Ingestion CSV** pour ré-importer les données avec le nouvel ID."
    )


def _cascade_info(items: list[tuple[str, str]]) -> None:
    """Affiche une checklist de tâches de cascade post-sauvegarde."""
    lines = "\n".join(f"- {icon} {desc}" for icon, desc in items)
    st.info(f"**📋 Pour que ce changement soit actif dans le dimensionnement :**\n\n{lines}")


# =============================================================================
# BLOCS DE DOCUMENTATION (un par page)
# Chaque fonction est appelée dans un st.expander depuis la page correspondante.
# =============================================================================

def _doc_sources():
    st.markdown("""
### Rôle de cette page
Vérifier que toutes les **tables d'entrée** sont bien alimentées avant de lancer les calculs.
Un chiffre faux ici se propage à toute la chaîne sans aucun message d'erreur.
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];
    edge [fontsize=10];

    pax  [label="pax_forecast\\n(mois, région, supply)\\n→ pax", fillcolor="#d4edda"];
    crf  [label="contact_rate_forecast\\n(mois, région, supply, tâche)\\n→ contact_rate", fillcolor="#d4edda"];
    gmap [label="group_map\\n(mois, région, supply)\\n→ group_id, active", fillcolor="#fff3cd"];
    grp  [label="group\\n(group_id)\\n→ group_label"];
    task [label="task_type\\n(task_type_id)\\n→ level (1=L1, 2=L2)"];
    aht  [label="param_aht\\n(task_type_id, group_id?)\\n→ aht_seconds"];
    sp   [label="service_params\\n(level, group_id?)\\n→ sl_target, sl_seconds,\\nshrinkage, max_occupancy", fillcolor="#fff3cd"];
    team [label="team\\n(team_id)\\n→ level, productivity,\\nhourly_cost, timezone, max_agents", fillcolor="#d4edda"];
    tg   [label="team_group\\n(team_id, group_id)"];
    avail[label="team_availability\\n(team_id, dow)\\n→ start_local, end_local"];
    pdow [label="profile_dow\\n(dow)\\n→ weight"];
    pid  [label="profile_intraday\\n(dow, slot_local)\\n→ weight"];

    pax -> gmap [label="mois×région×supply"];
    crf -> gmap [label="mois×région×supply"];
    gmap -> grp [label="group_id"];
    task -> aht [label="task_type_id"];
    team -> tg;
    grp -> tg [label="group_id"];
    team -> avail;
    pdow -> pid [label="DOW"];
}
""")
    st.markdown("""
#### Lecture du schéma

| Table | Clé | Rôle dans le modèle |
|-------|-----|---------------------|
| `pax_forecast` | mois × région × supply | Volume de passagers prévu — base de la demande |
| `contact_rate_forecast` | + tâche | Taux de contact par type de tâche (contacts / PAX) |
| `group_map` | mois × région × supply | Affectation d'un combo région×supply à un groupe commercial |
| `task_type` | task_type_id | Niveau de compétence requis (L1 externe ou L2 interne) |
| `param_aht` | task_type + group_id optionnel | Temps de traitement moyen en secondes |
| `service_params` | level + group_id optionnel | Objectifs SLA, shrinkage, occupation max |
| `team` | team_id | Caractéristiques de l'équipe (coût, productivité, fuseau) |
| `team_group` | team_id × group_id | Quels groupes une équipe peut-elle traiter ? |
| `team_availability` | team_id × dow | Fenêtres horaires où l'équipe peut travailler (heure locale) |
| `profile_dow` | dow | Poids relatif de chaque jour de semaine dans le mois |
| `profile_intraday` | dow × slot_local | Répartition des contacts sur les 96 créneaux de 15 min |

> **Point de vigilance** : `group_map` est la clé de voûte. Si une combinaison
> région×supply est absente ou `active=0`, toute la demande associée disparaît silencieusement
> lors des jointures internes. Vérifier le KPI « Conservation » en page ④.
""")


def _doc_demand_monthly():
    st.markdown("### Formule fondamentale")
    st.latex(r"""
\text{contacts}_{m,r,s,t,g} = \text{PAX\_forecast}_{m,r,s} \times \text{contact\_rate\_forecast}_{m,r,s,t}
""")
    st.markdown("""
où :
- $m$ = mois, $r$ = région, $s$ = supply (mode de transport), $t$ = type de tâche, $g$ = groupe commercial
- `contact_rate` ∈ [0 ; +∞[ — le nombre moyen de tâches générées par passager pour ce type de tâche

**Chaîne de jointures** (toutes les jointures internes → perte silencieuse si clé manquante) :
""")
    st.graphviz_chart("""
digraph {
    rankdir=TD; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    pax  [label="pax_forecast\\n(mois,région,supply)\\nN lignes", fillcolor="#d4edda"];
    crf  [label="contact_rate_forecast\\n(mois,région,supply,tâche)\\nN×T lignes", fillcolor="#d4edda"];
    m1   [label="INNER JOIN\\nmois×région×supply\\n→ N×T lignes (pax dupliqué ×T)"];
    gmap [label="group_map\\n(mois,région,supply,active=1)\\n→ group_id"];
    m2   [label="INNER JOIN région×supply\\n⚠ lignes sans mapping → perdues"];
    grp  [label="group\\n→ group_label (LEFT JOIN)"];
    base [label="base : contacts = pax × contact_rate\\nN×T×G lignes", fillcolor="#fff3cd"];

    pax -> m1; crf -> m1; m1 -> m2; gmap -> m2; m2 -> grp -> base;
}
""")
    st.markdown("""
#### Précautions sur les KPI affichés

Le `base` DataFrame a **N_task_types copies de chaque valeur pax** (une par tâche).
Pour les KPI de tête, on dédoublonne avant de sommer :

```
PAX total     = pax_forecast[month]["pax"].sum()          # avant le merge crf
Total contacts = base["contacts"].sum()                    # après toutes les jointures
CR moyen       = Total contacts / PAX total
```

Sans ce dédoublonnage, `PAX total` serait gonflé d'un facteur égal au nombre de types de tâche.

#### Conservation à vérifier
La somme `contacts` de cette page doit être **égale** à la somme `contacts_attendus (PAX×CR)`
affichée en page ④ (KPI « Conservation »). Tout écart révèle une perte en jointure.
""")


def _doc_demand_profiles():
    st.markdown("""
### Principe : du mensuel vers les buckets en deux étapes

Le volume mensuel de contacts est ventilé d'abord par **jour de semaine** (profil DOW),
puis par **créneau de 15 min** (profil intraday), tout en conservant le volume total.
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    mensuel [label="contacts_mensuel\\n(par région×supply×tâche×groupe)", fillcolor="#d4edda"];
    dow     [label="profile_dow\\ndow → weight\\n(normalisé sur les dates du mois)"];
    jour    [label="contacts_jour(date)\\n= mensuel × shape_dow(date)"];
    intra   [label="profile_intraday\\n(dow, slot_local) → weight\\n(normalisé par DOW)"];
    bucket  [label="contacts_bucket(date, slot_local)\\n= contacts_jour × weight_intraday", fillcolor="#fff3cd"];
    utc     [label="bucket_utc\\n(conversion heure locale → UTC\\npar fuseau de la région)", fillcolor="#fff3cd"];

    mensuel -> jour [label="× shape_dow(date)"];
    dow -> jour;
    jour -> bucket [label="× weight_intraday(dow, slot)"];
    intra -> bucket;
    bucket -> utc [label="tz_localize + tz_convert"];
}
""")
    st.markdown("#### Étape 1 — Profil jour de semaine")
    st.latex(r"""
\text{shape\_dow}(d) = \frac{w_{\text{DOW}}[\text{dayofweek}(d)]}{\sum_{d' \in \text{mois}} w_{\text{DOW}}[\text{dayofweek}(d')]}
\quad \text{avec} \quad \sum_{d \in \text{mois}} \text{shape\_dow}(d) = 1
""")
    st.markdown("""
**Correction jours fériés (IMP-3)** : pour la région concernée, le poids du jour férié est
remplacé par le poids du dimanche (`dow=6`), puis l'ensemble est re-normalisé.
Cela réduit la demande projetée ce jour-là sans altérer le total mensuel.
""")
    st.markdown("#### Étape 2 — Profil intraday")
    st.latex(r"""
w_{\text{bucket}}(d, s) = \text{shape\_dow}(d) \times \text{weight\_intraday}[\text{DOW}(d), s]
\quad \text{avec} \quad \sum_{d,s} w_{\text{bucket}}(d, s) = 1
""")
    st.markdown("""
#### Étape 3 — Conversion heure locale → UTC
Chaque slot local `s` de la date `d` est localisé dans le fuseau de la région, puis converti en UTC :
```
bucket_utc = tz_localize(date + s×15min, tz, nonexistent="shift_forward", ambiguous="NaT")
             .tz_convert("UTC")
```
Les créneaux ambigus (changement d'heure d'hiver) → `NaT` → supprimés (~4 buckets perdus 2×/an).

#### Conservation (invariant testé)
```
Σ contacts_bucket sur tout le mois = contacts_mensuel  (à l'arrondi flottant près)
```
""")


def _doc_demand_buckets():
    st.markdown("""
### Vue consolidée de la demande à 15 min

Cette page est le **résultat final** de la chaîne forecast → profils → UTC.
C'est le DataFrame `demand` sur lequel toute la suite (Erlang C, optimiseur, couverture) s'appuie.
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    pax   [label="PAX forecast\\n+ contact rate forecast", fillcolor="#d4edda"];
    prof  [label="Profils DOW\\n+ Intraday\\n+ Jours fériés", fillcolor="#d4edda"];
    aht   [label="param_aht\\n(AHT en secondes)", fillcolor="#d4edda"];
    dem   [label="demand\\n(bucket_utc, level, task, group)\\n→ contacts, workload_hours", fillcolor="#fff3cd"];
    check [label="KPI Conservation\\n∑ contacts = PAX×CR ?"];

    pax -> dem [label="× profils"];
    prof -> dem;
    aht -> dem [label="workload"];
    dem -> check;
}
""")
    st.markdown("#### Champs calculés dans `demand`")
    st.latex(r"""
\text{contacts\_bucket}_{b,l,t,g} = \text{contacts\_mensuel}_{r,s,t,g} \times w_{\text{bucket}}(d, \text{slot})
""")
    st.latex(r"""
\text{workload\_hours}_{b,l,t,g} = \text{contacts\_bucket} \times \frac{\text{AHT (secondes)}}{3600}
""")
    st.markdown("""
#### Granularité et clés
| Clé | Description |
|-----|-------------|
| `bucket_utc` | Timestamp UTC, pas de 15 min, timezone-aware |
| `level` | 1 = L1 externe (BPO), 2 = L2 interne (escalades) |
| `task_type_id` | Type de tâche (appel entrant, email, remboursement…) |
| `group_id` | Groupe commercial (DIRECT_AIR, OTA, RAIL…) |
| `region_id` | Région géographique |
| `supply_id` | Mode de transport |

#### KPI de conservation
```
contacts_attendus = Σ (pax_forecast × contact_rate)   — calcul direct sans profils
contacts_bucket   = demand["contacts"].sum()            — après application des profils
Conservation = contacts_bucket / contacts_attendus ≈ 1.0000
```
Un écart > 0.1 % révèle un bug dans les profils ou une perte en jointure.

#### AHT par groupe (IMP-4)
Si `param_aht` contient une colonne `group_id`, la priorité est :
1. AHT spécifique `(task_type_id, group_id)` — ex. les remboursements DIRECT_AIR sont plus longs
2. AHT générique `(task_type_id, group_id=NULL)` — fallback pour tous les groupes
""")


def _doc_erlang():
    st.markdown("""
### Chaîne de calcul : de la charge à l'ETP requis

Pour chaque bucket (15 min) × level × groupe, Erlang C détermine le nombre minimal d'agents
*en ligne* pour respecter le SLA, puis un gross-up shrinkage donne les ETP à planifier.
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    dem   [label="demand\\ncontacts, workload_hours\\npar bucket×level×group", fillcolor="#d4edda"];
    pool  [label="Agrégation par\\nbucket×level×group\\n(somme contacts + workload)"];
    aht   [label="AHT effectif pondéré\\naht_eff = workload_s / contacts"];
    sp    [label="service_params\\nsl_target, sl_seconds\\nshrinkage, max_occupancy", fillcolor="#d4edda"];
    erlc  [label="Erlang C\\nbinary search sur N\\ntels que SLA atteint", fillcolor="#fff3cd"];
    aon   [label="agents_online\\n(agents au combiné)"];
    gross [label="gross-up shrinkage\\nrequired_fte = agents_online\\n/ (1 − shrinkage)", fillcolor="#fff3cd"];

    dem -> pool -> aht -> erlc;
    sp -> erlc;
    erlc -> aon -> gross;
}
""")
    st.markdown("#### Trafic offert (Erlangs)")
    st.latex(r"""
A = \frac{\lambda \times \text{AHT}}{\text{INTERVAL}} = \frac{\text{contacts\_bucket}}{\text{900 s}} \times \text{aht\_seconds}
""")
    st.markdown("#### Formule Erlang C — probabilité d'attente")
    st.latex(r"""
C(N, A) = \frac{\dfrac{A^N}{N!} \cdot \dfrac{N}{N - A}}
           {\displaystyle\sum_{k=0}^{N-1} \frac{A^k}{k!} + \frac{A^N}{N!} \cdot \frac{N}{N - A}}
\quad \text{(défini pour } A < N \text{)}
""")
    st.markdown("#### Condition SLA")
    st.latex(r"""
P(\text{attente} \leq T) = 1 - C(N, A) \cdot e^{-(N - A) \cdot \mu \cdot T} \geq \text{sl\_target}
\quad \text{avec } \mu = 1 / \text{aht\_seconds}
""")
    st.markdown("""
**Algorithme** : recherche binaire sur $N \\in [\\max(1, \\lceil A \\rceil), N_{\\max}]$.
À chaque valeur de $N$, on vérifie deux conditions :
1. La condition SLA ci-dessus (P(attente ≤ T) ≥ sl_target)
2. La contrainte d'occupation : $A/N \\leq \\text{max\\_occupancy}$ (évite la surcharge)

Si les deux sont satisfaites, $N$ est le `agents_online`.

#### Gross-up shrinkage
""")
    st.latex(r"""
\text{required\_fte} = \frac{\text{agents\_online}}{1 - \text{shrinkage}}
""")
    st.markdown("""
Le `shrinkage` représente la fraction du temps où un agent planifié n'est **pas disponible**
(pauses, formation, absences, réunions). Un shrinkage de 30 % signifie que 1 ETP planifié
= 0,70 ETP au combiné → il faut planifier `agents_online / 0,70` ETP.

#### AHT effectif pondéré (après agrégation cross-tâches)
""")
    st.latex(r"""
\text{aht\_eff}_{b,l,g} = \frac{\sum_t \text{workload\_seconds}_{b,l,t,g}}{\sum_t \text{contacts}_{b,l,t,g}}
""")
    st.markdown("""
> **Hypothèse de mutualisaton** : le calcul Erlang C est fait par (level, groupe) séparément.
> Si une équipe L2 sert plusieurs groupes, les besoins sont calculés indépendamment pour chaque
> groupe, puis l'optimiseur partage la même équipe entre eux (file mutualisée implicite).
> Voir le commentaire dans `optimizer.py` pour les implications.
""")


def _doc_coverage():
    st.markdown("""
### Formulation du problème d'optimisation (LP entier)

L'optimiseur choisit le nombre d'agents par (équipe, shift) pour couvrir l'ETP requis
au coût minimal, respectant les disponibilités et le plafond d'effectif.
""")
    st.graphviz_chart("""
digraph {
    rankdir=TD; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    req   [label="required_fte\\npar (bucket, level, group)\\n(Erlang C)", fillcolor="#d4edda"];
    avail [label="team_availability\\nfenêtres de dispo\\n(heure locale → UTC)", fillcolor="#d4edda"];
    cost  [label="team\\nhourly_cost, productivity\\nmax_agents", fillcolor="#d4edda"];
    lp    [label="LP entier (PuLP/CBC)\\npar DOW\\nmin Σ coût agents", fillcolor="#fff3cd"];
    alloc [label="allocation\\n(dow, slot_utc, team_id)\\n→ agents", fillcolor="#fff3cd"];
    exp   [label="expand_allocation\\nalloc × team_group\\n→ déploiement sur le mois"];
    cov   [label="build_coverage\\ncoverage_ratio, gap_fte\\nreal_occupancy", fillcolor="#fff3cd"];

    req -> lp [label="contrainte couverture"];
    avail -> lp [label="shifts valides"];
    cost -> lp [label="objectif + borne"];
    lp -> alloc -> exp -> cov;
    req -> cov [label="required_fte"];
}
""")
    st.markdown("#### Variables de décision")
    st.latex(r"""
x_{\text{team}, \text{sid}} \in \mathbb{Z}_{\geq 0}
\quad \text{: nb d'agents de l'équipe \textit{team} sur le shift \textit{sid}}
""")
    st.markdown("""
Un **shift** `sid` = bloc contigu de `L` buckets (par défaut 24 ou 32 buckets = 6h ou 8h)
entièrement inclus dans la fenêtre de disponibilité de l'équipe pour ce DOW.
Les démarrages sont posés sur une grille horaire (`step=4`) pour limiter la symétrie en LP entier.
""")
    st.markdown("#### Objectif — coût total minimal")
    st.latex(r"""
\min \sum_{\text{team}, \text{sid}} x_{\text{team}, \text{sid}} \times L_{\text{sid}} \times \Delta t \times \text{hourly\_cost}_{\text{team}}
\quad \text{avec } \Delta t = 0{,}25\,\text{h (bucket)}
""")
    st.markdown("#### Contrainte de couverture — par (groupe, level, slot UTC)")
    st.latex(r"""
\sum_{\substack{\text{team} \in \mathcal{T}_{g,l} \\ \text{sid} \ni \text{slot}}}
x_{\text{team}, \text{sid}} \times \text{productivity}_{\text{team}}
\;\geq\; \text{required\_fte}_{g, l, \text{slot}}
""")
    st.markdown("#### Contrainte de capacité — par (équipe, slot UTC)")
    st.latex(r"""
\sum_{\text{sid} \ni \text{slot}} x_{\text{team}, \text{sid}} \;\leq\; \text{max\_agents}_{\text{team}}
\quad \forall (\text{team}, \text{slot})
""")
    st.markdown("""
Cette contrainte globale est la correction **CRIT-1** : elle empêche qu'une équipe soit
allouée plus que son effectif physique maximal quand elle sert plusieurs groupes.

#### Métriques de couverture post-optimisation
""")
    st.latex(r"""
\text{effective\_capacity}_{\text{bucket}, l, g}
= \sum_{\text{team} \in \mathcal{T}_{g,l}} x_{\text{team,slot}} \times \text{productivity}_{\text{team}}
""")
    st.latex(r"""
\text{coverage\_ratio} = \frac{\text{effective\_capacity}}{\text{required\_fte}}
\quad (\geq 1 = \text{couvert},\; < 1 = \text{trou})
""")
    st.latex(r"""
\text{real\_occupancy} = \frac{\text{workload\_hours}}
{\text{agents} \times \text{productivity} \times (1 - \text{shrinkage}) \times \Delta t}
""")
    st.markdown("""
#### Hypothèse de mutualisation (à revoir si L2 étanche)
La contrainte de couverture compte les **mêmes agents** pour chaque groupe qu'ils servent.
Ceci suppose que les agents L2 traitent n'importe quel groupe au fil de l'eau (file mutualisée).
Si les groupes sont des **files étanches**, il faudrait une variable `x[team, sid, group]`
avec la contrainte de liaison `Σ_group x[team,sid,group] = x[team,sid]`.
""")


def _doc_forecast():
    st.markdown("""
### Décomposition de l'écart tâches Réel vs Forecast

On cherche à expliquer *pourquoi* les tâches réelles diffèrent du forecast :
est-ce dû aux **volumes PAX** ou au **comportement de contact** (contact rate) ?
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    paxf [label="pax_forecast\\n(par mois×région×supply)", fillcolor="#d4edda"];
    paxr [label="pax_real\\n(indicatif)", fillcolor="#d4edda"];
    cr_f [label="contact_rate_forecast\\n(taux de contact retenu)", fillcolor="#d4edda"];
    tasks[label="tasks_real\\n(tâches réelles)", fillcolor="#d4edda"];
    cr_r [label="CR réel\\n= tasks_real / pax_real"];
    fc   [label="tasks_forecast\\n= pax_f × CR_f", fillcolor="#fff3cd"];
    delta[label="Décomposition\\nde l'écart", fillcolor="#fff3cd"];

    paxf -> fc; cr_f -> fc;
    paxr -> cr_r; tasks -> cr_r;
    fc -> delta; cr_r -> delta; paxf -> delta; paxr -> delta;
}
""")
    st.markdown("#### Formule de décomposition (exacte, sans résidu)")
    st.latex(r"""
\Delta\text{tâches} = \underbrace{(\text{PAX}_r - \text{PAX}_f) \times \text{CR}_f}_{\text{effet PAX}}
+ \underbrace{\text{PAX}_f \times (\text{CR}_r - \text{CR}_f)}_{\text{effet taux}}
+ \underbrace{(\text{PAX}_r - \text{PAX}_f) \times (\text{CR}_r - \text{CR}_f)}_{\text{interaction}}
""")
    st.latex(r"""
\text{Vérification :} \quad \text{effet PAX} + \text{effet taux} + \text{interaction}
= \text{PAX}_r \times \text{CR}_r - \text{PAX}_f \times \text{CR}_f = \Delta\text{tâches} \quad \checkmark
""")
    st.markdown("""
#### Interprétation
| Terme | Signification | Action |
|-------|---------------|--------|
| `effet_pax > 0` | Plus de passagers que prévu | Revoir le forecast PAX |
| `effet_taux > 0` | Les clients contactent plus souvent que prévu | Ajuster le contact rate |
| `interaction` | Croisement des deux effets | Généralement petit, signal de fiabilité |

#### Cas d'usage principal
Cette page sert à **calibrer le contact rate** mensuel avant de lancer le dimensionnement.
Un `CR_forecast` bien calé sur l'historique → un dimensionnement juste.

> **Note** : les PAX réels sont ingérés à titre indicatif.
> Seul le `contact_rate_forecast` (éditable) pilote le dimensionnement.
""")


def _doc_monthly_report():
    st.markdown("""
### Rapport mensuel — synthèse des KPI

Ce rapport consolide l'ensemble de la chaîne pour un mois donné, pensé pour être copié-collé
en comité ou exporté comme référence pour la réplication dans Pigment.
""")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent";
    node [shape=box, style=filled, fillcolor="#e8f4f8", fontsize=11];

    dem   [label="demand\\n(contacts, workload)"];
    req   [label="required_fte\\n(Erlang C)"];
    alloc [label="allocation\\n(agents par équipe)"];
    match [label="matching\\n(coverage, gaps)"];
    kpi   [label="KPI consolidés\\ncoût · ETP · couverture\\ncomparatif M vs M-1", fillcolor="#fff3cd"];

    dem -> req -> match;
    alloc -> match;
    match -> kpi;
    dem -> kpi;
}
""")
    st.markdown("""
#### KPI de tête — définitions exactes

| KPI | Formule | Interprétation |
|-----|---------|----------------|
| **Coût total** | `Σ agents × L_shift × Δt × hourly_cost` | Budget de staffing mensuel |
| **ETP requis (pic)** | `max(required_fte)` | Dimensionnement au créneau le plus chargé |
| **Capacité effective (pic)** | `max(effective_capacity)` | Pic de production disponible |
| **% créneaux en trou** | `Σ (required_fte > effective_capacity) / total_buckets` | Taux d'exposition au sous-staffing |
| **Heures en trou** | `Σ buckets_understaffed × 0,25 h` | Volume horaire sans couverture suffisante |
| **Occupation p50 / p95** | Médiane / 95e centile de `real_occupancy` | Distribution de la charge agents |

#### Comparatif M vs M-1
Tous les KPI sont calculés pour le mois courant **et** le mois précédent (si disponible).
La variation en % est affichée pour détecter les dérives.

#### Pour Pigment
La structure `by_level` de `reporting.cost_fte_summary()` produit les aggregats exacts
à reprendre dans Pigment :
- `total_cost` par level
- `required_fte_peak` / `required_fte_avg` par level
- `buckets_understaffed` par level
- `workload_hours` par group×région (pour l'allocation budgétaire)
""")


# =============================================================================
# ① Données sources
# =============================================================================
def render_sources():
    """① Données sources — aperçu de toutes les tables d'entrée."""
    st.header("📋 ① Données sources")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_sources()
    st.markdown("""
    Cette page montre l'état des tables d'entrée du modèle.
    Tout le moteur est piloté par ces données — on peut les modifier dans **Éditer les données**.
    """)

    with st.expander("📁 Où modifier chaque référentiel ?", expanded=False):
        st.markdown("""
| Référentiel | Édition **dans l'app** | Édition **dans le code** (seed initial) |
|-------------|------------------------|------------------------------------------|
| **Régions** (`region`) | Éditer les données → Régions | `scripts/seed_db.py` → `REGIONS` (ligne ~28) |
| **Modes de transport** (`supply`) | Éditer les données → Supply | `scripts/seed_db.py` → `SUPPLIES` (ligne ~32) |
| **Types de tâche** (`task_type`) | Éditer les données → Types de tâche | `scripts/seed_db.py` → `TASK_TYPES` (ligne ~35) |
| **Groupes commerciaux** (`group`) | Éditer les données → Groupes | `scripts/seed_db.py` → `GROUPS` (ligne ~39) |
| **Mapping groupe** (`group_map`) | Éditer les données → Mapping Group | `scripts/seed_db.py` → `REGION_GROUP_SUPPLY` (ligne ~47) |
| **Équipes** (`team`) | Éditer les données → Équipes | `scripts/seed_db.py` → `TEAMS` (ligne ~55) |
| **Disponibilités équipes** (`team_availability`) | Éditer les données → Disponibilités | `scripts/seed_db.py` → `AVAILABILITY` (ligne ~65) |
| **Affectation équipes→groupes** (`team_group`) | Éditer les données → Affectation | `scripts/seed_db.py` → `TEAM_GROUP` (ligne ~70) |
| **AHT** (`param_aht`) | Éditer les données → AHT | `scripts/seed_db.py` → `TASK_TYPES` colonne `aht_seconds` |
| **Objectifs SLA** (`service_params`) | Éditer les données → Objectifs SLA | `scripts/seed_db.py` → `SERVICE_PARAMS` (ligne ~84) |
| **Profil DOW** (`profile_dow`) | Éditer les données → Profil DOW | `scripts/seed_db.py` → `profile_dow` (ligne ~178) |
| **Profil intraday** (`profile_intraday`) | Éditer les données → Profil intraday | `scripts/seed_db.py` → `_intraday()` (ligne ~92) |

**Données mensuelles (CSV)** — déposer dans `data/incoming/` avec le format `<table>__<mois>.csv` :

| Fichier | Table cible | Exemple |
|---------|-------------|---------|
| `pax_forecast__2026-08.csv` | `pax_forecast` | Colonnes : `month, region_id, supply_id, pax` |
| `pax_real__2026-07.csv` | `pax_real` | Colonnes : `month, region_id, supply_id, pax` |
| `contact_rate_forecast__2026-08.csv` | `contact_rate_forecast` | Colonnes : `month, region_id, supply_id, task_type_id, contact_rate` |
| `tasks_real__2026-07.csv` | `tasks_real` | Colonnes : `month, region_id, supply_id, task_type_id, tasks` |
| `group_map__2026-08.csv` | `group_map` | Colonnes : `month, region_id, supply_id, group_id, active` |

> Les **templates CSV** avec les bons en-têtes sont disponibles dans `data/templates/`.
""")

    month = C.month_selector()

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("PAX forecast")
        pf = C.table("pax_forecast")
        d = pf[pf["month"] == month]
        fig = px.bar(d, x="region_id", y="pax", color="supply_id",
                     title=f"PAX par région × supply — {month}")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("Contact rate forecast")
        crf = C.table("contact_rate_forecast")
        d = crf[crf["month"] == month]
        fig = px.bar(d, x="task_type_id", y="contact_rate", color="region_id",
                     barmode="group", title=f"Contact rate par tâche — {month}")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch")

    st.subheader("Groupes commerciaux & affectation équipes")
    c3, c4 = st.columns(2)
    with c3:
        gm = C.table("group_map")
        grp = C.table("group")
        d = (gm[(gm["month"] == month) & (gm["active"] == 1)]
             .merge(grp, on="group_id", how="left"))
        st.dataframe(d[["region_id", "supply_id", "group_id", "group_label"]]
                     .sort_values(["group_id", "region_id"]),
                     width="stretch", hide_index=True)
    with c4:
        tg = C.table("team_group")
        tm = C.table("team")[["team_id", "team_label", "level"]]
        d = tg.merge(tm, on="team_id")
        st.dataframe(d.sort_values(["group_id", "level"]),
                     width="stretch", hide_index=True)


# =============================================================================
# ② Contacts mensuels
# =============================================================================
def render_demand_monthly():
    """② Contacts mensuels = PAX × contact rate."""
    st.header("✖️ ② Contacts mensuels")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_demand_monthly()
    st.markdown(r"""
    **Formule** : `contacts_mensuels = PAX_forecast × contact_rate_forecast`

    Pour chaque combinaison `(mois, région, supply, tâche)`, on multiplie les PAX attendus par le
    taux de contact retenu. C'est la **base de toute la chaîne** — si ces deux chiffres sont faux,
    tout le dimensionnement l'est aussi.
    """)
    month = C.month_selector()

    pf = C.table("pax_forecast")
    crf = C.table("contact_rate_forecast")
    gmap = C.table("group_map")
    grp = C.table("group")

    pf_month = pf[pf["month"] == month]
    crf_month = crf[crf["month"] == month]
    gmap_month = gmap[(gmap["month"] == month) & (gmap["active"] == 1)][["region_id", "supply_id", "group_id"]]

    # Conservation check : combien de combos (region×supply) du PAX forecast ont un mapping groupe actif ?
    pax_keys = set(zip(pf_month["region_id"], pf_month["supply_id"]))
    gmap_keys = set(zip(gmap_month["region_id"], gmap_month["supply_id"]))
    missing_in_gmap = pax_keys - gmap_keys
    if missing_in_gmap:
        st.warning(
            f"⚠️ {len(missing_in_gmap)} combo(s) région×supply présents dans pax_forecast "
            f"n'ont pas de mapping groupe actif ce mois-ci et seront ignorés : "
            f"{sorted(missing_in_gmap)}. Vérifier group_map."
        )

    base = (pf_month
            .merge(crf_month, on=["month", "region_id", "supply_id"])
            .merge(gmap_month, on=["region_id", "supply_id"])
            .merge(grp, on="group_id", how="left"))
    base["contacts"] = base["pax"] * base["contact_rate"]

    # PAX total : dédoublonner avant de sommer (pax est dupliqué × nb task_types après merge crf)
    pax_total = pf[pf["month"] == month]["pax"].sum()
    contacts_total = base["contacts"].sum()
    cr_moyen_str = f"{contacts_total / pax_total:.4f}" if pax_total > 0 else "—"
    C.kpi_row([
        ("Total contacts", f"{contacts_total:,.0f}"),
        ("PAX total", f"{pax_total:,.0f}"),
        ("CR moyen (toutes tâches)", cr_moyen_str),
        ("Régions actives", str(base["region_id"].nunique())),
    ])

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Contacts par tâche")
        d = base.groupby("task_type_id")["contacts"].sum().reset_index()
        fig = px.bar(d, x="task_type_id", y="contacts", color="task_type_id",
                     text_auto=".3s")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
        st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("Contacts par groupe")
        d = base.groupby(["group_id", "task_type_id"])["contacts"].sum().reset_index()
        fig = px.bar(d, x="group_id", y="contacts", color="task_type_id")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), legend_title="")
        st.plotly_chart(fig, width="stretch")

    st.subheader("Tableau détaillé")
    disp = base[["region_id", "supply_id", "group_id", "task_type_id", "pax", "contact_rate", "contacts"]].copy()
    disp["contacts"] = disp["contacts"].round(0).astype(int)
    st.dataframe(disp.sort_values(["group_id", "region_id", "task_type_id"]),
                 width="stretch", hide_index=True)


# =============================================================================
# ③ Profils de répartition
# =============================================================================
def render_demand_profiles():
    """③ Profils de répartition — du mensuel vers les buckets 15 min."""
    st.header("📅 ③ Profils de répartition")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_demand_profiles()
    st.markdown(r"""
    La demande mensuelle est **découpée** en deux étapes successives :

    1. **Profil hebdomadaire** : chaque jour de semaine reçoit un poids → contacts du jour = contacts_mois × w_dow / Σw_dow
    2. **Profil intraday** : dans chaque jour, chaque créneau de 15 min reçoit un poids → contacts_bucket = contacts_jour × w_slot

    Ces profils pilotent la **forme de la courbe de charge** — ils sont modifiables dans *Éditer les données*.
    """)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Profil jour de semaine (dow)")
        dow_df = C.table("profile_dow")
        jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        dow_df = dow_df.copy()
        dow_df["jour"] = dow_df["dow"].map(lambda d: jours[d])
        fig = px.bar(dow_df, x="jour", y="weight", color="weight",
                     color_continuous_scale="Blues",
                     text=dow_df["weight"].map(lambda w: f"{w:.2f}"))
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                          showlegend=False, coloraxis_showscale=False,
                          yaxis_title="Poids relatif")
        st.plotly_chart(fig, width="stretch")
        st.caption("Le samedi et dimanche ont un poids plus élevé → **pics de week-end**.")
    with c2:
        st.subheader("Profil intraday par jour de semaine")
        intra = C.table("profile_intraday")
        intra = intra.copy()
        intra["heure"] = (intra["slot_local"] * 15 // 60).astype(str).str.zfill(2) + "h"
        piv = intra.pivot_table(index="heure", columns="dow", values="weight")
        piv.columns = [jours[d][:3] for d in piv.columns]
        fig = px.imshow(piv, aspect="auto", color_continuous_scale="Blues",
                        labels=dict(x="Jour", y="Heure locale", color="Poids"))
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, width="stretch")
        st.caption("Double pic matin/soir visible. Heure locale (Europe/Paris).")

    st.subheader("Simulation de répartition")
    st.markdown("Visualisez comment **1 000 contacts mensuels** se répartissent sur la semaine et la journée :")
    contacts_m = 1000
    dow_df2 = C.table("profile_dow").set_index("dow")["weight"]
    dow_w_norm = dow_df2 / dow_df2.sum()
    intra2 = C.table("profile_intraday").copy()
    intra2["weight_norm"] = intra2.groupby("dow")["weight"].transform(lambda x: x / x.sum())
    sim = []
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    for dow_idx, dow_w in dow_w_norm.items():
        day_contacts = contacts_m * dow_w
        slots = intra2[intra2["dow"] == dow_idx].copy()
        slots["contacts_bucket"] = day_contacts * slots["weight_norm"]
        slots["heure"] = slots["slot_local"] * 15 // 60
        sim.append(slots[["dow", "heure", "contacts_bucket"]])
    sim_df = pd.concat(sim).groupby(["dow", "heure"])["contacts_bucket"].sum().reset_index()
    sim_df["jour"] = sim_df["dow"].map(lambda d: jours[d])
    fig3 = px.line(sim_df, x="heure", y="contacts_bucket", color="jour",
                   labels={"heure": "Heure locale", "contacts_bucket": "Contacts / bucket (moy.)"})
    fig3.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), legend_title="")
    st.plotly_chart(fig3, width="stretch")


# =============================================================================
# ④ Demande à 15 min
# =============================================================================
def render_demand_buckets():
    """④ Demande à 15 min — résultat de la chaîne forecast → buckets UTC."""
    from staffing.timespine import BUSINESS_TZ
    st.header("⏱️ ④ Demande à 15 min")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_demand_buckets()
    st.markdown(r"""
    Après application des profils, on obtient la **demande à la maille bucket** (UTC, 15 min).

    - `workload_hours` = contacts × AHT / 3600
    - La conversion heure locale → UTC peut créer des **décalages** selon le fuseau (ex. Manille ≠ Paris).
    """)
    month = C.month_selector()
    v = C.db_version()
    dem = C.demand(month, v)
    if dem.empty:
        st.warning("Aucune demande calculée pour ce mois.")
        return

    groups = sorted(dem["group_id"].unique())
    levels = sorted(dem["level"].unique())
    sel_group = st.multiselect("Groupe", groups, default=groups[:2] if len(groups) > 1 else groups)
    sel_level = st.selectbox("Level", levels, format_func=lambda l: f"Level {l}")

    sub = dem[(dem["level"] == sel_level)]
    if sel_group:
        sub = sub[sub["group_id"].isin(sel_group)]
    if sub.empty:
        st.info("Aucune donnée pour ce filtre.")
        return

    sub = sub.copy()
    sub["local"] = sub["bucket_utc"].dt.tz_convert(BUSINESS_TZ)
    sub["date"] = sub["local"].dt.date
    sub["heure"] = sub["local"].dt.hour

    # check conservation des contacts
    pf = C.table("pax_forecast")
    crf = C.table("contact_rate_forecast")
    gmap = C.table("group_map")
    base = (pf[pf["month"] == month]
            .merge(crf[crf["month"] == month], on=["month", "region_id", "supply_id"])
            .merge(gmap[(gmap["month"] == month) & (gmap["active"] == 1)]
                   [["region_id", "supply_id", "group_id"]], on=["region_id", "supply_id"]))
    expected_total = (base["pax"] * base["contact_rate"]).sum()
    actual_total = dem["contacts"].sum()

    C.kpi_row([
        ("Contacts (tous groupes)", f"{actual_total:,.0f}"),
        ("Contacts attendus (PAX×CR)", f"{expected_total:,.0f}"),
        ("Conservation", f"{100 * actual_total / expected_total:.2f} %" if expected_total > 0 else "—"),
        ("Workload total (h)", f"{dem['workload_hours'].sum():,.0f} h"),
    ])

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Charge horaire sur le mois (Paris)")
        hourly = sub.groupby(["date", "heure"])["workload_hours"].sum().reset_index()
        hourly["ts"] = pd.to_datetime(hourly["date"].astype(str)) + pd.to_timedelta(hourly["heure"], unit="h")
        fig = px.line(hourly, x="ts", y="workload_hours",
                      labels={"ts": "Date", "workload_hours": "Workload (h/bucket 15min)"})
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("Heatmap time-of-day × date")
        piv_h = hourly.pivot_table(index="heure", columns="date", values="workload_hours",
                                   aggfunc="mean")
        fig2 = px.imshow(piv_h, aspect="auto", color_continuous_scale="Blues",
                         labels=dict(x="Jour", y="Heure (Paris)", color="Workload h"))
        fig2.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig2, width="stretch")

    st.subheader("Contribution par groupe")
    by_grp = sub.groupby("group_id")["contacts"].sum().reset_index()
    fig3 = px.pie(by_grp, values="contacts", names="group_id", hole=0.4)
    fig3.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig3, width="stretch")


# =============================================================================
# ⑤ Erlang C
# =============================================================================
def render_erlang():
    """⑤ Dimensionnement Erlang C — de la charge à l'ETP requis."""
    from staffing.timespine import BUSINESS_TZ
    st.header("📐 ⑤ Dimensionnement Erlang C")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_erlang()
    st.markdown(r"""
    **Erlang C** calcule, pour chaque bucket de 15 min, le **nombre minimal d'agents** pour tenir le SLA.

    Paramètres (dans *Éditer → Objectifs de service*) :
    - `sl_target` : ex. 95 % des appels traités en moins de `sl_seconds` = 120 s
    - `shrinkage` : part du temps où un agent n'est pas disponible (pauses, formation…)
    - `max_occupancy` : plafond d'occupation pour éviter la surcharge

    **ETP requis** = `agents_online / (1 − shrinkage)` — le gross-up shrinkage convertit les
    *agents au combiné* en *équivalents temps plein à planifier*.
    """)
    month = C.month_selector()
    v = C.db_version()
    req = C.required(month, v)
    if req.empty:
        st.warning("Aucune donnée de dimensionnement pour ce mois.")
        return

    sp = C.table("service_params")
    st.subheader("Paramètres de service")
    st.dataframe(sp, width="stretch", hide_index=True)

    sel_group = st.selectbox("Groupe", sorted(req["group_id"].unique()))
    sel_level = st.selectbox("Level", sorted(req["level"].unique()),
                             format_func=lambda l: f"Level {l}")
    sub = req[(req["group_id"] == sel_group) & (req["level"] == sel_level)].copy()
    if sub.empty:
        st.info("Aucune donnée pour ce filtre.")
        return
    sub["local"] = sub["bucket_utc"].dt.tz_convert(BUSINESS_TZ)

    C.kpi_row([
        ("ETP requis (pic)",    f"{sub['required_fte'].max():.1f}"),
        ("Agents online (pic)", f"{sub['agents_online'].max():.1f}"),
        ("Shrinkage gross-up",  f"× {1 / (1 - min(float(sub['shrinkage'].iloc[0]), 0.9999)):.2f}"),
        ("AHT moyen effectif",  f"{sub['aht_eff'].mean():.0f} s"),
    ])

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Agents online vs ETP requis")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sub["local"], y=sub["required_fte"],
                                 name="ETP requis (planif.)", fill="tozeroy",
                                 line=dict(color="#2c7fb8")))
        fig.add_trace(go.Scatter(x=sub["local"], y=sub["agents_online"],
                                 name="Agents online (combiné)",
                                 line=dict(color="#74c476", dash="dash")))
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(orientation="h", y=1.12), yaxis_title="ETP")
        st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("Charge brute vs Erlang C")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=sub["local"],
                                  y=sub["workload_hours"] / 0.25,
                                  name="Charge brute (occ. 100 %)",
                                  line=dict(color="#fdae61", dash="dot")))
        fig2.add_trace(go.Scatter(x=sub["local"], y=sub["required_fte"],
                                  name="ETP requis (avec marge SLA)",
                                  line=dict(color="#d7301f")))
        fig2.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                           legend=dict(orientation="h", y=1.12), yaxis_title="ETP")
        st.plotly_chart(fig2, width="stretch")
        st.caption("L'écart entre les deux courbes = **marge Erlang C** (agents pour absorber "
                   "les files d'attente et tenir le SLA).")


# =============================================================================
# ⑥ COUVERTURE & COÛTS
# =============================================================================
def _render_occupancy_by_team(matching: pd.DataFrame, supply_team: pd.DataFrame,
                               level: int, teams: pd.DataFrame,
                               service_params: pd.DataFrame):
    """IMP-2 : tableau occupation réelle vs cible max_occupancy, par équipe."""
    if supply_team.empty or matching.empty:
        st.info("Aucune allocation — 0 agents planifiés.")
        return

    # Cible max_occupancy pour ce level (depuis service_params)
    sp_level = service_params[service_params["level"] == level]
    target_occ = float(sp_level["max_occupancy"].iloc[0]) if not sp_level.empty else 0.92

    # Joindre matching (qui a real_occupancy par bucket×level×group_id) avec
    # supply_team (qui a team_id par bucket) sur (bucket_utc, level, group_id)
    sup_lv = supply_team[supply_team["level"] == level].copy()
    if sup_lv.empty:
        st.info("Aucune donnée d'allocation pour ce level.")
        return

    match_lv = matching[matching["level"] == level][
        ["bucket_utc", "level", "group_id", "real_occupancy"]
    ].copy()
    # Remplacer inf par NaN pour le calcul de moyenne pondérée
    match_lv["real_occupancy"] = match_lv["real_occupancy"].replace([np.inf, -np.inf], np.nan)

    # Joindre sur (bucket_utc, level, group_id)
    joined = sup_lv.merge(match_lv, on=["bucket_utc", "level", "group_id"], how="left")
    if joined.empty or "real_occupancy" not in joined.columns:
        st.info("Impossible de calculer l'occupation par équipe (données insuffisantes).")
        return

    # Moyenne de real_occupancy pondérée par agents — exclure les lignes NaN (occ infinie)
    # des deux côtés pour éviter que le dénominateur soit gonflé par des buckets sans capacité.
    valid = joined[joined["real_occupancy"].notna()].copy()
    valid["occ_x_agents"] = valid["real_occupancy"] * valid["agents"]
    if not valid.empty:
        g_agg = valid.groupby("team_id").agg(num=("occ_x_agents", "sum"), den=("agents", "sum"))
        g_agg["occupation_moyenne"] = g_agg["num"] / g_agg["den"].where(g_agg["den"] > 0)
        computed = g_agg[["occupation_moyenne"]].reset_index()
    else:
        computed = pd.DataFrame(columns=["team_id", "occupation_moyenne"])
    missing_ids = [t for t in joined["team_id"].unique() if t not in computed["team_id"].values]
    if missing_ids:
        computed = pd.concat(
            [computed, pd.DataFrame({"team_id": missing_ids, "occupation_moyenne": np.nan})],
            ignore_index=True,
        )
    team_occ = computed

    # Ajouter le label de l'équipe
    teams_lv = teams[teams["level"] == level][["team_id", "team_label"]].copy()
    team_occ = team_occ.merge(teams_lv, on="team_id", how="left")
    team_occ["cible_max_occupancy"] = target_occ
    team_occ["statut"] = team_occ["occupation_moyenne"].apply(
        lambda v: "Sur-cible" if (not np.isnan(v) and v > target_occ) else (
            "Dans la cible" if not np.isnan(v) else "N/A"))
    team_occ = team_occ.sort_values("occupation_moyenne", ascending=False)

    # Affichage tableau
    col1, col2 = st.columns([1, 1])
    with col1:
        disp = team_occ[["team_id", "team_label", "occupation_moyenne",
                          "cible_max_occupancy", "statut"]].copy()
        disp["occupation_moyenne"] = disp["occupation_moyenne"].apply(
            lambda v: f"{v:.1%}" if not np.isnan(v) else "N/A")
        disp["cible_max_occupancy"] = disp["cible_max_occupancy"].apply(lambda v: f"{v:.0%}")
        st.dataframe(disp.rename(columns={
            "team_id": "Équipe", "team_label": "Libellé",
            "occupation_moyenne": "Occupation moy.", "cible_max_occupancy": "Cible",
            "statut": "Statut",
        }), hide_index=True, use_container_width=True)
    with col2:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=team_occ["team_id"],
            y=team_occ["occupation_moyenne"],
            name="Occupation moy.",
            marker_color=[
                "#d7301f" if (not np.isnan(v) and v > target_occ) else "#2c7fb8"
                for v in team_occ["occupation_moyenne"]
            ],
        ))
        fig.add_hline(y=target_occ, line_dash="dash", line_color="#fdae61",
                      annotation_text=f"Cible {target_occ:.0%}", annotation_position="top right")
        fig.update_layout(
            height=280, margin=dict(l=10, r=10, t=10, b=10),
            yaxis_tickformat=".0%", yaxis_title="Occupation",
            xaxis_title="Équipe", showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)


def _local(df):
    df = df.copy()
    df["local"] = df["bucket_utc"].dt.tz_convert(BUSINESS_TZ)
    df["date"] = df["local"].dt.date
    return df


def render_coverage():
    st.header("🗓️ ⑥ Couverture & coûts")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_coverage()
    month = C.month_selector()

    with st.expander("⚙️ Optimiseur de répartition (coût minimal)", expanded=False):
        st.markdown("Le bouton calcule, par **programmation linéaire**, la répartition d'agents la "
                    "moins chère qui couvre l'ETP requis (Erlang C), **par groupe × jour de semaine**. "
                    "Résultat éditable plus bas.")
        cc = st.columns(3)
        pct = cc[0].slider("Couvrir le percentile de demande", 0.5, 1.0, 1.0, 0.05,
                           help="1.0 = couvre le pire jour du même jour de semaine.")
        lens = cc[1].multiselect(
            "Durées de shift (h)", [1, 2, 4, 6, 8], default=[6, 8],
            help="Blocs autorisés. Des shifts courts collent mieux à la demande (coût plus bas) "
                 "mais sont peu réalistes en exploitation.")
        if cc[2].button("🚀 (Ré)optimiser", type="primary"):
            lengths = tuple(int(h * 4) for h in (lens or [6, 8]))
            with st.spinner("Optimisation…"):
                lp_warns = C.run_optimizer(month, percentile=pct, shift_lengths=lengths)
            if lp_warns:
                st.warning(
                    "⚠️ Solveur non-optimal pour : " + ", ".join(lp_warns) +
                    " — capacité insuffisante ou fenêtres de disponibilité trop courtes. "
                    "L'allocation de ces jours peut être incomplète."
                )
            else:
                st.success("Allocation optimisée.")
            st.rerun()

    cov = C.coverage(month, C.db_version())
    matching, supply_team, demand = cov["matching"], cov["supply_team"], cov["demand"]
    if supply_team.empty:
        st.info("Aucune allocation pour ce mois. Ouvrez l'optimiseur ci-dessus et lancez « (Ré)optimiser ».")
        return

    # --- filtres -------------------------------------------------------------
    f = st.columns(6)
    level = f[0].selectbox("Level", sorted(matching["level"].unique()),
                           format_func=lambda l: f"Level {l}")
    all_groups = sorted(matching["group_id"].unique())
    sel_groups = f[1].multiselect("Groupe", all_groups, default=all_groups)
    dregions = f[2].multiselect("Région", sorted(demand["region_id"].unique()))
    dsupply = f[3].multiselect("Supply", sorted(demand["supply_id"].unique()))
    dtasks = f[4].multiselect("Type de tâche", sorted(demand["task_type_id"].unique()))
    gran = f[5].radio("Granularité", ["15 min", "Heure", "Jour"], horizontal=False)

    m = _local(matching[matching["level"] == level])
    if sel_groups:
        m = m[m["group_id"].isin(sel_groups)]
    days = sorted(m["date"].unique())
    if not days:
        st.info("Aucune donnée pour ces filtres.")
        return
    day_sel = st.select_slider("Jours affichés", options=days,
                               value=(days[0], days[-1]) if len(days) > 1 else (days[0], days[0]))
    m = m[(m["date"] >= day_sel[0]) & (m["date"] <= day_sel[1])]

    # KPI
    C.kpi_row([
        ("Coût total", C.eur(m["cost"].sum())),
        ("ETP requis (pic)", f"{m['required_fte'].max():.1f}"),
        ("Capacité (pic)", f"{m['effective_capacity'].max():.1f}"),
        ("Buckets sous-staffés", f"{100 * m['understaffed'].mean():.0f} %"),
        ("Occupation réelle (méd.)", f"{m.query('effective_capacity>0')['real_occupancy'].replace(np.inf, np.nan).median():.0%}"),
    ])

    # --- F1 heatmap ----------------------------------------------------------
    st.subheader("Heatmap de couverture")
    metric = st.selectbox("Métrique", ["coverage_ratio", "gap_fte", "real_occupancy"],
                          format_func=lambda x: {"coverage_ratio": "Taux de couverture",
                                                 "gap_fte": "Écart d'ETP", "real_occupancy": "Occupation réelle"}[x])
    hourly = st.checkbox("Agréger à l'heure (plus lisible)", value=True)
    match_filtered = matching[(matching["level"] == level)]
    if sel_groups:
        match_filtered = match_filtered[match_filtered["group_id"].isin(sel_groups)]
    piv = reporting.heatmap_pivot(match_filtered, level, metric, BUSINESS_TZ)
    piv = piv[[c for c in piv.columns if day_sel[0] <= c <= day_sel[1]]]
    if hourly:
        piv.index = [t[:2] + "h" for t in piv.index]
        piv = piv.groupby(level=0).mean()
        piv = piv.reindex(sorted(piv.index))
    z = piv.clip(0, 3) if metric == "coverage_ratio" else (piv.clip(0, 2) if metric == "real_occupancy" else piv)
    mid = {"coverage_ratio": 1.0, "gap_fte": 0.0, "real_occupancy": None}[metric]
    fig = px.imshow(z.replace([np.inf, -np.inf], np.nan), aspect="auto",
                    color_continuous_scale=DIV if metric != "real_occupancy" else "RdYlGn_r",
                    color_continuous_midpoint=mid, labels=dict(x="Jour", y=f"Heure ({BUSINESS_TZ})", color=""))
    fig.update_layout(height=460, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, width="stretch")

    # --- F2 requis vs capacité (granularité) ---------------------------------
    st.subheader("ETP requis vs capacité disponible")
    freq = {"15 min": "15min", "Heure": "h", "Jour": "D"}[gran]
    ts = m.set_index("local")
    agg = ts[["required_fte", "effective_capacity", "workload_hours"]].resample(freq).mean()
    agg["charge_brute_fte"] = ts["workload_hours"].resample(freq).mean() / BUCKET_HOURS
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=agg.index, y=agg["effective_capacity"], name="Capacité", fill="tozeroy",
                              line=dict(color="#9ecae1")))
    fig2.add_trace(go.Scatter(x=agg.index, y=agg["required_fte"], name="ETP requis (Erlang C)",
                              line=dict(color="#d7301f", width=2)))
    fig2.add_trace(go.Scatter(x=agg.index, y=agg["charge_brute_fte"], name="Charge brute (occ. 100%)",
                              line=dict(color="#fdae61", width=1, dash="dot")))
    fig2.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.12),
                       yaxis_title="ETP")
    st.plotly_chart(fig2, width="stretch")
    st.caption("L'écart entre la charge brute (occ. 100 %) et l'ETP requis = **marge de sécurité Erlang C** "
               "(agents en plus pour tenir le SLA).")

    # --- FTE par équipe / jour (bâton divisé) + coût/jour --------------------
    sup_t = _local(supply_team[supply_team["level"] == level])
    if sel_groups:
        sup_t = sup_t[sup_t["group_id"].isin(sel_groups)]
    sup_t = sup_t[(sup_t["date"] >= day_sel[0]) & (sup_t["date"] <= day_sel[1])]
    if dregions or dsupply or dtasks:
        st.caption("ℹ️ Les filtres région/supply/tâche s'appliquent à la **demande** (graphes ci-dessous).")
    c5, c6 = st.columns(2)
    with c5:
        st.subheader("Agents-heures par équipe et par jour")
        per = sup_t.groupby(["date", "team_id"])["agents"].sum().reset_index()
        per["agent_h"] = per["agents"] * BUCKET_HOURS
        fig3 = px.bar(per, x="date", y="agent_h", color="team_id")
        fig3.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend_title="", yaxis_title="Agents·h")
        st.plotly_chart(fig3, width="stretch")
    with c6:
        st.subheader("Coût par jour")
        cpd = sup_t.groupby("date")["cost"].sum().reset_index()
        fig4 = px.bar(cpd, x="date", y="cost", color_discrete_sequence=["#2c7fb8"])
        fig4.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Coût (€)")
        st.plotly_chart(fig4, width="stretch")

    # --- demande filtrée (contribution) --------------------------------------
    dd = demand[demand["level"] == level].copy()
    if sel_groups:
        dd = dd[dd["group_id"].isin(sel_groups)]
    if dregions:
        dd = dd[dd["region_id"].isin(dregions)]
    if dsupply:
        dd = dd[dd["supply_id"].isin(dsupply)]
    if dtasks:
        dd = dd[dd["task_type_id"].isin(dtasks)]
    st.subheader("Contribution à la charge (demande filtrée)")
    contrib = dd.groupby(["region_id", "supply_id", "task_type_id"])["workload_hours"].sum().reset_index()
    fig5 = px.bar(contrib.sort_values("workload_hours"), x="workload_hours", y="region_id", color="task_type_id",
                  orientation="h", hover_data=["supply_id"])
    fig5.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="Heures de charge", legend_title="")
    st.plotly_chart(fig5, width="stretch")

    # --- F3 trous ------------------------------------------------------------
    st.subheader("Trous de couverture prioritaires")
    gaps = reporting.gap_intervals(match_filtered, top=15)
    if gaps.empty:
        st.success("Aucun trou de couverture sur ce level.")
    else:
        g = gaps.copy()
        g["début"] = g["start_utc"].dt.tz_convert(BUSINESS_TZ).dt.strftime("%a %d/%m %H:%M")
        g["fin"] = g["end_utc"].dt.tz_convert(BUSINESS_TZ).dt.strftime("%a %d/%m %H:%M")
        st.dataframe(g[["début", "fin", "duration_h", "max_deficit_fte", "total_deficit_fte_hours"]]
                     .rename(columns={"duration_h": "durée (h)", "max_deficit_fte": "déficit max",
                                      "total_deficit_fte_hours": "déficit cumulé (ETP·h)"}),
                     width="stretch", hide_index=True)

    # --- IMP-2 : Occupation réelle vs cible par équipe -----------------------
    st.subheader("Occupation réelle vs cible par équipe")
    st.caption("Taux d'occupation moyen du mois par équipe, comparé à la cible max_occupancy (paramètres SLA).")
    _render_occupancy_by_team(matching, supply_team, level, C.table("team"),
                              db.read_table("service_params"))

    # --- édition de l'allocation ---------------------------------------------
    st.subheader("✏️ Répartition d'agents (éditable)")
    st.caption("Ajustez le nb d'agents par équipe et par créneau (UTC) pour un jour de semaine. "
               "« Enregistrer » recalcule la couverture sans relancer l'optimiseur.")
    dow = st.selectbox("Jour de semaine", list(range(7)),
                       format_func=lambda d: ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"][d])
    alloc = C.table("allocation")
    teams_lvl = C.table("team")
    teams_lvl = teams_lvl[teams_lvl["level"] == level]["team_id"].tolist()
    sub = alloc[(alloc["dow"] == dow) & (alloc["team_id"].isin(teams_lvl))]
    grid = (sub.pivot_table(index="slot_utc", columns="team_id", values="agents", fill_value=0)
            .reindex(range(96), fill_value=0).reindex(columns=teams_lvl, fill_value=0).reset_index())
    grid["UTC"] = grid["slot_utc"].map(lambda s: f"{s * 15 // 60:02d}:{s * 15 % 60:02d}")
    grid = grid[["UTC"] + teams_lvl]
    edited = st.data_editor(grid, width="stretch", hide_index=True, height=300, key=f"alloc_{dow}_{level}",
                            disabled=["UTC"])
    if st.button("💾 Enregistrer la répartition", type="primary"):
        e = edited.copy()
        # Reconstituer slot_utc depuis la colonne 'UTC' (format 'HH:MM') pour
        # résister au tri du tableau par l'utilisateur (CRIT-3).
        # Si la colonne 'UTC' a été supprimée par data_editor, on la recalcule
        # depuis l'index d'origine du grid avant le melt.
        if "UTC" not in e.columns:
            e = e.reset_index(drop=True)
            e["UTC"] = e.index.map(lambda i: f"{i * 15 // 60:02d}:{i * 15 % 60:02d}")
        e["slot_utc"] = e["UTC"].map(lambda s: int(s[:2]) * 4 + int(s[3:]) // 15)
        long = e.melt(id_vars="slot_utc", value_vars=teams_lvl, var_name="team_id", value_name="agents")
        long = long[long["agents"] > 0]
        long["dow"] = dow
        keep = alloc[~((alloc["dow"] == dow) & (alloc["team_id"].isin(teams_lvl)))]
        C.save_table("allocation", pd.concat([keep, long[["dow", "slot_utc", "team_id", "agents"]]], ignore_index=True))
        st.success("Répartition enregistrée.")
        st.rerun()


# =============================================================================
# DEMANDE — Réel vs Forecast
# =============================================================================
def render_forecast():
    st.header("📊 Demande — Réel vs Forecast")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_forecast()
    st.markdown(
        "On compare les **PAX** et les **tâches** *réels* (ingérés par CSV, à titre indicatif) "
        "au *forecast* (PAX prévus × **contact rate** retenu). Objectif : voir si un écart vient "
        "des **PAX** ou du **contact rate**, et **ajuster le contact rate** en direct.")
    st.caption("Le forecast est tracé sur **tous les mois** ; le réel s'arrête au dernier mois connu "
               "(au-delà = forecast seul).")

    av = C.actuals(C.db_version())
    regions = C.table("region")
    reg = st.selectbox("Région (pays)", sorted(av["region_id"].unique()),
                       format_func=lambda r: f"{r} — {regions.set_index('region_id').loc[r, 'region_label']}")
    sup = st.selectbox("Supply", sorted(av[av["region_id"] == reg]["supply_id"].unique()))
    sel = av[(av["region_id"] == reg) & (av["supply_id"] == sup)].copy()

    # --- décomposition de l'écart de volume (PAX vs contact rate) ------------
    sel["ecart_taches"] = sel["tasks_real"] - sel["tasks_forecast"]
    sel["effet_pax"] = (sel["pax_real"] - sel["pax_forecast"]) * sel["cr_forecast"]
    sel["effet_taux"] = sel["pax_forecast"] * (sel["cr_real"] - sel["cr_forecast"])
    sel["interaction"] = (sel["pax_real"] - sel["pax_forecast"]) * (sel["cr_real"] - sel["cr_forecast"])

    real_only = sel.dropna(subset=["pax_real"])
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("PAX : réel vs forecast")
        d = sel.groupby("month").agg(reel=("pax_real", "max"), forecast=("pax_forecast", "max")).reset_index()
        fig = px.line(d, x="month", y=["reel", "forecast"], markers=True,
                      color_discrete_map={"reel": "#d7301f", "forecast": "#2c7fb8"})
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="PAX", legend_title="")
        st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("Décomposition de l'écart de tâches")
        if not real_only.empty:
            dd = real_only.groupby("month")[["effet_pax", "effet_taux", "interaction"]].sum().reset_index()
            fig = px.bar(dd, x="month", y=["effet_pax", "effet_taux", "interaction"],
                         color_discrete_map={"effet_pax": "#2c7fb8", "effet_taux": "#fdae61", "interaction": "#999999"})
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                              yaxis_title="Δ tâches vs forecast", legend_title="")
            st.plotly_chart(fig, width="stretch")
            st.caption("Bleu = effet PAX · Orange = effet contact rate · Gris = interaction.")

    c3, c4 = st.columns(2)
    with c3:
        st.subheader("Contact rate : réel vs forecast")
        d = sel.melt(id_vars=["month", "task_type_id"], value_vars=["cr_real", "cr_forecast"],
                     var_name="type", value_name="contact_rate")
        fig = px.line(d, x="month", y="contact_rate", color="task_type_id", line_dash="type", markers=True)
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend_title="")
        st.plotly_chart(fig, width="stretch")
    with c4:
        st.subheader("Volume de tâches : réel vs forecast")
        d = sel.melt(id_vars=["month", "task_type_id"], value_vars=["tasks_real", "tasks_forecast"],
                     var_name="type", value_name="tasks")
        fig = px.line(d, x="month", y="tasks", color="task_type_id", line_dash="type", markers=True)
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend_title="")
        st.plotly_chart(fig, width="stretch")

    # --- édition du contact rate forecast (mois × task) ----------------------
    st.subheader(f"✏️ Contact rate forecast éditable — {reg} × {sup}")
    st.caption("Modifiez les valeurs puis « Enregistrer ». Le dimensionnement se recalcule automatiquement.")
    crf = C.table("contact_rate_forecast")
    sub = crf[(crf["region_id"] == reg) & (crf["supply_id"] == sup)]
    pivot = sub.pivot_table(index="month", columns="task_type_id", values="contact_rate").reset_index()
    edited = st.data_editor(pivot, width="stretch", hide_index=True, key=f"cr_{reg}_{sup}",
                            column_config={c: st.column_config.NumberColumn(format="%.4f")
                                           for c in pivot.columns if c != "month"})
    if st.button("💾 Enregistrer le contact rate", type="primary"):
        long = edited.melt(id_vars="month", var_name="task_type_id", value_name="contact_rate")
        long["region_id"], long["supply_id"] = reg, sup
        keep = crf[~((crf["region_id"] == reg) & (crf["supply_id"] == sup))]
        C.save_table("contact_rate_forecast", pd.concat([keep, long[crf.columns]], ignore_index=True))
        st.success("Contact rate mis à jour.")
        st.rerun()


# =============================================================================
# EXPLORATEUR DE DONNÉES (type BI)
# =============================================================================
COMPUTED = {
    "demand": ("Demande forecast (bucket)", ["bucket_utc", "level", "task_type_id", "group_id", "region_id", "supply_id"],
               ["contacts", "workload_hours"]),
    "required": ("ETP requis Erlang C (bucket×level×groupe)", ["bucket_utc", "level", "group_id"],
                 ["contacts", "workload_hours", "agents_online", "required_fte", "aht_eff"]),
    "coverage": ("Couverture (bucket×level×groupe)", ["bucket_utc", "level", "group_id"],
                 ["required_fte", "effective_capacity", "agents", "gap_fte", "cost", "coverage_ratio", "real_occupancy"]),
    "supply_team": ("Capacité par équipe (bucket)", ["bucket_utc", "team_id", "group_id", "level"],
                    ["agents", "effective_capacity", "cost"]),
    "actuals": ("Réel vs forecast (mois)", ["month", "region_id", "supply_id", "group_id", "task_type_id"],
                ["pax_real", "pax_forecast", "tasks_real", "tasks_forecast", "cr_real", "cr_forecast"]),
}


def _explorer_source(name, month):
    if name in db.TABLES:
        return C.table(name)
    if name == "demand":
        return C.demand(month, C.db_version())
    if name == "required":
        return C.required(month, C.db_version())
    if name == "coverage":
        return C.coverage(month, C.db_version())["matching"]
    if name == "supply_team":
        return C.coverage(month, C.db_version())["supply_team"]
    if name == "actuals":
        return C.actuals(C.db_version())
    return pd.DataFrame()


def render_explorer():
    st.header("🔎 Explorateur de données (pivot)")
    st.markdown("Visualisez **n'importe quelle table** du modèle (entrée ou calculée) et **pivotez-la** "
                "selon les dimensions présentes — comme dans un outil BI.")

    opts = {n: s.label for n, s in db.TABLES.items()} | {n: v[0] for n, v in COMPUTED.items()}
    groups = {}
    for n, s in db.TABLES.items():
        groups.setdefault(s.group, []).append(n)
    groups.setdefault("Calculé (moteur)", list(COMPUTED))
    flat = [n for g in groups.values() for n in g]
    name = st.selectbox("Table", flat, format_func=lambda n: f"{opts[n]}  ·  [{n}]")

    month = None
    if name in COMPUTED and name != "actuals":
        month = C.month_selector(key="exp_month")
    df = _explorer_source(name, month)
    if df.empty:
        st.warning("Table vide (lancez l'optimiseur pour les tables de couverture).")
        return

    if name in db.TABLES:
        dims, measures = db.TABLES[name].dims, db.TABLES[name].measures
        st.caption(db.TABLES[name].note)
    else:
        _, dims, measures = COMPUTED[name]
    dims = [c for c in dims if c in df.columns]
    measures = [c for c in measures if c in df.columns] or [c for c in df.columns if c not in dims]

    st.markdown("**Constructeur de pivot**")
    p = st.columns(4)
    rows = p[0].multiselect("Lignes", dims, default=dims[:1])
    cols = p[1].selectbox("Colonnes", ["(aucune)"] + dims, index=0)
    val = p[2].selectbox("Valeur", measures) if measures else None
    aggf = p[3].selectbox("Agrégation", ["sum", "mean", "max", "min", "count"])

    tab_pivot, tab_raw = st.tabs(["Pivot", "Données brutes"])
    with tab_pivot:
        if not measures or val is None:
            st.info("Aucune mesure disponible pour cette table.")
        elif rows:
            fill_val = 0 if pd.api.types.is_numeric_dtype(df[val]) else ""
            piv = pd.pivot_table(df, index=rows, columns=None if cols == "(aucune)" else cols,
                                 values=val, aggfunc=aggf, fill_value=fill_val)
            st.dataframe(piv, width="stretch")
            st.download_button("⬇️ Télécharger (CSV)", piv.to_csv().encode(), f"{name}_pivot.csv")
        else:
            st.info("Choisissez au moins une dimension en ligne.")
    with tab_raw:
        st.dataframe(df.head(5000), width="stretch", hide_index=True)
        st.caption(f"{len(df):,} lignes — {', '.join(df.columns)}")


# =============================================================================
# ÉDITION DES RÉFÉRENTIELS
# =============================================================================
# NOTE : _group_map_editor() et _group_map_editor_regions() sont quasi-identiques
# (même table, même pivot) mais diffèrent sur num_rows et la sélection du group_id.
# À fusionner en une seule fonction paramétrée si les deux évoluent ensemble.
def _group_map_editor():
    """Pivot éditable du mapping group : combos (Region×Supply) × mois, actif/inactif."""
    gm = C.table("group_map")
    grp = C.table("group")
    all_groups = sorted(grp["group_id"].tolist()) if not grp.empty else sorted(gm["group_id"].unique())
    cols_order = list(gm.columns)
    months = sorted(gm["month"].unique())
    piv = (gm.pivot_table(index=["region_id", "supply_id"], columns="month",
                          values="active", fill_value=0)
           .reindex(columns=months, fill_value=0).reset_index())
    # add group_id column (pick from current data — one group per region×supply)
    group_by_rs = gm.drop_duplicates(["region_id", "supply_id"]).set_index(["region_id", "supply_id"])["group_id"].to_dict()
    piv["group_id"] = piv.apply(lambda r: group_by_rs.get((r["region_id"], r["supply_id"]), ""), axis=1)
    # reorder columns: region_id, supply_id, group_id, then months
    piv = piv[["region_id", "supply_id", "group_id"] + months]
    for m in months:
        piv[m] = piv[m].astype(bool)
    st.caption("Cochez les mois où chaque **(Region×Supply)** est actif et renseignez le **groupe** associé.")
    colcfg = {m: st.column_config.CheckboxColumn(m[2:]) for m in months}
    colcfg["group_id"] = st.column_config.SelectboxColumn("Groupe", options=all_groups)
    colcfg["region_id"] = st.column_config.TextColumn("Région")
    colcfg["supply_id"] = st.column_config.TextColumn("Supply")
    edited = st.data_editor(piv, width="stretch", hide_index=True, num_rows="dynamic",
                            column_config=colcfg, key="gm_pivot")
    if st.button("💾 Enregistrer le mapping group", type="primary"):
        e = edited.dropna(subset=["region_id", "supply_id"])
        long = e.melt(id_vars=["region_id", "supply_id", "group_id"], value_vars=months,
                      var_name="month", value_name="active")
        long["active"] = long["active"].fillna(False).astype(int)
        C.save_table("group_map", long[cols_order])
        st.success("Mapping group enregistré.")
        st.rerun()


def _import_section():
    """Import mensuel par fichier CSV (upsert par clé) + ré-ingestion du dossier."""
    from staffing import ingest
    st.caption("Workflow mensuel : préparez un CSV par table (depuis le **modèle** ci-dessous), "
               "puis **uploadez-le** ici ou déposez-le dans `data/incoming/` et poussez sur GitHub. "
               "L'import **remplace les lignes du même mois** et conserve le reste (upsert par clé).")
    monthly = ["pax_real", "tasks_real", "pax_forecast", "contact_rate_forecast", "group_map"]
    tbl = st.selectbox("Table cible", monthly + [n for n in db.EDITABLE if n not in monthly],
                       format_func=lambda n: db.TABLES[n].label, key="imp_tbl")
    keys = db.TABLES[tbl].keys
    st.caption(f"Clé d'upsert : **{' × '.join(keys)}** — colonnes attendues : "
               f"`{', '.join(ingest.expected_columns(db.TABLES[tbl]))}`")
    c1, c2 = st.columns([1, 2])
    with c1:
        st.download_button("⬇️ Modèle CSV (données actuelles)",
                           C.table(tbl).to_csv(index=False).encode(),
                           f"{tbl}.csv", key="imp_tmpl")
    with c2:
        up = st.file_uploader(f"Uploader un CSV pour « {db.TABLES[tbl].label} »", type="csv", key="imp_up")
        if up is not None and st.button("📥 Importer ce fichier", type="primary", key="imp_btn"):
            try:
                r = C.ingest_upload(tbl, up, label=up.name)
                st.success(f"Importé : {r['rows_in']} lignes (remplacées {r['rows_replaced']}, "
                           f"total {r['total_after']}).")
                for col, n in (r.get("coerce_warnings") or {}).items():
                    st.warning(f"⚠️ {n} valeur(s) non numérique(s) ignorées (→ NaN) dans la colonne « {col} ».")
                st.rerun()
            except Exception as exc:
                st.error(f"Échec de l'import : {exc}")
    st.divider()
    if st.button("🔄 Ré-ingérer tout le dossier data/incoming/", key="imp_dir"):
        reports = C.ingest_incoming()
        if not reports:
            st.info("Aucun fichier .csv dans data/incoming/.")
        else:
            st.success("Ingestion terminée :")
            st.dataframe(pd.DataFrame(reports), width="stretch", hide_index=True)
            st.rerun()


# =============================================================================
# RAPPORT MENSUEL (IMP-1)
# =============================================================================
def _build_monthly_report(month: str, prev_month: str | None = None) -> io.BytesIO:
    """Génère un rapport mensuel Excel (3 onglets) en mémoire."""
    cov = C.coverage(month, C.db_version())
    matching = cov["matching"]
    supply_team = cov["supply_team"]
    demand = cov["demand"]

    # Ajouter sourcing depuis la table team (requis par cost_fte_summary)
    teams = C.table("team")
    supply_with_sourcing = (
        supply_team.merge(teams[["team_id", "sourcing"]], on="team_id", how="left")
        if not supply_team.empty
        else supply_team
    )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:

        # --- Onglet 1 : Synthèse -----------------------------------------------
        if not supply_team.empty and not matching.empty:
            summary = reporting.cost_fte_summary(demand, supply_with_sourcing, matching)
            by_level = summary["by_level"]
            req_by_group = summary["required_by_group"]
            cost_by_team = summary["cost_by_team"]

            # Coût total par groupe et par level (depuis matching)
            cost_group = (
                matching.groupby(["level", "group_id"])
                .agg(
                    total_cost=("cost", "sum"),
                    fte_requis_pic=("required_fte", "max"),
                    fte_capacite_pic=("effective_capacity", "max"),
                    pct_sous_staffe=(
                        "understaffed",
                        lambda s: f"{100 * s.mean():.1f}%",
                    ),
                )
                .reset_index()
                .sort_values(["level", "total_cost"], ascending=[True, False])
            )
            _sanitize_excel(cost_group).to_excel(writer, sheet_name="Synthèse", index=False, startrow=0)

            # ETP requis vs capacité (par level)
            by_level_out = by_level[
                ["level", "total_cost", "required_fte_peak", "capacity_peak",
                 "required_fte_avg", "capacity_avg", "hours_understaffed"]
            ]
            startrow = len(cost_group) + 3
            _sanitize_excel(by_level_out).to_excel(writer, sheet_name="Synthèse", index=False, startrow=startrow)
        else:
            # Aucune allocation : on écrit un onglet vide avec message
            pd.DataFrame({"info": ["Aucune allocation pour ce mois. Lancez l'optimiseur."]}).to_excel(
                writer, sheet_name="Synthèse", index=False)

        # --- Onglet 2 : Comparatif M vs M-1 ------------------------------------
        if prev_month:
            try:
                cov_prev = C.coverage(prev_month, C.db_version())
                matching_prev = cov_prev["matching"]
                demand_prev = cov_prev["demand"]

                def _kpis(m, d):
                    return {
                        "total_cost": m["cost"].sum() if not m.empty else 0.0,
                        "fte_requis_peak": m["required_fte"].max() if not m.empty else 0.0,
                        "contacts": d["contacts"].sum() if not d.empty else 0.0,
                    }

                kpi_curr = _kpis(matching, demand)
                kpi_prev = _kpis(matching_prev, demand_prev)

                comp = pd.DataFrame([
                    {"KPI": "Coût total (€)", prev_month: kpi_prev["total_cost"],
                     month: kpi_curr["total_cost"],
                     "Delta": kpi_curr["total_cost"] - kpi_prev["total_cost"]},
                    {"KPI": "ETP requis pic", prev_month: kpi_prev["fte_requis_peak"],
                     month: kpi_curr["fte_requis_peak"],
                     "Delta": kpi_curr["fte_requis_peak"] - kpi_prev["fte_requis_peak"]},
                    {"KPI": "Contacts totaux", prev_month: kpi_prev["contacts"],
                     month: kpi_curr["contacts"],
                     "Delta": kpi_curr["contacts"] - kpi_prev["contacts"]},
                ])
                _sanitize_excel(comp).to_excel(writer, sheet_name="Comparatif M vs M-1", index=False)
            except Exception:
                pd.DataFrame({"info": [f"Données indisponibles pour {prev_month}."]}).to_excel(
                    writer, sheet_name="Comparatif M vs M-1", index=False)
        else:
            pd.DataFrame({"info": ["Aucun mois précédent disponible."]}).to_excel(
                writer, sheet_name="Comparatif M vs M-1", index=False)

        # --- Onglet 3 : Gaps prioritaires --------------------------------------
        if not matching.empty:
            gaps = reporting.gap_intervals(matching, top=20)
            if gaps.empty:
                pd.DataFrame({"info": ["Aucun trou de couverture."]}).to_excel(
                    writer, sheet_name="Gaps prioritaires", index=False)
            else:
                gaps_out = gaps.copy()
                # Convertir les colonnes datetime en chaînes pour Excel
                for col in ["start_utc", "end_utc"]:
                    if col in gaps_out.columns:
                        gaps_out[col] = gaps_out[col].dt.tz_convert(BUSINESS_TZ).dt.strftime("%Y-%m-%d %H:%M")
                _sanitize_excel(gaps_out).to_excel(writer, sheet_name="Gaps prioritaires", index=False)
        else:
            pd.DataFrame({"info": ["Aucune donnée de couverture disponible."]}).to_excel(
                writer, sheet_name="Gaps prioritaires", index=False)

    buf.seek(0)
    return buf


def render_monthly_report():
    """Rapport mensuel synthétique exportable (IMP-1)."""
    st.header("📊 Rapport mensuel")
    with st.expander("📖 Documentation & formules détaillées", expanded=False):
        _doc_monthly_report()
    st.markdown(
        "Agrégez les KPI clés pour un mois sélectionné et exportez-les en **Excel** (3 onglets : "
        "Synthèse · Comparatif M vs M-1 · Gaps prioritaires)."
    )

    all_months = C.months()
    month = C.month_selector(key="report_month")
    month_idx = all_months.index(month) if month in all_months else 0
    prev_month = all_months[month_idx - 1] if month_idx > 0 else None

    # Aperçu rapide des KPI
    cov = C.coverage(month, C.db_version())
    matching = cov["matching"]

    if not matching.empty:
        kpis = reporting.summary_kpis(matching)
        st.subheader(f"KPI — {month}")
        st.dataframe(kpis.style.format({
            "total_cost": "{:,.0f} €",
            "required_fte_peak": "{:.1f}",
            "capacity_peak": "{:.1f}",
            "pct_buckets_understaffed": "{:.1f}%",
            "hours_understaffed": "{:.1f} h",
            "real_occupancy_p50": "{:.1%}",
            "real_occupancy_p95": "{:.1%}",
        }), hide_index=True, use_container_width=True)
    else:
        st.info("Aucune allocation pour ce mois. Le rapport contiendra uniquement les données de demande.")

    st.divider()
    st.subheader("Export Excel")
    if prev_month:
        st.caption(f"Le comparatif inclura le mois précédent : **{prev_month}**.")
    else:
        st.caption("Pas de mois précédent disponible pour le comparatif.")

    with st.spinner("Génération du rapport Excel…"):
        excel_buf = _build_monthly_report(month, prev_month)

    st.download_button(
        label="⬇️ Télécharger le rapport mensuel (Excel)",
        data=excel_buf,
        file_name=f"rapport_staffing_{month}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

# =============================================================================
# GLOSSAIRE
# =============================================================================

GLOSSARY: dict[str, tuple[str, str]] = {
    "AHT": (
        "Average Handling Time — Temps moyen de traitement",
        "Durée moyenne **en secondes** pour traiter un contact (appel entrant, email, chat, remboursement…). "
        "Formule : **Workload (h) = contacts × AHT / 3 600**. "
        "Un AHT +10 % → charge +10 % → ~10 % d'agents supplémentaires. "
        "Paramétrable par type de tâche ; surcharge optionnelle par groupe commercial.",
    ),
    "ETP / FTE": (
        "Équivalent Temps Plein / Full-Time Equivalent",
        "Nombre d'agents à temps plein nécessaires sur un créneau de 15 min. "
        "**ETP requis (Erlang C)** = nb agents pour tenir le SLA. "
        "**ETP planifié** = ETP_Erlang / (1 − shrinkage) — majoration pour les indisponibilités. "
        "**ETP réel** = agents alloués × productivité de l'équipe.",
    ),
    "PAX": (
        "Passagers transportés",
        "Volume de passagers par région × supply × mois. "
        "C'est la **variable motrice de la demande** : contacts = PAX × contact_rate. "
        "Disponible sous deux formes : *pax_real* (réalisé, indicatif) et *pax_forecast* (base du dimensionnement).",
    ),
    "Contact Rate": (
        "Taux de contact (CR)",
        "Nombre de contacts par passager. "
        "Renseigné par mois × région × supply × type de tâche. "
        "Formule : **CR = tâches_réelles / PAX_réels** (calculé automatiquement sur la page Réel vs Forecast). "
        "Un CR qui monte signale un problème de qualité ou un événement exceptionnel.",
    ),
    "Erlang C": (
        "Formule Erlang C — modèle de file d'attente M/M/c",
        "Modèle mathématique qui calcule le nombre minimum d'agents pour tenir un SLA, "
        "à partir de : intensité du trafic (Erlangs = λ × AHT), nombre d'agents, seuil de réponse. "
        "Hypothèses : arrivées de Poisson (aléatoires), temps de service exponentiel, file infinie. "
        "**Limite** : ne capture pas les rappels, les abandons ou les effets de débordement inter-groupes.",
    ),
    "SLA": (
        "Service Level Agreement — Objectif de niveau de service",
        "Engagement de qualité : ex. *« 95 % des contacts répondus en ≤ 120 secondes »*. "
        "Paramètres dans la table **Objectifs SLA** : `sl_target` (fraction, ex. 0.95) et "
        "`sl_seconds` (délai, ex. 120). Configurable par level et optionnellement par groupe commercial.",
    ),
    "Shrinkage": (
        "Indisponibilité agents (shrinkage)",
        "Part du temps de présence d'un agent **non consacrée aux contacts** : "
        "pauses réglementaires, formations, réunions, absences imprévues, latences système. "
        "Valeur typique : **25–35 %**. "
        "**ETP planifié = ETP_Erlang / (1 − shrinkage)** — si shrinkage = 0.30 et Erlang = 10, il faut planifier ~14.3 agents.",
    ),
    "Occupancy": (
        "Taux d'occupation",
        "Ratio temps_de_traitement / temps_de_présence. "
        "**< 75 %** : agents souvent inactifs (coût élevé). "
        "**75–85 %** : zone optimale. "
        "**> 85 %** : files d'attente s'allongent, SLA se dégrade. "
        "Configurable via `max_occupancy` dans les Objectifs SLA.",
    ),
    "DOW": (
        "Day Of Week — Jour de la semaine",
        "Encodage numérique : **0 = Lundi, 1 = Mardi, …, 6 = Dimanche**. "
        "Le modèle calcule un plan type par DOW, puis le réutilise sur tous les jours équivalents du mois. "
        "Les **jours fériés** sont traités comme des dimanches (poids de contact réduit).",
    ),
    "Bucket UTC": (
        "Créneau de 15 minutes exprimé en heure UTC",
        "Granularité de base : **96 buckets de 15 min** par journée (indices 0 à 95). "
        "Tout est stocké et calculé en UTC pour uniformiser les fuseaux horaires. "
        "Conversion en heure locale (ex. Paris UTC+1 / UTC+2) uniquement à l'affichage. "
        "**slot_utc** = heure_UTC × 4 + minutes // 15.",
    ),
    "Level 1 / L1": (
        "Support de premier niveau",
        "Contacts entrants traités par les **prestataires BPO** (externalisés). "
        "Gèrent les demandes courantes (informations, modifications simples). "
        "Identifié dans la table Équipes par `level = 1` et `sourcing = 'external'`.",
    ),
    "Level 2 / L2": (
        "Support de second niveau — Escalades",
        "Contacts complexes **transférés** depuis le L1 vers les équipes internes. "
        "Réclamations, remboursements, litiges. "
        "Identifié par `level = 2` et `sourcing = 'internal'`. "
        "Dimensionné séparément du L1 (SLA et AHT distincts).",
    ),
    "BPO": (
        "Business Process Outsourcing",
        "Prestataire externe qui prend en charge tout ou partie du traitement des contacts "
        "(ex. EXT_MADRID, EXT_LISBON, EXT_DGKAR). "
        "Facturation à l'heure d'agent × nombre d'agents. "
        "Coût défini par `hourly_cost` dans la table Équipes.",
    ),
    "Groupe commercial": (
        "Segment de clientèle / canal de vente",
        "Partitionne la demande par canal : **DIRECT_AIR**, **OTA**, **RAIL**, **BUS**, **FERRY**. "
        "Chaque groupe a sa propre demande, son SLA et ses équipes dédiées. "
        "L'optimiseur alloue la capacité **indépendamment** par groupe (pas de pooling). "
        "Routage : region×supply → groupe via `group_map`.",
    ),
    "OTA": (
        "Online Travel Agency — Agence de voyage en ligne",
        "Canal de distribution indirect : Booking.com, Expedia, Amadeus GDS… "
        "Segment distinct de la vente directe (DIRECT_AIR). "
        "Généralement un contact_rate plus élevé (intermédiaires génèrent plus de questions).",
    ),
    "Optimiseur IP": (
        "Programmation Linéaire en Nombres Entiers (Integer Programming)",
        "Algorithme (solver **CBC** via PuLP) qui minimise le coût total en heures×agents "
        "sous contraintes : capacité ≥ ETP requis, disponibilités horaires respectées, plafonds `max_agents`. "
        "Résout **un problème par jour de semaine** — un seul IP global avec variables partagées entre groupes "
        "(évite la sur-allocation d'équipes multi-groupes).",
    ),
    "Productivité": (
        "Facteur de productivité équipe",
        "Coefficient entre 0 et 1. **Capacité effective = agents × productivité**. "
        "Ex. 0.85 = l'équipe traite 85 % d'un ETP théorique (formation en cours, langue secondaire, "
        "procédures plus longues). "
        "L'optimiseur en tient compte : il stafe plus d'agents si la productivité est faible.",
    ),
    "Profil DOW": (
        "Profil hebdomadaire (mensuel → jour)",
        "Poids relatif par jour de semaine. "
        "**Contacts du jour = contacts_mensuels × poids_DOW / Σ(poids × nb_jours_du_mois)**. "
        "Permet de concentrer les contacts sur les jours ouvrés ou d'aplatir vers le week-end.",
    ),
    "Profil intraday": (
        "Profil intraday (jour → créneau 15 min)",
        "Répartition des contacts sur les **96 créneaux** de 15 min d'une journée. "
        "Dépend du DOW. Renseigné en heure **locale** (Paris), converti en UTC automatiquement. "
        "**Global dans la V2** : un seul profil pour tous les groupes et régions.",
    ),
    "max_agents": (
        "Plafond d'effectif",
        "Nombre maximum d'agents qu'une équipe peut aligner sur un créneau. "
        "Contrainte physique (taille du plateau téléphonique) ou contractuelle (clause BPO). "
        "`NULL` = pas de plafond (l'optimiseur peut prendre autant qu'il veut).",
    ),
    "Allocation": (
        "Répartition d'agents (résultat de l'optimiseur)",
        "Table `allocation` : agents par **(équipe, slot_utc, DOW)**. "
        "Générée par l'optimiseur, **éditable à la main** dans la page Couverture & coûts. "
        "Sert de base au calcul de couverture (ETP réel vs ETP requis).",
    ),
    "Supply": (
        "Mode de transport",
        "Type de service : AIR (aérien), RAIL, BUS, FERRY, etc. "
        "Croisé avec la région pour former une entité opérationnelle (ex. FR/AIR, ES/RAIL). "
        "Le choix du supply influe sur le groupe commercial cible via `group_map`.",
    ),
    "Région": (
        "Zone géographique opérationnelle",
        "Entité avec un **pays** et un **fuseau horaire** (ex. FR = France, Europe/Paris). "
        "La demande est initialement renseignée par région × supply × mois. "
        "Le fuseau sert à convertir les disponibilités des équipes en UTC.",
    ),
    "Shrinkage gross-up": (
        "Majoration ETP pour le shrinkage",
        "L'Erlang C calcule le nombre d'agents *présents et disponibles*. "
        "Comme les agents ne sont pas disponibles 100 % du temps (cf. Shrinkage), "
        "on majore : **ETP_planifié = ETP_Erlang / (1 − shrinkage)**. "
        "Ex. shrinkage = 0.30 → on planifie 43 % d'agents de plus qu'Erlang ne demande.",
    ),
}


def _glossary_entry(term: str, full_name: str, definition: str):
    with st.expander(f"**{term}** — {full_name}"):
        st.markdown(definition)


def render_glossary():
    st.header("📖 Glossaire & Documentation")
    st.markdown(
        "Toutes les définitions des termes, acronymes et paramètres du moteur de staffing. "
        "Cliquez sur un terme pour lire la définition complète."
    )

    search = st.text_input("🔍 Rechercher", placeholder="ex. Erlang, SLA, PAX, BPO…")
    filtered = {
        k: v for k, v in GLOSSARY.items()
        if not search
        or search.lower() in k.lower()
        or search.lower() in v[0].lower()
        or search.lower() in v[1].lower()
    }

    if not filtered:
        st.warning(f"Aucun terme ne correspond à « {search} ».")
    else:
        for term, (full_name, definition) in sorted(filtered.items()):
            _glossary_entry(term, full_name, definition)

    st.divider()
    st.subheader("Modèle de données — architecture des tables")
    st.graphviz_chart("""
digraph {
    rankdir=LR; bgcolor="transparent"; compound=true;
    node [shape=box, style="rounded,filled", fontname="sans-serif", fontsize=10];
    edge [fontsize=9, color="#666666"];

    subgraph cluster_ref {
        label="Référentiels"; style=dashed; color="#2C7FB8"; bgcolor="#EEF6FC";
        region  [label="region (A3)\\nrégion + fuseau",   fillcolor="#BDD7EE", color="#2C7FB8"];
        supply  [label="supply (A2)\\nmode de transport", fillcolor="#BDD7EE", color="#2C7FB8"];
        grp     [label="group (A7)\\ngroupe commercial",  fillcolor="#BDD7EE", color="#2C7FB8"];
        task    [label="task_type (A5)\\ntype de tâche",  fillcolor="#BDD7EE", color="#2C7FB8"];
    }

    subgraph cluster_demand {
        label="Demande"; style=dashed; color="#E67E22"; bgcolor="#FEF5EC";
        gmap  [label="group_map (A4)\\nrégion×supply→groupe", fillcolor="#FAD7A0", color="#E67E22"];
        pax   [label="pax_forecast\\nPAX mensuels",            fillcolor="#FAD7A0", color="#E67E22"];
        cr    [label="contact_rate\\nforecast",                fillcolor="#FAD7A0", color="#E67E22"];
        pdow  [label="profile_dow\\npoids hebdo",              fillcolor="#FAD7A0", color="#E67E22"];
        pint  [label="profile_intraday\\npoids 15 min",        fillcolor="#FAD7A0", color="#E67E22"];
    }

    subgraph cluster_teams {
        label="Équipes"; style=dashed; color="#27AE60"; bgcolor="#EAFAF1";
        team  [label="team (A6)\\ncoût · productivité", fillcolor="#A9DFBF", color="#27AE60"];
        avail [label="team_availability (D1)\\ndisponibilités",  fillcolor="#A9DFBF", color="#27AE60"];
        tg    [label="team_group (A8)\\naffectation groupes",    fillcolor="#A9DFBF", color="#27AE60"];
    }

    subgraph cluster_params {
        label="Paramètres"; style=dashed; color="#8E44AD"; bgcolor="#F5EEF8";
        aht [label="param_aht (B1)\\nAHT par tâche",     fillcolor="#D7BDE2", color="#8E44AD"];
        sla [label="service_params (B4)\\nSLA · shrinkage", fillcolor="#D7BDE2", color="#8E44AD"];
    }

    subgraph cluster_results {
        label="Résultats (calculés / éditables)"; style=dashed; color="#E74C3C"; bgcolor="#FDEDEC";
        alloc [label="allocation\\nagents par slot×DOW", fillcolor="#F1948A", color="#E74C3C"];
    }

    region -> gmap; supply -> gmap; grp -> gmap;
    gmap -> pax; gmap -> cr;
    task -> cr; task -> aht;
    grp -> tg; team -> tg; team -> avail;
    grp -> sla;
    pax  -> alloc [label="via Erlang C", style=dashed, ltail=cluster_demand];
    aht  -> alloc [style=dashed];
    sla  -> alloc [style=dashed];
    tg   -> alloc;
    avail -> alloc;
}
""")

    st.divider()
    st.subheader("Pipeline de calcul — étapes ① à ⑥")
    st.markdown("""
| Étape | Page | Calcul |
|-------|------|--------|
| ① | Données sources | Lecture des tables de référence |
| ② | Contacts mensuels | PAX × Contact Rate → contacts par groupe×mois |
| ③ | Profils de répartition | Étalage mensuel → jours (profil DOW) → créneaux (profil intraday) |
| ④ | Demande à 15 min | Contacts × AHT / 3 600 = workload en heures par bucket UTC |
| ⑤ | Erlang C | Workload → ETP requis (tenant compte SLA + shrinkage gross-up) |
| ⑥ | Couverture & coûts | Optimiseur IP → allocation d'agents → couverture vs requis |
""")


# =============================================================================
# RÉFÉRENTIELS
# =============================================================================

def render_ref_regions():
    st.header("🌍 Régions & Modes de transport")
    st.markdown(
        "Le **référentiel géographique** est la brique de base de la demande. "
        "Chaque combinaison **Région × Supply** (ex. *FR/AIR*, *ES/RAIL*) est une entité opérationnelle "
        "pour laquelle on saisit des PAX et des taux de contact."
    )

    tab_reg, tab_sup, tab_map = st.tabs(
        ["🗺️ Régions (A3)", "🚆 Modes de transport (A2)", "🔀 Mapping Groupe×Mois (A4)"]
    )

    with tab_reg:
        st.markdown(
            "**Région** = zone géographique avec un pays et un fuseau horaire. "
            "Le fuseau horaire convertit automatiquement les disponibilités des équipes en UTC."
        )
        st.warning(
            "⚠️ `region_id` est utilisé comme **clé** dans toutes les autres tables (PAX, contact rate, group_map…). "
            "Ne le modifiez pas après avoir saisi des données — cela romprait les liaisons."
        )
        df = C.table("region")
        edited = st.data_editor(
            df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_region",
            column_config={
                "region_id":    st.column_config.TextColumn(
                    "ID région (clé)", help="Code court unique, sans espace. Ex : FR, ES, IT, UK"),
                "region_label": st.column_config.TextColumn(
                    "Libellé affiché", help="Nom complet pour l'interface. Ex : France"),
                "country_code": st.column_config.TextColumn(
                    "Code pays ISO-2", help="2 lettres majuscules (norme ISO 3166-1). Ex : FR, ES, IT. "
                                            "Sert à récupérer les jours fériés."),
                "timezone":     st.column_config.TextColumn(
                    "Fuseau horaire (tz)", help="Nom IANA complet. Ex : Europe/Paris, Europe/Madrid. "
                                                "Liste complète : https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"),
            },
        )
        if st.button("💾 Enregistrer les Régions", type="primary", key="save_region"):
            _check_key_rename(df, edited, "region_id", "region")
            C.save_table("region", edited)
            st.success("Régions enregistrées.")
            _cascade_info([
                ("🗺️", "**group_map** — ajouter le mapping Région×Supply→Groupe pour les nouveaux combos (onglet Mapping ci-dessous)"),
                ("📊", "**pax_forecast** — saisir les PAX prévus pour chaque nouveau couple région×supply (Ingestion CSV ou Éditer les données)"),
                ("📞", "**contact_rate_forecast** — saisir les taux de contact pour chaque région×supply×tâche"),
            ])

    with tab_sup:
        st.markdown(
            "**Mode de transport** (supply) = type de service commercial. "
            "Croisé avec la région pour former une entité opérationnelle. "
            "Ex. *FR* (région) × *AIR* (supply) = contacts liés aux vols directs depuis la France."
        )
        df = C.table("supply")
        edited = st.data_editor(
            df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_supply",
            column_config={
                "supply_id":    st.column_config.TextColumn(
                    "ID supply (clé)", help="Ex : AIR, RAIL, BUS, FERRY"),
                "supply_label": st.column_config.TextColumn(
                    "Libellé affiché", help="Ex : Aérien, Ferroviaire"),
            },
        )
        if st.button("💾 Enregistrer les Modes de transport", type="primary", key="save_supply"):
            _check_key_rename(df, edited, "supply_id", "supply")
            C.save_table("supply", edited)
            st.success("Modes de transport enregistrés.")
            _cascade_info([
                ("🗺️", "**group_map** — router ce supply vers un groupe commercial (onglet Mapping ci-dessous)"),
                ("📊", "**pax_forecast** — saisir les PAX pour ce supply × régions concernées"),
                ("📞", "**contact_rate_forecast** — saisir les taux de contact pour ce supply"),
            ])

    with tab_map:
        st.markdown(
            "Le **mapping Groupe** détermine, **mois par mois**, quelles combinaisons Région × Supply "
            "sont actives et vers quel **groupe commercial** elles sont routées. "
            "C'est ici que vous décidez, par exemple, si *IT/AIR* va dans *DIRECT_AIR* ou *OTA*."
        )
        st.info(
            "💡 Une combinaison Région×Supply peut changer de groupe d'un mois à l'autre "
            "(reclassification commerciale, nouvelle campagne)."
        )
        # Import _group_map_editor logic inline to avoid circular imports
        _group_map_editor_regions()


def _group_map_editor_regions():
    """Pivot éditable du mapping group : combos (Region×Supply) × mois, actif/inactif."""
    gm = C.table("group_map")
    grp = C.table("group")
    all_groups = sorted(grp["group_id"].tolist()) if not grp.empty else sorted(gm["group_id"].unique()) if not gm.empty else []
    if gm.empty:
        st.info("Aucun mapping Groupe configuré. Lancez le script de seed ou importez un CSV group_map.")
        return
    months_list = sorted(gm["month"].unique())
    piv = (gm.pivot_table(index=["region_id", "supply_id"], columns="month",
                          values="active", fill_value=0)
           .reindex(columns=months_list, fill_value=0).reset_index())
    # Add group_id column (one per region×supply, from latest month)
    latest = gm.loc[gm.groupby(["region_id", "supply_id"])["month"].idxmax(), ["region_id", "supply_id", "group_id"]]
    piv = piv.merge(latest, on=["region_id", "supply_id"], how="left")
    # Reorder: put group_id right after supply_id
    front = ["region_id", "supply_id", "group_id"]
    piv = piv[front + [c for c in piv.columns if c not in front]]
    col_config = {
        "region_id": st.column_config.TextColumn("Région", disabled=True),
        "supply_id": st.column_config.TextColumn("Supply", disabled=True),
        "group_id":  st.column_config.SelectboxColumn("Groupe commercial", options=all_groups),
    }
    # CheckboxColumn requires bool dtype — cast float 0/1 from pivot
    for m in months_list:
        piv[m] = piv[m].fillna(0).astype(bool)
        col_config[m] = st.column_config.CheckboxColumn(m)
    edited = st.data_editor(piv, column_config=col_config, hide_index=True,
                            num_rows="fixed", width="stretch", key="ed_gmap_ref")
    if st.button("💾 Enregistrer le mapping Groupe", type="primary", key="save_gmap_ref"):
        rows = []
        # iterrows() instead of itertuples(): month names like "2026-01" are not
        # valid Python identifiers so itertuples() renames them to _N, breaking getattr.
        for _, r in edited.iterrows():
            gid = r["group_id"]
            for m in months_list:
                rows.append({"month": m, "region_id": r["region_id"],
                             "supply_id": r["supply_id"], "group_id": gid,
                             "active": int(bool(r[m]))})
        C.save_table("group_map", pd.DataFrame(rows))
        st.success("Mapping Groupe enregistré.")
        st.rerun()


def render_ref_groups():
    st.header("🏷️ Groupes commerciaux (A7)")
    st.markdown(
        "Un **groupe commercial** est un segment de clientèle / canal de vente. "
        "Il détermine le **routage** des contacts : chaque groupe a ses propres équipes dédiées, "
        "son propre SLA et des paramètres AHT optionnels. "
        "L'optimiseur dimensionne la capacité **indépendamment** par groupe (pas de mutualisation)."
    )

    col_def, col_flow = st.columns(2)
    with col_def:
        st.markdown("**Groupes actuellement en base :**")
        grp_live = C.table("group")
        st.dataframe(grp_live.rename(columns={"group_id": "Groupe", "group_label": "Libellé"}),
                     hide_index=True, use_container_width=True)
    with col_flow:
        st.markdown("**Comment le routage fonctionne :**")
        st.markdown("""
1. PAX × Contact Rate → demande par région×supply
2. `group_map` : région×supply → **groupe commercial**
3. `team_group` : groupe → **équipes autorisées**
4. L'optimiseur alloue la capacité séparément par groupe
        """)

    st.divider()
    st.subheader("Éditer les groupes")
    df = C.table("group")
    edited = st.data_editor(
        df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_group_page",
        column_config={
            "group_id":    st.column_config.TextColumn("ID groupe (clé)",
                                                       help="Code court sans espace. Ex : DIRECT_AIR"),
            "group_label": st.column_config.TextColumn("Libellé affiché",
                                                       help="Nom complet pour l'interface. Modifiable librement."),
        },
    )
    if st.button("💾 Enregistrer les Groupes commerciaux", type="primary", key="save_group_page"):
        _check_key_rename(df, edited, "group_id", "group")
        C.save_table("group", edited)
        st.success("Groupes commerciaux enregistrés.")
        _cascade_info([
            ("🗺️", "**group_map** — router au moins une combinaison Région×Supply vers ce groupe (Régions → Mapping Groupe×Mois)"),
            ("👥", "**team_group** — affecter au moins une équipe à ce groupe (Équipes → Affectation groupes)"),
            ("📐", "**service_params** — optionnel : créer une ligne SLA spécifique à ce groupe si besoin (sinon la ligne générique s'applique)"),
        ])

    st.divider()
    st.subheader("Équipes actuellement affectées par groupe")
    tg = C.table("team_group")
    teams = C.table("team")[["team_id", "team_label", "level", "sourcing"]]
    if not tg.empty and not teams.empty:
        tg_merged = tg.merge(teams, on="team_id", how="left")
        st.dataframe(
            tg_merged.sort_values(["group_id", "level"]),
            hide_index=True, use_container_width=True,
            column_config={
                "group_id":   st.column_config.TextColumn("Groupe"),
                "team_id":    st.column_config.TextColumn("ID équipe"),
                "team_label": st.column_config.TextColumn("Équipe"),
                "level":      st.column_config.NumberColumn("Level"),
                "sourcing":   st.column_config.TextColumn("Sourcing"),
            },
        )
        st.caption("Pour modifier l'affectation, allez dans **Équipes → Affectation groupes**.")

        # Chart: nombre d'équipes par groupe et level
        count = tg_merged.groupby(["group_id", "level"]).size().reset_index(name="nb_équipes")
        count["Level"] = "L" + count["level"].astype(int).astype(str)
        lvl_cmap = _level_color_map(count["level"].unique())
        fig = px.bar(count, x="group_id", y="nb_équipes", color="Level",
                     barmode="stack",
                     labels={"group_id": "Groupe commercial", "nb_équipes": "Nb équipes"},
                     title="Équipes affectées par groupe commercial",
                     color_discrete_map=lvl_cmap,
                     category_orders={"Level": sorted(lvl_cmap.keys())})
        fig.update_layout(height=300, legend_title_text="Level")
        st.plotly_chart(fig, use_container_width=True)


def render_ref_tasks():
    st.header("📋 Types de tâche (A5)")
    st.markdown(
        "Un **type de tâche** est une catégorie de contact client "
        "(appel entrant, email, réclamation, remboursement…). "
        "Chaque type porte un **level** (L1 ou L2) et un **AHT** défini dans Paramètres → AHT."
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("""
**Level :**
- `1` → tâche L1, traitée par les **BPO externes**
- `2` → tâche L2, traitée en **interne** (escalades)
        """)
    with col2:
        st.markdown("""
**Impact sur le dimensionnement :**
- L'AHT (Paramètres → AHT) est lié à `task_type_id`
- Le contact rate (PAX → taux de contact) est renseigné par région×supply×task_type
        """)

    df = C.table("task_type")
    edited = st.data_editor(
        df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_task_type",
        column_config={
            "task_type_id":    st.column_config.TextColumn(
                "ID tâche (clé)", help="Ex : CALL_IN, EMAIL, REFUND_L1. Sans espace."),
            "task_type_label": st.column_config.TextColumn(
                "Libellé affiché", help="Ex : Appel entrant, Email, Remboursement"),
            "level":           st.column_config.NumberColumn(
                "Level (1 ou 2)", min_value=1, max_value=2, step=1,
                help="1 = L1 BPO externe  ·  2 = L2 interne (escalades)"),
        },
    )
    if st.button("💾 Enregistrer les Types de tâche", type="primary", key="save_task"):
        _check_key_rename(df, edited, "task_type_id", "task_type")
        C.save_table("task_type", edited)
        st.success("Types de tâche enregistrés.")
        _cascade_info([
            ("⏱️", "**param_aht** — définir l'AHT en secondes pour ce type de tâche (Paramètres → AHT par type de tâche)"),
            ("📞", "**contact_rate_forecast** — saisir un taux de contact pour chaque mois × région × supply × tâche (Ingestion CSV ou Éditer les données)"),
            ("🔢", "Level 1 → équipes L1 (BPO externe) doivent être affectées aux groupes concernés"),
            ("🔢", "Level 2 → équipes L2 internes mutualisées — vérifier l'affectation dans Équipes → Affectation groupes"),
        ])

    st.divider()
    st.subheader("AHT actuels par type de tâche")
    aht = C.table("param_aht")
    if not aht.empty:
        aht_disp = aht.copy()
        aht_disp["aht_min"] = (aht_disp["aht_seconds"] / 60).round(1)
        cols_show = ["task_type_id"]
        if "group_id" in aht_disp.columns:
            cols_show.append("group_id")
        cols_show += ["aht_seconds", "aht_min"]
        col_cfg = {
            "task_type_id": st.column_config.TextColumn("Type de tâche"),
            "aht_seconds":  st.column_config.NumberColumn("AHT (s)"),
            "aht_min":      st.column_config.NumberColumn("AHT (min)", format="%.1f"),
        }
        if "group_id" in aht_disp.columns:
            col_cfg["group_id"] = st.column_config.TextColumn("Groupe (vide = tous)")
        st.dataframe(aht_disp[cols_show], hide_index=True, use_container_width=True, column_config=col_cfg)

        # Chart: AHT par type de tâche (moyenne si plusieurs groupes)
        aht_chart = aht_disp.groupby("task_type_id")["aht_seconds"].mean().reset_index()
        aht_chart["aht_min"] = (aht_chart["aht_seconds"] / 60).round(1)
        fig = px.bar(aht_chart.sort_values("aht_seconds", ascending=True),
                     x="aht_seconds", y="task_type_id", orientation="h",
                     text="aht_min",
                     labels={"aht_seconds": "AHT (secondes)", "task_type_id": ""},
                     title="Temps de traitement moyen par type de tâche",
                     color="aht_seconds", color_continuous_scale="Blues")
        fig.update_traces(texttemplate="%{text} min", textposition="outside")
        fig.update_layout(coloraxis_showscale=False, height=max(250, 50 * len(aht_chart)))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Pour modifier les AHT → **Paramètres → AHT & Objectifs SLA**")
    else:
        st.info("Aucun AHT configuré. Allez dans **Paramètres → AHT & Objectifs SLA**.")


# =============================================================================
# ÉQUIPES
# =============================================================================

def render_teams_page():
    st.header("👥 Équipes, disponibilités & affectation groupes")
    st.markdown(
        "Tout ce qui concerne les **équipes** : leur fiche (coût, productivité, plafond), "
        "leurs **fenêtres de disponibilité** (quand elles peuvent travailler) "
        "et leur **affectation aux groupes commerciaux** (quels contacts elles traitent)."
    )

    tab_teams, tab_avail, tab_tg = st.tabs(
        ["🧑‍💼 Équipes (A6)", "🕐 Disponibilités (D1)", "🔗 Affectation groupes (A8)"]
    )

    # ---- Tab Équipes ----
    with tab_teams:
        st.markdown("""
| Colonne | Signification |
|---------|---------------|
| `level` | 1 = L1 BPO externe · 2 = L2 interne |
| `sourcing` | `external` (facturation horaire BPO) · `internal` (masse salariale) |
| `productivity` | Facteur 0–1 : capacité_effective = agents × productivité |
| `hourly_cost` | Coût par heure·agent en € (charge totale, pas le salaire brut) |
| `max_agents` | Plafond contractuel ou physique. Vide = illimité |
| `timezone` | Fuseau local, pour convertir les fenêtres de dispo en UTC |
        """)
        st.info(
            "💡 L'effectif n'est **pas fixé ici** — c'est l'optimiseur qui décide combien d'agents allouer. "
            "`max_agents` est seulement un plafond optionnel."
        )
        df = C.table("team")
        edited = st.data_editor(
            df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_team",
            column_config={
                "team_id":      st.column_config.TextColumn("ID équipe (clé)",
                                                            help="Ex : EXT_MADRID, INT_PARIS"),
                "team_label":   st.column_config.TextColumn("Libellé affiché"),
                "level":        st.column_config.NumberColumn("Level", min_value=1, max_value=2, step=1),
                "sourcing":     st.column_config.SelectboxColumn("Sourcing",
                                                                  options=["external", "internal"]),
                "country_code": st.column_config.TextColumn("Pays (ISO-2)"),
                "timezone":     st.column_config.TextColumn("Fuseau (tz)",
                                                            help="Ex : Europe/Madrid, Africa/Dakar"),
                "productivity": st.column_config.NumberColumn("Productivité",
                                                               min_value=0.0, max_value=1.0, step=0.05, format="%.2f"),
                "hourly_cost":  st.column_config.NumberColumn("Coût horaire (€/h)",
                                                               min_value=0.0, format="%.2f"),
                "max_agents":   st.column_config.NumberColumn("Plafond agents",
                                                               min_value=0,
                                                               help="Laisser vide = pas de plafond"),
            },
        )
        if st.button("💾 Enregistrer les Équipes", type="primary", key="save_team"):
            C.save_table("team", edited)
            st.success("Équipes enregistrées.")
            st.rerun()

        # Charts: coût horaire et productivité par équipe
        df_chart = C.table("team")
        if not df_chart.empty:
            st.divider()
            ch1, ch2 = st.columns(2)
            with ch1:
                fig = px.bar(
                    df_chart.sort_values("hourly_cost"),
                    x="hourly_cost", y="team_id", orientation="h",
                    color="sourcing",
                    color_discrete_map={"external": "#3498DB", "internal": "#E67E22"},
                    labels={"hourly_cost": "Coût horaire (€/h)", "team_id": ""},
                    title="Coût horaire par équipe",
                )
                fig.update_layout(height=max(250, 45 * len(df_chart)), legend_title_text="Sourcing")
                st.plotly_chart(fig, use_container_width=True)
            with ch2:
                df_chart2 = df_chart.copy()
                df_chart2["Level"] = "L" + df_chart2["level"].astype(int).astype(str)
                lvl_cmap2 = _level_color_map(df_chart2["level"].unique())
                fig2 = px.bar(
                    df_chart2.sort_values("productivity"),
                    x="productivity", y="team_id", orientation="h",
                    color="Level", color_discrete_map=lvl_cmap2,
                    labels={"productivity": "Productivité (0–1)", "team_id": ""},
                    title="Productivité par équipe",
                )
                fig2.update_layout(height=max(250, 45 * len(df_chart2)), legend_title_text="Level")
                fig2.update_xaxes(range=[0, 1.05])
                st.plotly_chart(fig2, use_container_width=True)

    # ---- Tab Disponibilités ----
    with tab_avail:
        st.markdown(
            "Définissez les **fenêtres horaires** où chaque équipe peut travailler, par jour de semaine. "
            "Les heures sont en **heure locale** de l'équipe (timezone définie dans l'onglet Équipes). "
            "L'optimiseur place les agents **uniquement** dans ces fenêtres."
        )
        st.info(
            "💡 **DOW** = Day Of Week : 0 = Lundi, 1 = Mardi, …, 6 = Dimanche. "
            "Pour une équipe 24h/24, renseignez start=`00:00` et end=`00:00` "
            "(minuit à minuit = 24h cycliques)."
        )
        team_ids = C.table("team")["team_id"].tolist() if not C.table("team").empty else []
        dow_map = {0: "Lundi", 1: "Mardi", 2: "Mercredi", 3: "Jeudi",
                   4: "Vendredi", 5: "Samedi", 6: "Dimanche"}
        df = C.table("team_availability")
        if not df.empty and "dow" in df.columns:
            df_disp = df.copy()
            df_disp.insert(2, "jour", df_disp["dow"].map(dow_map))
        else:
            df_disp = df
        edited = st.data_editor(
            df_disp, width="stretch", hide_index=True, num_rows="dynamic", key="ed_avail",
            column_config={
                "team_id":     st.column_config.SelectboxColumn("Équipe", options=team_ids),
                "dow":         st.column_config.NumberColumn("DOW (0=Lun…6=Dim)",
                                                              min_value=0, max_value=6, step=1),
                "jour":        st.column_config.TextColumn("Jour (info)", disabled=True),
                "start_local": st.column_config.TextColumn("Début (HH:MM local)",
                                                            help="Ex : 08:00"),
                "end_local":   st.column_config.TextColumn("Fin (HH:MM local)",
                                                            help="Ex : 20:00. 00:00 = minuit (fin de journée cyclique)"),
            },
        )
        to_save_avail = edited.drop(columns=["jour"], errors="ignore")
        if st.button("💾 Enregistrer les Disponibilités", type="primary", key="save_avail"):
            C.save_table("team_availability", to_save_avail)
            st.success("Disponibilités enregistrées.")
            st.rerun()

        # Heatmap: heures de couverture par équipe × jour de semaine
        av = C.table("team_availability")
        if not av.empty and "start_local" in av.columns and "end_local" in av.columns:
            st.divider()
            st.markdown("**Couverture horaire par équipe et jour de semaine**")

            def _window_hours(row):
                try:
                    sh, sm = map(int, str(row["start_local"]).split(":"))
                    eh, em = map(int, str(row["end_local"]).split(":"))
                    start_min = sh * 60 + sm
                    end_min = eh * 60 + em
                    if end_min <= start_min:
                        end_min += 24 * 60
                    return (end_min - start_min) / 60
                except Exception:
                    return 0

            av = av.copy()
            av["heures"] = av.apply(_window_hours, axis=1)
            pivot_av = av.pivot_table(index="team_id", columns="dow",
                                      values="heures", aggfunc="sum", fill_value=0)
            dow_labels_map = {0: "Lun", 1: "Mar", 2: "Mer", 3: "Jeu",
                              4: "Ven", 5: "Sam", 6: "Dim"}
            pivot_av.columns = [dow_labels_map.get(c, str(c)) for c in pivot_av.columns]
            fig_av = px.imshow(
                pivot_av, text_auto=".0f",
                color_continuous_scale="Blues",
                labels={"color": "Heures/jour"},
                title="Fenêtres de disponibilité (heures locales/jour par équipe)",
            )
            fig_av.update_layout(height=max(250, 50 * len(pivot_av)))
            st.plotly_chart(fig_av, use_container_width=True)
            st.caption("Valeurs = nb d'heures de disponibilité sur ce jour de semaine (heure locale).")

    # ---- Tab Affectation groupes ----
    with tab_tg:
        st.markdown(
            "L'**affectation équipes → groupes** détermine quels contacts une équipe peut traiter. "
            "**Une ligne = une équipe peut prendre en charge les contacts de ce groupe commercial.** "
            "Une équipe peut couvrir plusieurs groupes (typiquement INT_PARIS couvre tous les groupes en L2)."
        )
        team_ids = C.table("team")[["team_id", "team_label"]].copy() if not C.table("team").empty \
            else pd.DataFrame(columns=["team_id", "team_label"])
        group_ids = C.table("group")["group_id"].tolist() if not C.table("group").empty else []
        df = C.table("team_group")

        # Visual matrix (read-only)
        if not df.empty and not team_ids.empty and group_ids:
            st.markdown("**Vue matricielle :**")
            matrix = df.assign(v=1).pivot_table(
                index="team_id", columns="group_id", values="v", fill_value=0,
                aggfunc="sum",
            ).reset_index()
            matrix = matrix.merge(C.table("team")[["team_id", "level"]], on="team_id", how="left")
            cols_ord = ["team_id", "level"] + [c for c in matrix.columns if c not in ("team_id", "level")]
            st.dataframe(matrix[cols_ord], hide_index=True, use_container_width=True)
            st.caption("1 = équipe affectée à ce groupe  ·  0 = non affectée")
            st.divider()

        st.markdown("**Éditer (ajouter / supprimer des affectations) :**")
        edited = st.data_editor(
            df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_tg",
            column_config={
                "team_id":  st.column_config.SelectboxColumn("Équipe",
                                                              options=team_ids["team_id"].tolist()),
                "group_id": st.column_config.SelectboxColumn("Groupe commercial",
                                                              options=group_ids),
            },
        )
        if st.button("💾 Enregistrer l'affectation équipes→groupes", type="primary", key="save_tg"):
            C.save_table("team_group", edited)
            st.success("Affectation enregistrée.")
            st.rerun()


# =============================================================================
# PARAMÈTRES
# =============================================================================

def render_params_page():
    st.header("⚙️ Paramètres de dimensionnement")
    st.markdown(
        "Ces deux tables **pilotent le calcul Erlang C** : elles définissent combien de charge "
        "génère chaque contact (AHT) et quel niveau de service il faut atteindre (SLA). "
        "Tout changement ici impacte directement l'**ETP requis** et l'**allocation optimisée**."
    )

    tab_aht, tab_sla = st.tabs(
        ["⏱️ AHT — Temps de traitement (B1)", "🎯 Objectifs SLA & Erlang C (B4)"]
    )

    # ---- Tab AHT ----
    with tab_aht:
        st.markdown("""
**AHT** = Average Handling Time = durée moyenne de traitement d'un contact, **en secondes**.

Formule : **Workload (h) = contacts × AHT (s) / 3 600**

- Renseignez un AHT **par type de tâche** (obligatoire)
- Le champ `group_id` est **optionnel** :
  - Laissez **vide** (`NULL`) → AHT s'applique à **tous** les groupes pour ce type de tâche
  - Renseignez un groupe → **surcharge** l'AHT uniquement pour ce groupe
  - Ex. : CALL_IN = 180 s par défaut, mais 240 s spécifiquement pour FERRY
        """)
        st.info("💡 AHT +10 % → charge +10 % → ~10 % d'agents supplémentaires toutes choses égales par ailleurs.")

        task_ids = C.table("task_type")["task_type_id"].tolist() if not C.table("task_type").empty else []
        group_ids = C.table("group")["group_id"].tolist() if not C.table("group").empty else []

        df = C.table("param_aht")
        df_disp = df.copy()
        if not df_disp.empty and "aht_seconds" in df_disp.columns:
            df_disp["aht_min"] = (df_disp["aht_seconds"] / 60).round(1)

        edited = st.data_editor(
            df_disp, width="stretch", hide_index=True, num_rows="dynamic", key="ed_aht",
            column_config={
                "task_type_id": st.column_config.SelectboxColumn("Type de tâche", options=task_ids),
                "group_id":     st.column_config.SelectboxColumn("Groupe (vide = tous)",
                                                                   options=[None] + group_ids),
                "aht_seconds":  st.column_config.NumberColumn("AHT (secondes)",
                                                               min_value=0, format="%d",
                                                               help="Durée en secondes. Ex : 180 = 3 min"),
                "aht_min":      st.column_config.NumberColumn("AHT (minutes)",
                                                               disabled=True, format="%.1f",
                                                               help="Colonne calculée — non enregistrée"),
            },
        )
        to_save_aht = edited.drop(columns=["aht_min"], errors="ignore")
        if st.button("💾 Enregistrer les AHT", type="primary", key="save_aht"):
            C.save_table("param_aht", to_save_aht)
            st.success("AHT enregistrés.")
            st.rerun()

        # Chart AHT
        if not df.empty and "aht_seconds" in df.columns:
            df_chart = df.copy()
            df_chart["aht_min"] = (df_chart["aht_seconds"] / 60).round(1)
            df_chart["groupe"] = df_chart["group_id"].fillna("(tous groupes)") if "group_id" in df_chart.columns else "(tous groupes)"
            fig = px.bar(df_chart.sort_values("aht_seconds"),
                         x="aht_seconds", y="task_type_id", orientation="h",
                         color="groupe", text="aht_min",
                         labels={"aht_seconds": "AHT (s)", "task_type_id": "", "groupe": "Groupe"},
                         title="AHT par type de tâche (secondes)")
            fig.update_traces(texttemplate="%{text} min", textposition="outside")
            fig.update_layout(height=max(250, 55 * len(df_chart)), legend_title_text="Groupe")
            st.plotly_chart(fig, use_container_width=True)

    # ---- Tab SLA ----
    with tab_sla:
        st.markdown("""
**Objectifs de service** utilisés dans la formule **Erlang C** pour calculer l'ETP requis.

| Paramètre | Description | Exemple |
|-----------|-------------|---------|
| `level` | 1 (L1 BPO) ou 2 (L2 interne) | `1` |
| `group_id` | Groupe commercial, ou **vide** = fallback global pour ce level | `FERRY` ou vide |
| `sl_target` | Fraction de contacts à traiter dans le délai | `0.95` = 95 % |
| `sl_seconds` | Délai cible en secondes | `120` = 2 minutes |
| `shrinkage` | Part du temps non disponible (pauses, formation…) | `0.30` = 30 % |
| `max_occupancy` | Taux d'occupation maximal toléré | `0.85` = 85 % |

**Priorité** : (level, group_id spécifique) ▶ (level, vide = global)
— si aucun paramètre spécifique pour un groupe, les paramètres globaux du level s'appliquent.
        """)
        st.warning(
            "⚠️ Un `sl_target` plus élevé (ex. 99 % vs 90 %) exige beaucoup plus d'agents : "
            "la courbe Erlang C est très non-linéaire à la saturation."
        )

        group_ids = C.table("group")["group_id"].tolist() if not C.table("group").empty else []
        df = C.table("service_params")
        edited = st.data_editor(
            df, width="stretch", hide_index=True, num_rows="dynamic", key="ed_sla",
            column_config={
                "level":         st.column_config.NumberColumn("Level (1 ou 2)",
                                                                min_value=1, max_value=2, step=1),
                "group_id":      st.column_config.SelectboxColumn("Groupe (vide = global)",
                                                                   options=[None] + group_ids),
                "sl_target":     st.column_config.NumberColumn("SLA cible (0–1)",
                                                                min_value=0.0, max_value=1.0,
                                                                step=0.01, format="%.2f",
                                                                help="Ex : 0.95 = 95 % des contacts"),
                "sl_seconds":    st.column_config.NumberColumn("Délai cible (s)",
                                                                min_value=0, step=1,
                                                                help="Ex : 120 = répondre en moins de 2 min"),
                "shrinkage":     st.column_config.NumberColumn("Shrinkage (0–1)",
                                                                min_value=0.0, max_value=1.0,
                                                                step=0.01, format="%.2f",
                                                                help="0.30 = 30 % du temps non disponible aux contacts"),
                "max_occupancy": st.column_config.NumberColumn("Occupation max (0–1)",
                                                                min_value=0.0, max_value=1.0,
                                                                step=0.01, format="%.2f",
                                                                help="0.85 = taux d'occupation max toléré"),
            },
        )
        if st.button("💾 Enregistrer les Objectifs SLA", type="primary", key="save_sla"):
            C.save_table("service_params", edited)
            st.success("Objectifs SLA enregistrés.")
            st.rerun()

        # Charts SLA
        sla_data = C.table("service_params")
        if not sla_data.empty:
            sla_data = sla_data.copy()
            sla_data["Level"] = "L" + sla_data["level"].astype(int).astype(str)
            lvl_cmap_sla = _level_color_map(sla_data["level"].unique())
            if "group_id" in sla_data.columns:
                sla_data["label"] = sla_data["Level"] + " — " + sla_data["group_id"].fillna("global")
            else:
                sla_data["label"] = sla_data["Level"]
            ch1, ch2 = st.columns(2)
            with ch1:
                fig_sl = px.bar(sla_data, x="label", y="sl_target",
                                color="Level", text="sl_target",
                                labels={"label": "", "sl_target": "SLA cible"},
                                title="SLA cible par configuration",
                                color_discrete_map=lvl_cmap_sla)
                fig_sl.update_traces(texttemplate="%{text:.0%}")
                fig_sl.update_yaxes(range=[0, 1.05], tickformat=".0%")
                fig_sl.update_layout(height=300, showlegend=False)
                st.plotly_chart(fig_sl, use_container_width=True)
            with ch2:
                fig_sh = px.bar(sla_data, x="label", y="shrinkage",
                                color="Level", text="shrinkage",
                                labels={"label": "", "shrinkage": "Shrinkage"},
                                title="Shrinkage par configuration",
                                color_discrete_map=lvl_cmap_sla)
                fig_sh.update_traces(texttemplate="%{text:.0%}")
                fig_sh.update_yaxes(range=[0, 0.6], tickformat=".0%")
                fig_sh.update_layout(height=300, showlegend=False)
                st.plotly_chart(fig_sh, use_container_width=True)
