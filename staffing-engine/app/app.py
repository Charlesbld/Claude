#!/usr/bin/env python3
"""Moteur de staffing — application Streamlit (V2, multipage).

    cd staffing-engine
    streamlit run app/app.py

L'appli amorce la base SQLite si besoin, puis propose :
  • Accueil (explication du fonctionnement)
  • Demande (étapes ① à ④)
  • Dimensionnement (Erlang C ⑤ + Couverture ⑥)
  • Analyse (Réel vs Forecast)
  • Outils (Explorateur + Éditeur)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # rend 'common'/'pages_app' importables

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Staffing — capacity planning 15 min", layout="wide", page_icon="🗓️")

import common as C  # noqa: E402  (configure aussi le sys.path racine)
import pages_app as P  # noqa: E402

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

    st.info("👉 Naviguez par étape (① à ⑥ dans le menu), lancez l'optimiseur dans **⑥ Couverture & coûts**, "
            "puis explorez. Toutes les tables sont visibles dans **Explorateur** et modifiables dans **Éditer**.")
    with st.expander("Hypothèses de modélisation"):
        st.markdown(
            "- Tout est stocké en **UTC** ; l'affichage est en heure de Paris.\n"
            "- **Routage par groupe** : chaque groupe commercial (DIRECT_AIR, RAIL, OTA…) n'est couvert "
            "que par les équipes affectées à ce groupe (table `team_group`).\n"
            "- Le **shrinkage** est porté côté demande (gross-up de l'ETP requis), la **productivité** côté offre.\n"
            "- L'optimiseur résout **un jour de semaine type** réutilisé sur le mois.")


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
        st.Page(P.render_forecast, title="Réel vs Forecast", icon="🔍"),
    ],
    "Outils": [
        st.Page(P.render_explorer, title="Explorateur",        icon="🔎"),
        st.Page(P.render_editor,   title="Éditer les données", icon="✏️"),
    ],
}
st.navigation(pages).run()
