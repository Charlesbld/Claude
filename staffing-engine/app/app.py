#!/usr/bin/env python3
"""Moteur de staffing — application Streamlit (V2, multipage).

    cd staffing-engine
    streamlit run app/app.py

Navigation :
  • Accueil
  • Demande         (étapes ① à ④)
  • Dimensionnement (Erlang C ⑤ + Couverture ⑥)
  • Analyse         (Réel vs Forecast · Rapport mensuel)
  • Référentiels    (Régions · Groupes · Types de tâche)
  • Équipes         (Fiche · Disponibilités · Affectation)
  • Paramètres      (AHT · Objectifs SLA)
  • Outils          (Explorateur · Glossaire)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # rend 'common'/'pages_app'/'pages_edit' importables

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Staffing — capacity planning 15 min", layout="wide", page_icon="🗓️")

import common as C        # noqa: E402
import pages_app as P     # noqa: E402
import pages_edit as E    # noqa: E402

C.ensure_db()


def render_home():
    st.title("🗓️ Moteur de staffing — capacity planning au pas de 15 min")
    st.markdown(
        "Banc d'essai (avant réplication dans Pigment) pour dimensionner un service client "
        "**au quart d'heure**, en **UTC**, avec routage de la capacité par **groupe commercial** "
        "(Level 1 = prestataires externes, Level 2 = escalades internes).")

    st.subheader("Comment ça marche")
    st.graphviz_chart("""
    digraph { rankdir=LR; bgcolor="transparent";
      node [shape=box, style="rounded,filled", fillcolor="#EAF2F8", color="#2C7FB8", fontname="sans-serif", fontsize=11];
      PAX[label="PAX forecast\\n(région×supply×mois)"]; CR[label="Contact rate\\n(éditable)"];
      GRP[label="Groupes\\ncommerciaux"];
      DEM[label="Demande\\n(bucket 15 min, UTC)"]; ERL[label="Erlang C\\n→ ETP requis/groupe"];
      TEAM[label="Équipes + groupes\\n+ disponibilités"]; OPT[label="Optimiseur IP\\n(coût minimal, par groupe)"];
      ALLOC[label="Répartition d'agents\\n(éditable)"];
      COV[label="Couverture · coûts · trous", fillcolor="#FDEBD0", color="#E67E22"];
      PAX->DEM; CR->DEM; GRP->DEM; DEM->ERL; ERL->COV; TEAM->OPT; ERL->OPT; OPT->ALLOC; ALLOC->COV;
    }""")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            "**Demande (forecast)** — `PAX forecast × contact rate` → contacts mensuels, étalés sur les jours "
            "(profil hebdo) puis sur les créneaux (profil intraday, heure locale → UTC).\n\n"
            "**Réel vs Forecast** — on compare aux PAX/tâches réels et on **décompose l'écart** entre "
            "effet *PAX* et effet *contact rate*, qu'on ajuste en direct.")
    with c2:
        st.markdown(
            "**Dimensionnement (Erlang C)** — pour chaque bucket **par groupe** : nb d'agents pour tenir le SLA "
            "(ex. 95 % en ≤ 120 s), puis effectif à planifier = agents / (1 − shrinkage).\n\n"
            "**Offre & optimiseur** — un programme linéaire choisit la **répartition d'agents la moins chère** "
            "couvrant le requis (par groupe × jour de semaine). Résultat **éditable**.")

    st.info(
        "👉 Naviguez par étape (① à ⑥ dans **Demande** et **Dimensionnement**), "
        "lancez l'optimiseur dans **⑥ Couverture & coûts**, "
        "puis éditez les données dans **Référentiels**, **Équipes** et **Paramètres**. "
        "Tous les termes sont définis dans **Outils → Glossaire**."
    )

    with st.expander("Hypothèses de modélisation"):
        st.markdown(
            "- Tout est stocké en **UTC** ; l'affichage est en heure de Paris.\n"
            "- **Routage par groupe** : chaque groupe commercial (DIRECT_AIR, RAIL, OTA…) n'est couvert "
            "que par les équipes affectées à ce groupe (table `team_group`).\n"
            "- Le **shrinkage** est porté côté demande (gross-up de l'ETP requis), la **productivité** côté offre.\n"
            "- L'optimiseur résout **un jour de semaine type** réutilisé sur le mois.\n"
            "- Le **profil intraday** est global (un seul profil pour tous les groupes et régions dans la V2).")

    with st.expander("Guide de navigation"):
        st.markdown("""
| Section | Contenu | Fréquence d'utilisation |
|---------|---------|------------------------|
| **Demande** ①–④ | Vérifier les données sources, visualiser les contacts par bucket | Mensuel (après import CSV) |
| **Dimensionnement** ⑤–⑥ | Lancer l'optimiseur, analyser la couverture et les coûts | Mensuel |
| **Analyse** | Comparer réel vs forecast, générer le rapport Excel | Mensuel |
| **Référentiels** | Modifier régions, transports, groupes, types de tâche | Trimestriel / exceptionnel |
| **Équipes** | Gérer équipes, disponibilités, affectation groupes | Mensuel si changement RH |
| **Paramètres** | Ajuster AHT, SLA, shrinkage | Trimestriel |
| **Outils** | Explorateur (pivot libre) + Glossaire | Ad hoc |
        """)


pages = {
    "": [st.Page(render_home, title="Accueil", icon="🏠", default=True)],
    "Demande": [
        st.Page(P.render_sources,         title="① Données sources",        icon="📋"),
        st.Page(P.render_demand_monthly,  title="② Contacts mensuels",      icon="✖️"),
        st.Page(P.render_demand_profiles, title="③ Profils de répartition", icon="📅"),
        st.Page(P.render_demand_buckets,  title="④ Demande à 15 min",       icon="⏱️"),
    ],
    "Dimensionnement": [
        st.Page(P.render_erlang,   title="⑤ Erlang C",           icon="📐"),
        st.Page(P.render_coverage, title="⑥ Couverture & coûts", icon="🗓️"),
    ],
    "Analyse": [
        st.Page(P.render_forecast,       title="Réel vs Forecast",  icon="🔍"),
        st.Page(P.render_monthly_report, title="Rapport mensuel",   icon="📊"),
    ],
    "Référentiels": [
        st.Page(E.render_ref_regions, title="Régions & Transports",  icon="🌍"),
        st.Page(E.render_ref_groups,  title="Groupes commerciaux",   icon="🏷️"),
        st.Page(E.render_ref_tasks,   title="Types de tâche",        icon="📋"),
    ],
    "Équipes": [
        st.Page(E.render_teams_page, title="Équipes, dispo & groupes", icon="👥"),
    ],
    "Paramètres": [
        st.Page(E.render_params_page, title="AHT & Objectifs SLA", icon="⚙️"),
    ],
    "Outils": [
        st.Page(P.render_explorer, title="Explorateur", icon="🔎"),
        st.Page(E.render_glossary, title="Glossaire",   icon="📖"),
    ],
}
st.navigation(pages).run()
