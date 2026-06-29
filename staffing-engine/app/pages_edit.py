"""Pages d'édition structurées et glossaire (Référentiels · Équipes · Paramètres · Documentation).

Chaque page couvre un domaine métier précis avec :
- Contexte explicatif (pourquoi cette table existe)
- Aide colonne par colonne
- data_editor configuré (dropdowns, format, limites)
- Bouton Enregistrer
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import common as C  # noqa: E402
from staffing import db  # noqa: E402


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
            C.save_table("region", edited)
            st.success("Régions enregistrées.")
            st.rerun()

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
            C.save_table("supply", edited)
            st.success("Modes de transport enregistrés.")
            st.rerun()

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
        _group_map_editor_inline()


def _group_map_editor_inline():
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
        st.markdown("**Groupes par défaut :**")
        st.dataframe(
            pd.DataFrame([
                {"Groupe": "DIRECT_AIR", "Description": "Billets aériens vendus en direct"},
                {"Groupe": "OTA",        "Description": "Agences de voyage en ligne"},
                {"Groupe": "RAIL",       "Description": "Ferroviaire"},
                {"Groupe": "BUS",        "Description": "Autocar"},
                {"Groupe": "FERRY",      "Description": "Maritime"},
            ]),
            hide_index=True, use_container_width=True,
        )
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
        C.save_table("group", edited)
        st.success("Groupes commerciaux enregistrés.")
        st.rerun()

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
        fig = px.bar(count, x="group_id", y="nb_équipes", color="Level",
                     barmode="stack",
                     labels={"group_id": "Groupe commercial", "nb_équipes": "Nb équipes"},
                     title="Équipes affectées par groupe commercial",
                     color_discrete_map={"L1": "#3498DB", "L2": "#E67E22"},
                     category_orders={"Level": ["L1", "L2"]})
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
        C.save_table("task_type", edited)
        st.success("Types de tâche enregistrés.")
        st.rerun()

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
                fig2 = px.bar(
                    df_chart2.sort_values("productivity"),
                    x="productivity", y="team_id", orientation="h",
                    color="Level", color_discrete_map={"L1": "#27AE60", "L2": "#8E44AD"},
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
                                color_discrete_map={"L1": "#3498DB", "L2": "#E67E22"})
                fig_sl.update_traces(texttemplate="%{text:.0%}")
                fig_sl.update_yaxes(range=[0, 1.05], tickformat=".0%")
                fig_sl.update_layout(height=300, showlegend=False)
                st.plotly_chart(fig_sl, use_container_width=True)
            with ch2:
                fig_sh = px.bar(sla_data, x="label", y="shrinkage",
                                color="Level", text="shrinkage",
                                labels={"label": "", "shrinkage": "Shrinkage"},
                                title="Shrinkage par configuration",
                                color_discrete_map={"L1": "#3498DB", "L2": "#E67E22"})
                fig_sh.update_traces(texttemplate="%{text:.0%}")
                fig_sh.update_yaxes(range=[0, 0.6], tickformat=".0%")
                fig_sh.update_layout(height=300, showlegend=False)
                st.plotly_chart(fig_sh, use_container_width=True)
