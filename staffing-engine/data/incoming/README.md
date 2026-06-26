# `data/incoming/` — dépôt des fichiers mensuels

Déposez ici les **CSV mensuels** ; ils sont chargés dans la base SQLite par
upsert (les lignes du même mois sont remplacées, le reste est conservé).

## Comment ça marche

1. Partez d'un **modèle** : page *Éditer les données → 📥 Importer*, bouton
   « Modèle CSV », ou les exemples dans [`../templates/`](../templates).
2. Nommez le fichier **d'après sa table cible**. Ce qui suit `__` est libre :
   - `pax_real.csv` ou `pax_real__2026-07.csv` → table `pax_real`
   - `contact_rate_forecast__juillet.csv` → table `contact_rate_forecast`
3. Posez le fichier ici, puis **soit** :
   - **en local** : `python scripts/ingest.py` (ou le bouton « Ré-ingérer » dans l'app) ;
   - **sur GitHub / Cloud** : poussez le fichier ; au redéploiement la base est
     réamorcée puis les fichiers d'`incoming/` sont ingérés automatiquement.

## Tables typiquement poussées chaque mois

| Fichier (table) | Clé d'upsert | Colonnes |
|-----------------|--------------|----------|
| `pax_forecast` | month × region_id × supply_id | `month, region_id, supply_id, pax` |
| `pax_real` | month × region_id × supply_id | `month, region_id, supply_id, pax` |
| `tasks_real` | month × region_id × supply_id × task_type_id | `month, region_id, supply_id, task_type_id, tasks` |
| `contact_rate_forecast` | month × region_id × supply_id × task_type_id | `month, region_id, supply_id, task_type_id, contact_rate` |
| `group_map` | month × region_id × supply_id | `month, region_id, supply_id, group_id, active` |

> `month` au format `AAAA-MM` (ex. `2026-07`). Les identifiants (`region_id`,
> `supply_id`, `task_type_id`) doivent exister dans les référentiels.
> Réingérer le même fichier ne crée pas de doublon (idempotent).
