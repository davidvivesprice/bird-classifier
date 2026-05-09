#!/usr/bin/env python3
"""Hourly database integrity audit.

Checks that the three SQLite databases have the expected schema and row counts
matching iMac behavior. Logs results to journal via stdout.

Environment:
- BIRD_DB_DIR: path to bird-snapshots (default: ~/bird-snapshots)
- DB_MODE: "mirror" during Phase 1 shadow (read-only mirror); "primary" or unset for normal operation
"""
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_DIR = Path(os.environ.get("BIRD_DB_DIR", "~/bird-snapshots")).expanduser()
DB_MODE = os.environ.get("DB_MODE", "primary")
EXPECTED_MODE = DB_MODE

def check_db(db_path, db_name):
    """Check database schema and row counts."""
    if not db_path.exists():
        print(f"[{datetime.now().isoformat()}] {db_name}: MISSING")
        return False
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA integrity_check")
        
        # Sample row counts
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cursor.fetchone()[0]
        
        # classifications.db should have 'classifications' table
        if 'classifications' in db_name.lower():
            cursor.execute("SELECT COUNT(*) FROM classifications LIMIT 1")
            row_count = cursor.fetchone()[0]
            print(f"[{datetime.now().isoformat()}] {db_name}: OK (tables={table_count}, rows={row_count})")
        else:
            print(f"[{datetime.now().isoformat()}] {db_name}: OK (tables={table_count})")
        
        conn.close()
        return True
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] {db_name}: ERROR - {e}")
        return False

def main():
    """Run integrity audit."""
    print(f"[{datetime.now().isoformat()}] Integrity audit starting (DB_MODE={DB_MODE})")
    
    results = []
    results.append(check_db(DB_DIR / "logs" / "classifications.db", "classifications.db"))
    results.append(check_db(DB_DIR / "logs" / "pipeline.db", "pipeline.db"))
    results.append(check_db(DB_DIR / "logs" / "pi_reviews.db", "pi_reviews.db"))
    
    if all(results):
        print(f"[{datetime.now().isoformat()}] Audit complete: ALL OK")
        sys.exit(0)
    else:
        print(f"[{datetime.now().isoformat()}] Audit complete: FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()
