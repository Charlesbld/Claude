# Moteur de staffing / capacity planning — service client (pas de 15 min)

Moteur local de **capacity planning** au pas de **15 minutes (UTC)**, conçu comme
**banc d'essai** pour valider les calculs avant réplication dans **Pigment**.
Python + pandas pour le calcul, Streamlit pour la restitution, données d'entrée
en CSV (parquet en sortie).

> **Maille pivot du modèle** : `1 bucket de 15 min (UTC) × 1 segment (level + type de tâche)`.
> La demande (charge) et l'offre (capacité) produisent toutes deux cette maille
> pour pouvoir **se joindre**.

---

## Démarrage rapide

```bash
cd staffing-engine
pip install -r requirements.txt

python scripts/generate_synthetic_data.py   # (A) génère data/raw/*.csv (déterministe)
python scripts/run_pipeline.py               # (A→F) écrit data/processed/*.parquet + valide
streamlit run app/app.py                     # (F) tableau de bord interactif

pytest -q                                    # validation des calculs (9 tests)
```

### Périmètre du run (MVP d'abord)

Le pipeline construit **toujours** demande et offre sur l'horizon complet (UTC) —
ce qui garantit une conversion de fuseau correcte — puis applique un **périmètre**
en filtrage de sortie :

```bash
python scripts/run_pipeline.py --mvp                       # étape 1 : Level 1, une journée type (mardi 16/06)
python scripts/run_pipeline.py --levels 1 --regions FR --start 2026-06-16 --end 2026-06-17
python scripts/run_pipeline.py                             # étape 3 : généralisation (tous levels, tout l'horizon)
```

---

## Architecture en 6 couches → code

| Couche | Rôle | Module |
|--------|------|--------|
| **A** Référentiels | A1 time spine UTC 15 min, calendriers pays, courbe de délai (lag) ; A2 supply, A3 region, A4 group mensuel, A5 types de tâche, A6 équipes, A7 routage | `staffing/timespine.py`, `staffing/referentials.py` |
| **B** Paramètres | B1 AHT (tâche×group, fallback), B2 productivité/shrinkage/occupation cible, B3 coûts horaires, B4 SLA/occupation max/marge | `staffing/params.py`, `config.yaml` |
| **C** Demande (charge) | C1 volume mensuel → jour → bucket ; C2 profils (départ⊗lag, intraday) ; C3 charge brute → **ETP requis** | `staffing/demand.py` |
| **D** Offre (capacité) | D1 shifts (heure locale → UTC 15 min) ; D2 capacité = effectif×productivité×dispo | `staffing/supply.py` |
| **E** Rapprochement | E1 couverture requis vs dispo par bucket×level (contrainte L1↔externe / L2↔interne) ; E2 écarts, coût, **occupation réelle** | `staffing/matching.py` |
| **F** Restitution | F1 heatmap, F2 synthèse coûts & ETP (ventilée group/région), F3 trous prioritaires | `staffing/reporting.py`, `app/app.py` |

---

## Modèle de calcul

Tout est exprimé en **ETP « concurrents » par bucket** (nb d'agents simultanés sur
15 min), ce qui rend demande et offre directement comparables.

**Demande (C3)** — le shrinkage et l'occupation **cible** sont portés ici (gross-up) :

```
charge_brute(bucket)   = contacts(bucket) × AHT
ETP_requis(bucket)     = charge_brute / 0.25h / occupation_cible / (1 − shrinkage)
```

**Offre (D2)** — bodies réellement planifiés, ajustés de la productivité :

```
capacité_effective(bucket) = Σ_équipes  effectif_planifié × productivité
```

**Rapprochement (E2)** — l'occupation **réelle** est recalculée en sortie :

```
écart(bucket)            = capacité_effective − ETP_requis
occupation_réelle(bucket)= charge_brute / (capacité_effective × 0.25h × (1 − shrinkage))
```

> **Identité de cohérence** (testée) : si `capacité == ETP_requis`, alors
> `occupation_réelle == occupation_cible`. Sur-staffing ⇒ occupation < cible ;
> sous-staffing ⇒ occupation > cible. Le shrinkage est appliqué **une seule fois**
> (côté demande), jamais en double.

