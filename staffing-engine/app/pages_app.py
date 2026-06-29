"""Pages de l'application (rendues via st.navigation)."""
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


# =============================================================================
# ① Données sources
# =============================================================================
def render_sources():
    """① Données sources — aperçu de toutes les tables d'entrée."""
    st.header("📋 ① Données sources")
    st.markdown("""
    Cette page montre l'état des tables d'entrée du modèle.
    Tout le moteur est piloté par ces données — on peut les modifier dans **Éditer les données**.
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

    base = (pf[pf["month"] == month]
            .merge(crf[crf["month"] == month], on=["month", "region_id", "supply_id"])
            .merge(gmap[(gmap["month"] == month) & (gmap["active"] == 1)]
                   [["region_id", "supply_id", "group_id"]], on=["region_id", "supply_id"])
            .merge(grp, on="group_id", how="left"))
    base["contacts"] = base["pax"] * base["contact_rate"]

    C.kpi_row([
        ("Total contacts", f"{base['contacts'].sum():,.0f}"),
        ("PAX total", f"{base['pax'].sum():,.0f}"),
        ("CR moyen (toutes tâches)", f"{base['contacts'].sum() / base['pax'].sum():.4f}" if base['pax'].sum() > 0 else "—"),
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
        ("Shrinkage gross-up",  f"× {1 / (1 - float(sub['shrinkage'].iloc[0])):.2f}"),
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

    # Moyenne de real_occupancy pondérée par agents
    joined["occ_x_agents"] = joined["real_occupancy"] * joined["agents"]
    team_occ = (
        joined.groupby("team_id")
        .apply(lambda g: g["occ_x_agents"].sum() / g["agents"].sum()
               if g["agents"].sum() > 0 else np.nan)
        .reset_index()
        .rename(columns={0: "occupation_moyenne"})
    )

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
                C.run_optimizer(month, percentile=pct, shift_lengths=lengths)
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
    edited = st.data_editor(grid, width="stretch", hide_index=True, height=300, key=f"alloc_{dow}_{level}")
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
    val = p[2].selectbox("Valeur", measures)
    aggf = p[3].selectbox("Agrégation", ["sum", "mean", "max", "min", "count"])

    tab_pivot, tab_raw = st.tabs(["Pivot", "Données brutes"])
    with tab_pivot:
        if rows:
            # Fix: use fill_value=0 only for numeric columns, else fill_value=""
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


def render_editor():
    st.header("✏️ Éditer les données")
    st.markdown("Modifiez directement les tables d'entrée. **« Enregistrer »** écrit en base SQLite "
                "et recalcule tout. (Les PAX/tâches sont aussi modifiables ici ; voir la page Réel vs Forecast "
                "pour le contact rate par région×supply.)")

    with st.expander("📥 Importer des fichiers (workflow mensuel)", expanded=False):
        _import_section()

    name = st.selectbox("Table à éditer", db.EDITABLE,
                        format_func=lambda n: db.TABLES[n].label)
    spec = db.TABLES[name]
    if spec.note:
        st.info(spec.note)
    if name == "group_map":
        _group_map_editor()
        return
    if name == "group":
        st.subheader("Groupes commerciaux")
        df = C.table("group")
        edited = st.data_editor(df, width="stretch", hide_index=True, num_rows="dynamic",
                                key="ed_group",
                                column_config={
                                    "group_id": st.column_config.TextColumn("ID groupe (clé)"),
                                    "group_label": st.column_config.TextColumn("Libellé"),
                                })
        if st.button("💾 Enregistrer les groupes", type="primary"):
            C.save_table("group", edited)
            st.success("Table « Groupes commerciaux » enregistrée.")
            st.rerun()
        return
    if name == "team_group":
        st.subheader("Affectation équipes → groupes")
        teams = C.table("team")[["team_id", "team_label", "level"]]
        groups = C.table("group")["group_id"].tolist() if not C.table("group").empty else []
        df = C.table("team_group")
        edited = st.data_editor(df, width="stretch", hide_index=True, num_rows="dynamic",
                                key="ed_team_group",
                                column_config={
                                    "team_id": st.column_config.SelectboxColumn("Équipe",
                                                                                options=teams["team_id"].tolist()),
                                    "group_id": st.column_config.SelectboxColumn("Groupe", options=groups),
                                })
        if st.button("💾 Enregistrer l'affectation équipes→groupes", type="primary"):
            C.save_table("team_group", edited)
            st.success("Table « Affectation équipes → groupes » enregistrée.")
            st.rerun()
        return
    df = C.table(name)
    edited = st.data_editor(df, width="stretch", hide_index=True, num_rows="dynamic", key=f"ed_{name}")
    if st.button("💾 Enregistrer", type="primary"):
        C.save_table(name, edited)
        st.success(f"Table « {spec.label} » enregistrée.")
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
            cost_group.to_excel(writer, sheet_name="Synthèse", index=False, startrow=0)

            # ETP requis vs capacité (par level)
            by_level_out = by_level[
                ["level", "total_cost", "required_fte_peak", "capacity_peak",
                 "required_fte_avg", "capacity_avg", "hours_understaffed"]
            ]
            startrow = len(cost_group) + 3
            by_level_out.to_excel(writer, sheet_name="Synthèse", index=False, startrow=startrow)
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
                comp.to_excel(writer, sheet_name="Comparatif M vs M-1", index=False)
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
                gaps_out.to_excel(writer, sheet_name="Gaps prioritaires", index=False)
        else:
            pd.DataFrame({"info": ["Aucune donnée de couverture disponible."]}).to_excel(
                writer, sheet_name="Gaps prioritaires", index=False)

    buf.seek(0)
    return buf


def render_monthly_report():
    """Rapport mensuel synthétique exportable (IMP-1)."""
    st.header("📊 Rapport mensuel")
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
