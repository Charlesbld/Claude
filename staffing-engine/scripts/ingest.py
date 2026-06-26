#!/usr/bin/env python3
"""Ingestion des CSV mensuels (data/incoming/) dans la base SQLite.

    python scripts/ingest.py                  # ingère tous les data/incoming/*.csv
    python scripts/ingest.py a.csv b.csv      # ingère des fichiers précis

Chaque fichier est nommé d'après sa table cible (« pax_real.csv », ou
« pax_real__2026-07.csv »). Upsert par clé : un mois poussé s'ajoute / remplace
sans toucher au reste. Voir staffing/ingest.py et data/incoming/README.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from staffing import db, ingest  # noqa: E402


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not db.db_exists():
        print("[!] Base absente — lancez d'abord : python scripts/seed_db.py")
        return
    reports = [ingest.ingest_file(p) for p in argv] if argv else ingest.ingest_dir()
    if not reports:
        print(f"[i] Aucun fichier .csv à ingérer dans {ingest.INCOMING}")
        return
    for r in reports:
        print(f"[OK] {r['file']:34s} → {r['table']:22s} "
              f"{r['rows_in']:>6d} lignes  (remplacées {r['rows_replaced']}, total {r['total_after']})")


if __name__ == "__main__":
    main()
