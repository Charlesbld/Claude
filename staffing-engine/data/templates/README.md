# `data/templates/` — modèles de fichiers d'ingestion

Exemples **réalistes** (extraits du mois `2026-07`) montrant le format attendu
de chaque CSV mensuel. Copiez-en un, remplacez les valeurs par les vôtres,
renommez-le si besoin (le nom **avant `__`** désigne la table cible), puis
déposez-le dans [`../incoming/`](../incoming) et lancez l'ingestion.

| Modèle | Table | Clé d'upsert |
|--------|-------|--------------|
| `pax_forecast__2026-07.csv` | `pax_forecast` | month × region_id × supply_id |
| `pax_real__2026-07.csv` | `pax_real` | month × region_id × supply_id |
| `tasks_real__2026-07.csv` | `tasks_real` | month × region_id × supply_id × task_type_id |
| `contact_rate_forecast__2026-07.csv` | `contact_rate_forecast` | month × region_id × supply_id × task_type_id |
| `group_map__2026-07.csv` | `group_map` | month × region_id × supply_id |

> Ces fichiers sont de simples **exemples** : ils ne sont pas ingérés
> automatiquement (seul `data/incoming/` l'est).
