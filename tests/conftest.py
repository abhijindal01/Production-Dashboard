"""Shared fixtures: Part-DB + production database builders and a Grafana
variable substitutor so dashboard SQL can be executed exactly as Grafana
would send it (after frontend interpolation)."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------- Grafana vars

def grafana_sql(query, project="All", from_ms=None, to_ms=None):
    """Simulate Grafana frontend interpolation for $project/${__from}/${__to}."""
    if from_ms is None:
        from_ms = to_epoch_ms("2026-08-01T00:00:00Z")
    if to_ms is None:
        to_ms = to_epoch_ms("2026-09-09T00:00:00Z")
    return (query
            .replace("$project", project)
            .replace("${__from}", str(from_ms))
            .replace("${__to}", str(to_ms)))


def to_epoch_ms(rfc3339):
    dt = datetime.strptime(rfc3339, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_dashboard():
    with open(REPO_ROOT / "production-dashboard-sqlite.json") as fh:
        return json.load(fh)


def iter_panel_queries(dashboard):
    """Yield (panel_id, title, query_type, ref_id, sql) for every target."""
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            yield (panel.get("id"), panel.get("title"),
                   target.get("queryType"), target.get("refId"),
                   target.get("rawQueryText"))


# ---------------------------------------------------------------- Part-DB

def make_partdb(path, parts):
    """Create a Part-DB-like SQLite file.

    parts: list of dicts: {"id": int, "comment": str, "lots": [amounts]}.
    """
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("CREATE TABLE parts (id INTEGER PRIMARY KEY, comment TEXT)")
    cur.execute("CREATE TABLE part_lots "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                " id_part INTEGER, amount REAL)")
    for part in parts:
        cur.execute("INSERT INTO parts (id, comment) VALUES (?, ?)",
                    (part["id"], part.get("comment")))
        for amount in part.get("lots", []):
            cur.execute("INSERT INTO part_lots (id_part, amount) VALUES (?, ?)",
                        (part["id"], amount))
    con.commit()
    con.close()
    return path


def set_part_lots(path, part_id, lots):
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("DELETE FROM part_lots WHERE id_part = ?", (part_id,))
    for amount in lots:
        cur.execute("INSERT INTO part_lots (id_part, amount) VALUES (?, ?)",
                    (part_id, amount))
    con.commit()
    con.close()


# ---------------------------------------------------------------- Production DB

def apply_canonical_schema(con):
    con.executescript(
        (REPO_ROOT / "production_schema.sql").read_text())


def make_proddb(path):
    con = sqlite3.connect(str(path))
    apply_canonical_schema(con)
    con.commit()
    con.close()
    return path


@pytest.fixture()
def seeded_proddb(tmp_path):
    """Production DB with deterministic sample data (see docstring table).

    Projects:
      Heatsink    target 1000 | stages: After sand blasting(1), Anodised(2)
      Drone Frame target 500  | stages: Cutting(1), Assembly(2)
    daily_production:
      Heatsink/Anodised:            2026-08-24 +56; 2026-08-26 +10,+15,-15,-10
      Heatsink/After sand blasting: 2026-08-25 +40
      Drone Frame/Cutting:          2026-08-21 +100
      Drone Frame/Assembly:         2026-08-22 +60; 2026-08-23 -5
    Net totals: All=251, Heatsink=96, Drone Frame=155.
    Added (gross +): 281. Corrections (gross -): 30.
    quality_tests: Heatsink 50/48/2, Drone Frame 60/60/0 -> All 98.2%.
    rework_log: Heatsink 3 on 2026-08-25.
    """
    path = tmp_path / "production.db"
    con = sqlite3.connect(str(path))
    apply_canonical_schema(con)
    cur = con.cursor()

    # NOTE: projects first with NULL current stage (FK-safe), stages
    # second, then wire up current_stage_id.
    cur.execute(
        "INSERT INTO production_projects (id, project_name, part_id, "
        "start_date, target_quantity, status, current_stage_id) VALUES "
        "(1, 'Heatsink', 225, '2026-08-24', 1000, 'in_progress', NULL), "
        "(2, 'Drone Frame', 339, '2026-08-20', 500, 'in_progress', NULL)")
    cur.execute(
        "INSERT INTO production_stages (id, project_id, stage_name, "
        "sequence_order) VALUES "
        "(1, 1, 'After sand blasting', 1), (2, 1, 'Anodised', 2), "
        "(3, 2, 'Cutting', 1), (4, 2, 'Assembly', 2)")
    cur.execute("UPDATE production_projects SET current_stage_id = 2 "
                "WHERE id = 1")
    cur.execute("UPDATE production_projects SET current_stage_id = 4 "
                "WHERE id = 2")
    cur.execute(
        "INSERT INTO daily_production (project_id, stage_id, production_date,"
        " quantity_produced) VALUES "
        "(1, 2, '2026-08-24', 56), (1, 2, '2026-08-26', 10), "
        "(1, 2, '2026-08-26', 15), (1, 2, '2026-08-26', -15), "
        "(1, 2, '2026-08-26', -10), (1, 1, '2026-08-25', 40), "
        "(2, 3, '2026-08-21', 100), (2, 4, '2026-08-22', 60), "
        "(2, 4, '2026-08-23', -5)")
    cur.execute(
        "INSERT INTO quality_tests (project_id, stage_id, test_date, "
        "quantity_tested, quantity_passed, quantity_failed) VALUES "
        "(1, 2, '2026-08-26', 50, 48, 2), "
        "(2, 4, '2026-08-22', 60, 60, 0)")
    cur.execute(
        "INSERT INTO rework_log (project_id, stage_id, rework_date, "
        "quantity_reworked, reason) VALUES (1, 1, '2026-08-25', 3, 'scratch')")
    cur.execute(
        "INSERT INTO part_stock_snapshot (part_id, last_known_quantity, "
        "last_checked) VALUES "
        "(223, 40, datetime('now')), (225, 56, datetime('now')), "
        "(339, 55, '2026-01-01 00:00:00')")
    cur.execute(
        "INSERT INTO production_part_mapping (part_id, project_id, stage_id, "
        "project_name, stage_name) VALUES "
        "(223, 1, 1, 'Heatsink', 'After sand blasting'), "
        "(225, 1, 2, 'Heatsink', 'Anodised'), "
        "(339, 2, 4, 'Drone Frame', 'Assembly')")
    con.commit()
    con.close()
    return path


@pytest.fixture()
def dashboard():
    return load_dashboard()