### Décomposition de la demande (C1/C2)

```
contacts_mensuels(mois, group, tâche) = passagers(mois, group) × taux_contact(tâche, group)

mensuel → jour  : forme[arrivée a] = Σ_lag  P(lag) × poids_départ( jour_de_semaine(a − lag) )
                  (profil de DÉPART convolué avec la COURBE DE DÉLAI, renormalisé sur le mois)
jour → bucket   : profil INTRADAY (96 créneaux) par jour de semaine, en heure LOCALE
                  puis converti en UTC (gère décalage horaire + heure d'été)
```

---

## Décisions de modélisation (à garder en tête pour Pigment)

1. **Maille de matching = `(bucket_utc, level)`.** Le segment du modèle est
   `(level, type de tâche)`, mais la contrainte de **pooling** (traduction
   automatique ⇒ pas de routage par langue ; A7 très mince) fait que la capacité
   se mutualise entre régions **et** types de tâche au sein d'un level. La demande
   est calculée à la maille fine `(bucket, level, tâche, group, région)` pour la
   restitution, puis **agrégée à `(bucket, level)`** pour le rapprochement.
   *Pour passer à un routage par type de tâche : enrichir A7 (`team_skills`) et
   remonter la maille de matching d'un cran.*
2. **Tout est stocké en UTC.** `business_timezone` (Europe/Paris) ne sert qu'à
   l'affichage (axe des heatmaps). Conversions locale↔UTC via `tz_localize` /
   `tz_convert` (DST géré).
3. **Shrinkage / occupation côté demande ; productivité côté offre.** Évite tout
   double comptage (cf. identité ci-dessus).
4. **Conservation du volume** garantie par renormalisation de la forme journalière
   sur chaque mois (le report de fuseau est absorbé par ±1 jour de marge dans
   l'horizon). Test : conservation à 100 %.
5. **Contrainte structurante** L1 ⇔ externe / L2 ⇔ interne validée au chargement
   des référentiels **et** vérifiée sur l'offre.

---

## Données d'entrée (`data/raw/`, générées)

| Fichier | Couche | Colonnes clés |
|---------|--------|---------------|
| `dim_supply.csv` | A2 | supply_id, supply_label |
| `dim_region.csv` | A3 | region_id, region_label, country_code, timezone |
| `dim_group_monthly.csv` | A4 | month, group_id, supply_id, region_id, passengers, active |
| `dim_task_type.csv` | A5 | task_type_id, task_type_label, level |
| `dim_team.csv` | A6 | team_id, level, sourcing, timezone, headcount, productivity, hourly_cost |
| `team_skills.csv` | A7 | team_id, task_type_id, can_handle |
| `param_aht.csv` | B1 | task_type_id, group_id, aht_seconds |
| `param_contact_rate.csv` | C1 | task_type_id, group_id, contact_rate |
| `profile_departure_dow.csv` | C2 | dow, weight |
| `lag_curve.csv` | A1/C2 | lag_days, weight |
| `profile_intraday.csv` | C2 | dow, slot_local, weight |

Univers synthétique : 4 régions (FR/ES/IT/GB) × 4 modes (avion/train/bus/ferry),
4 types de tâche (2 en Level 1 externe, 2 en Level 2 interne), 7 équipes
(4 BPO externes « follow-the-sun » + 3 équipes internes UE), ancrage juin 2026.

## Sorties (`data/processed/`, régénérables)

`demand.parquet` (maille fine), `supply.parquet` (par équipe), `matching.parquet`
(par `bucket_utc × level`), `run_meta.json`.

---

## Pistes d'extension

- Volume : brancher SQLite/DuckDB si les CSV deviennent trop gros (les loaders
  isolent déjà les I/O).
- Plusieurs mois : `dim_group_monthly` accepte déjà plusieurs `month` (snapshots).
- Routage fin par type de tâche : enrichir `team_skills` et la maille E.
- Optimisation de shifts : la couche D fournit la capacité ; un optimiseur peut
  itérer sur `shifts.csv` pour combler les trous de F3.
