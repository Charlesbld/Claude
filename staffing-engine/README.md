# Moteur de staffing / capacity planning — service client (pas de 15 min)

Moteur local de **capacity planning** au pas de **15 minutes (UTC)**, conçu comme
**banc d'essai** pour valider les calculs avant réplication dans **Pigment**.

- **Python + pandas** pour le calcul (15 min géré nativement, index datetime UTC)
- **SQLite** comme magasin de données éditable (`data/staffing.db`, versionné)
- **Erlang C** pour le dimensionnement (ETP requis qui tient le SLA)
- **PuLP / CBC** pour l'optimiseur de répartition (coût minimal)
- **Streamlit** pour l'application (BI, réel vs forecast, couverture, édition)

> **Maille pivot** : `1 bucket de 15 min (UTC) × 1 level`. La capacité se **mutualise
> entre régions et tâches au sein d'un même level** (traduction automatique → pas de
> routage par langue). Level 1 = prestataires externes, Level 2 = escalades internes.

---

## Démarrage rapide

```bash
cd staffing-engine
pip install -r requirements.txt

python scripts/seed_db.py        # amorce data/staffing.db (données synthétiques 2026)
streamlit run app/app.py         # application (amorce la base si besoin)

pytest -q                        # validation des calculs (9 tests)
```

L'application comporte 5 pages :

| Page | Contenu |
|------|---------|
| **Accueil** | Schéma + explication du fonctionnement |
| **Réel vs Forecast** | Compare PAX/tâches réels au forecast, **décompose l'écart** (effet PAX vs effet contact rate), **édite le contact rate** par région×supply×task |
| **Couverture & coûts** | Erlang C, **bouton optimiseur**, heatmap, ETP requis vs capacité (15 min / heure / jour), agents-h par équipe, coût/jour, trous, **répartition éditable** |
| **Explorateur** | Pivots type BI sur **toutes** les tables (entrée + calculées) |
| **Éditer les données** | Modification directe des tables d'entrée (écriture SQLite) |

---

## Chaîne de calcul

```
PAX forecast (région×supply×mois)  ┐
contact rate forecast (×task)      ┘→ contacts mensuels
        → jour (profil hebdo) → bucket 15 min (profil intraday, heure locale → UTC)
        → charge → ERLANG C → ETP requis (= agents_online / (1 − shrinkage))
                                   │
équipes + disponibilités ──→ OPTIMISEUR IP (coût min, par jour de semaine)
                                   │
                          répartition d'agents (éditable)
                                   │
                  COUVERTURE · écarts · coût · occupation réelle · trous
```

### Formules clés

- **Contacts** = `PAX × contact_rate`. Le **contact rate réel** = `tâches_réelles / PAX_réels`.
- **ETP requis** (par bucket × level) : `agents_online` via **Erlang C** (cible ex. 95 % en ≤ 120 s,
  occupation max), puis `required_fte = agents_online / (1 − shrinkage)`.
- **Capacité** (par bucket × level) : `Σ agents × productivité` (l'effectif est **décidé** par
  l'optimiseur, il n'y a plus de headcount fixe).
- **Optimiseur** : variables = nb d'agents par équipe × shift ; contrainte = `Σ agents·prod ≥ ETP requis`
  sur chaque créneau ; objectif = `Σ agents · durée · coût_horaire` minimal. Résolu **par jour de
  semaine** (IP, PuLP/CBC) et réutilisé sur le mois.
- **Occupation réelle** (sortie) = `charge / (capacité × 0.25 h × (1 − shrinkage))`, à comparer à la cible.

### Décomposition réel vs forecast (page dédiée)

Pour chaque région×supply×task : `Δtâches = effet_PAX + effet_taux + interaction`, avec
`effet_PAX = (PAX_réel − PAX_fc)·cr_fc` et `effet_taux = PAX_fc·(cr_réel − cr_fc)`. On voit ainsi
si l'écart vient des **PAX** ou du **contact rate**, et on ajuste le contact rate en direct.

---

## Modèle de données (SQLite — `staffing/db.py`)

| Table | Rôle | Éditable |
|-------|------|:--------:|
| `region`, `supply`, `task_type` | référentiels (A2/A3/A5) | ✅ |
| `group_map` | mapping Group = Supply×Region par mois (A4) | ✅ |
| `pax_real`, `pax_forecast` | PAX réels (indicatif) / forecast (planif.) | ✅ |
| `tasks_real` | tâches réelles (⇒ contact rate réel) | ✅ |
| `contact_rate_forecast` | contact rate retenu, mois×région×supply×task | ✅ |
| `param_aht` | AHT par type de tâche (B1) | ✅ |
| `service_params` | cible Erlang C, shrinkage, occupation max (B2/B4) | ✅ |
| `team` | équipes : level, sourcing, fuseau, productivité, coût, plafond | ✅ |
| `team_availability` | fenêtres où une équipe peut travailler (D1) | ✅ |
| `profile_dow`, `profile_intraday` | profils de répartition (C2) | ✅ |
| `allocation` | répartition d'agents (sortie optimiseur, éditable) | ✅ |

Le registre `db.TABLES` décrit dimensions/mesures/notes de chaque table et pilote l'explorateur BI.

---

## Code

```
staffing/
  timespine.py   time spine UTC 15 min, fuseaux (A1)
  db.py          couche SQLite + registre des tables
  model.py       demande forecast → buckets + table réel vs forecast (C)
  erlang.py      dimensionnement Erlang C
  optimizer.py   optimiseur IP (répartition) + couverture (D'/E)
  reporting.py   heatmap, trous, synthèses (F)
app/
  app.py         entrée multipage (thème clair + navigation)
  common.py      cache / chargement / helpers
  pages_app.py   les 4 pages métier
scripts/seed_db.py   amorçage des données synthétiques
tests/test_v2.py     validation (Erlang C, conservation, optimiseur, décomposition)
```

---

## Hypothèses de modélisation

- Tout est stocké en **UTC** ; l'affichage est en **heure de Paris**.
- **Pooling** intra-level : la capacité couvre toutes les régions/tâches d'un même level.
- **Shrinkage** porté côté demande (gross-up de l'ETP requis), **productivité** côté offre
  (pas de double comptage). Identité : si `capacité == requis`, occupation réelle == cible.
- L'optimiseur résout **un jour de semaine type** réutilisé sur le mois (option : percentile de
  demande couvert, durées de shift).
- Persistance : base **SQLite versionnée** — committer `data/staffing.db` pour conserver les
  éditions (utile sur un déploiement Streamlit Cloud, dont le disque est éphémère).
