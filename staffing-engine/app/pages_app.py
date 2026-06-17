"""Pages de l'application (rendues via st.navigation)."""
from __future__ import annotations

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
# 1) DEMANDE — Réel vs Forecast
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
# 2) COUVERTURE & COÛTS
# =============================================================================
def _local(df):
    df = df.copy()
    df["local"] = df["bucket_utc"].dt.tz_convert(BUSINESS_TZ)
    df["date"] = df["local"].dt.date
    return df


def render_coverage():
    st.header("🗓️ Couverture & coûts")
    month = C.month_selector()

    with st.expander("⚙️ Optimiseur de répartition (coût minimal)", expanded=False):
        st.markdown("Le bouton calcule, par **programmation linéaire**, la répartition d'agents la "
                    "moins chère qui couvre l'ETP requis (Erlang C), **par jour de semaine**. "
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
    f = st.columns(5)
    level = f[0].selectbox("Level", sorted(matching["level"].unique()),
                           format_func=lambda l: f"Level {l}")
    dregions = f[1].multiselect("Région", sorted(demand["region_id"].unique()))
    dsupply = f[2].multiselect("Supply", sorted(demand["supply_id"].unique()))
    dtasks = f[3].multiselect("Type de tâche", sorted(demand["task_type_id"].unique()))
    gran = f[4].radio("Granularité", ["15 min", "Heure", "Jour"], horizontal=False)

    m = _local(matching[matching["level"] == level])
    days = sorted(m["date"].unique())
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
    piv = reporting.heatmap_pivot(matching[matching["level"] == level], level, metric, BUSINESS_TZ)
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
    gaps = reporting.gap_intervals(matching[matching["level"] == level], top=15)
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
        e["slot_utc"] = range(96)
        long = e.melt(id_vars="slot_utc", value_vars=teams_lvl, var_name="team_id", value_name="agents")
        long = long[long["agents"] > 0]
        long["dow"] = dow
        keep = alloc[~((alloc["dow"] == dow) & (alloc["team_id"].isin(teams_lvl)))]
        C.save_table("allocation", pd.concat([keep, long[["dow", "slot_utc", "team_id", "agents"]]], ignore_index=True))
        st.success("Répartition enregistrée.")
        st.rerun()


# =============================================================================
# 3) EXPLORATEUR DE DONNÉES (type BI)
# =============================================================================
COMPUTED = {
    "demand": ("Demande forecast (bucket)", ["bucket_utc", "level", "task_type_id", "group_id", "region_id", "supply_id"],
               ["contacts", "workload_hours"]),
    "required": ("ETP requis Erlang C (bucket×level)", ["bucket_utc", "level"],
                 ["contacts", "workload_hours", "agents_online", "required_fte", "aht_eff"]),
    "coverage": ("Couverture (bucket×level)", ["bucket_utc", "level"],
                 ["required_fte", "effective_capacity", "agents", "gap_fte", "cost", "coverage_ratio", "real_occupancy"]),
    "supply_team": ("Capacité par équipe (bucket)", ["bucket_utc", "team_id", "level"],
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
            piv = pd.pivot_table(df, index=rows, columns=None if cols == "(aucune)" else cols,
                                 values=val, aggfunc=aggf, fill_value=0)
            st.dataframe(piv, width="stretch")
            st.download_button("⬇️ Télécharger (CSV)", piv.to_csv().encode(), f"{name}_pivot.csv")
        else:
            st.info("Choisissez au moins une dimension en ligne.")
    with tab_raw:
        st.dataframe(df.head(5000), width="stretch", hide_index=True)
        st.caption(f"{len(df):,} lignes — {', '.join(df.columns)}")


# =============================================================================
# 4) ÉDITION DES RÉFÉRENTIELS
# =============================================================================
def _group_map_editor():
    """Pivot éditable du mapping group : combos (Region×Supply) × mois, actif/inactif."""
    gm = C.table("group_map")
    cols_order = list(gm.columns)
    months = sorted(gm["month"].unique())
    piv = (gm.pivot_table(index=["group_id", "region_id", "supply_id"], columns="month",
                          values="active", fill_value=0)
           .reindex(columns=months, fill_value=0).reset_index())
    for m in months:
        piv[m] = piv[m].astype(bool)
    st.caption("Cochez les mois où chaque **group** (Region×Supply) est actif. "
               "Vous pouvez ajouter une ligne (renseignez group_id / region_id / supply_id).")
    colcfg = {m: st.column_config.CheckboxColumn(m[2:]) for m in months}
    colcfg |= {c: st.column_config.TextColumn(c) for c in ["group_id", "region_id", "supply_id"]}
    edited = st.data_editor(piv, width="stretch", hide_index=True, num_rows="dynamic",
                            column_config=colcfg, key="gm_pivot")
    if st.button("💾 Enregistrer le mapping group", type="primary"):
        e = edited.dropna(subset=["group_id", "region_id", "supply_id"])
        long = e.melt(id_vars=["group_id", "region_id", "supply_id"], value_vars=months,
                      var_name="month", value_name="active")
        long["active"] = long["active"].fillna(False).astype(int)
        C.save_table("group_map", long[cols_order])
        st.success("Mapping group enregistré.")
        st.rerun()


def render_editor():
    st.header("✏️ Éditer les données")
    st.markdown("Modifiez directement les tables d'entrée. **« Enregistrer »** écrit en base SQLite "
                "et recalcule tout. (Les PAX/tâches sont aussi modifiables ici ; voir la page Réel vs Forecast "
                "pour le contact rate par région×supply.)")
    name = st.selectbox("Table à éditer", db.EDITABLE,
                        format_func=lambda n: db.TABLES[n].label)
    spec = db.TABLES[name]
    if spec.note:
        st.info(spec.note)
    if name == "group_map":
        _group_map_editor()
        return
    df = C.table(name)
    edited = st.data_editor(df, width="stretch", hide_index=True, num_rows="dynamic", key=f"ed_{name}")
    if st.button("💾 Enregistrer", type="primary"):
        C.save_table(name, edited)
        st.success(f"Table « {spec.label} » enregistrée.")
        st.rerun()
