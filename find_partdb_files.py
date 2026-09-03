#!/usr/bin/env python3
"""
Find every SQLite database under a folder and show which one is the
Part-DB source (has the 'parts' table) and which one is the production
database (has 'production_projects').

Usage
-----
    python3 find_partdb_files.py [FOLDER ...]

    Default: scan the current directory recursively.
    Examples:
        python3 find_partdb_files.py /home/server/Production
        python3 find_partdb_files.py ~/Production/production-db
"""

import os
import sqlite3
import sys


def table_names(path):
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = con.cursor()
        names = [
            str(row[0])
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        con.close()
        return names
    except Exception as error:
        return [f"<error: {error}>"]


def main():
    roots = sys.argv[1:] or ["."]
    for root in roots:
        print(f"\n=== Scanning: {root} ===")
        for dirpath, dirnames, filenames in os.walk(os.path.abspath(root)):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in {".git", "node_modules", "__pycache__"}
            ]
            for filename in sorted(filenames):
                if not filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
                    continue
                path = os.path.join(dirpath, filename)
                size = os.path.getsize(path)
                tables = table_names(path)
                
                if "parts" in tables and "part_lots" in tables:
                    kind = "PART-DB SOURCE"
                elif "production_projects" in tables:
                    kind = "PRODUCTION DATABASE"
                elif not tables or tables == ["<error: ..."]:
                    kind = "EMPTY / NOT A DATABASE"
                else:
                    kind = "other"
                print(f"  {path}  ({size} bytes)")
                print(f"      -> {kind}")
                print(f"      tables: {', '.join(tables[:12])}")


if __name__ == "__main__":
    main()
