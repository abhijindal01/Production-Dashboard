"""Tests for db_maintenance.py: migrate / check / clean-html."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_maintenance import main  # noqa: E402
from tests.conftest import apply_canonical_schema  # noqa: E402


def query(path, sql, params=()):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def make_legacy_db(path):
    """A pre-v2 database: missing columns/tables/indexes."""
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE production_projects (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, project_name TEXT NOT NULL, part_id INTEGER, "
        "start_date TEXT NOT NULL, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    cur.execute(
        "CREATE TABLE production_stages (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, project_id INTEGER NOT NULL, stage_name TEXT "
        "NOT NULL, sequence_order INTEGER NOT NULL)")
    cur.execute(
        "CREATE TABLE daily_production (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, project_id INTEGER NOT NULL, stage_id INTEGER "
        "NOT NULL, production_date TEXT NOT NULL, quantity_produced "
        "INTEGER NOT NULL DEFAULT 0)")
    cur.execute(
        "INSERT INTO production_projects (project_name, start_date) "
        "VALUES ('Legacy', '2026-01-01')")
    cur.execute(
        "INSERT INTO production_stages (project_id, stage_name, "
        "sequence_order) VALUES (1, 'Only', 1)")
    con.commit()
    con.close()
    return path


def make_healthy_db(path):
    con = sqlite3.connect(str(path))
    apply_canonical_schema(con)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO production_projects (id, project_name, start_date, "
        "target_quantity, status, current_stage_id) VALUES "
        "(1, 'Healthy', '2026-08-01', 100, 'in_progress', NULL)")
    cur.execute(
        "INSERT INTO production_stages (id, project_id, stage_name, "
        "sequence_order) VALUES (1, 1, 'Main', 1)")
    cur.execute("UPDATE production_projects SET current_stage_id = 1 "
                "WHERE id = 1")
    cur.execute(
        "INSERT INTO daily_production (project_id, stage_id, production_date,"
        " quantity_produced) VALUES (1, 1, '2026-09-01', 10)")
    cur.execute(
        "INSERT INTO quality_tests (project_id, stage_id, test_date, "
        "quantity_tested, quantity_passed, quantity_failed) VALUES "
        "(1, 1, '2026-09-01', 10, 10, 0)")
    cur.execute(
        "INSERT INTO rework_log (project_id, stage_id, rework_date, "
        "quantity_reworked) VALUES (1, 1, '2026-09-01', 1)")
    cur.execute(
        "INSERT INTO part_stock_snapshot (part_id, last_known_quantity, "
        "last_checked) VALUES (9, 10, datetime('now'))")
    cur.execute(
        "INSERT INTO production_part_mapping (part_id, project_id, stage_id, "
        "project_name, stage_name) VALUES (9, 1, 1, 'Healthy', 'Main')")
    con.commit()
    con.close()
    return path


def make_polluted_db(path):
    """Mimics real v1 damage: HTML-suffixed duplicate project + stage."""
    con = sqlite3.connect(str(path))
    apply_canonical_schema(con)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO production_projects (id, project_name, start_date, "
        "target_quantity, status, current_stage_id) VALUES "
        "(1, 'Heatsink', '2026-08-24', 1000, 'in_progress', NULL), "
        "(2, 'Heatsink</span> <span style=\"color:rgb(1);\">', "
        "'2026-08-25', 500, 'planned', NULL)")
    cur.execute(
        "INSERT INTO production_stages (id, project_id, stage_name, "
        "sequence_order) VALUES "
        "(1, 1, 'Anodised', 1), "
        "(2, 2, 'Anodised</span>', 1), (3, 2, 'Extra', 2)")
    cur.execute("UPDATE production_projects SET current_stage_id = 1 "
                "WHERE id = 1")
    cur.execute("UPDATE production_projects SET current_stage_id = 2 "
                "WHERE id = 2")
    cur.execute(
        "INSERT INTO daily_production (project_id, stage_id, production_date,"
        " quantity_produced) VALUES "
        "(1, 1, '2026-08-24', 10), (2, 2, '2026-08-25', 5), "
        "(2, 3, '2026-08-25', 7)")
    cur.execute(
        "INSERT INTO production_part_mapping (part_id, project_id, stage_id, "
        "project_name, stage_name) VALUES "
        "(101, 1, 1, 'Heatsink', 'Anodised'), "
        "(102, 2, 2, 'Heatsink</span> <span style=\"color:rgb(1);\">', "
        "'Anodised</span>')")
    cur.execute(
        "INSERT INTO part_stock_snapshot (part_id, last_known_quantity, "
        "last_checked) VALUES (101, 10, datetime('now')), "
        "(102, 5, datetime('now'))")
    con.commit()
    con.close()
    return path


# ---------------------------------------------------------------- migrate

def test_migrate_repairs_legacy_db(tmp_path):
    path = make_legacy_db(tmp_path / "legacy.db")
    assert main(["migrate", str(path)]) == 0
    cols = {r["name"] for r in
            query(path, "PRAGMA table_info(production_projects)")}
    assert {"target_quantity", "status", "current_stage_id"} <= cols
    tables = {r["name"] for r in
              query(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quality_tests", "rework_log", "part_stock_snapshot",
            "production_part_mapping"} <= tables
    assert query(path, "SELECT target_quantity, status, current_stage_id "
                       "FROM production_projects")[0] == {
        "target_quantity": 0, "status": "planned", "current_stage_id": 1}
    assert main(["migrate", str(path)]) == 0  # idempotent


def test_migrate_skips_unique_index_when_duplicates_exist(tmp_path, capsys):
    path = make_healthy_db(tmp_path / "dup.db")
    con = sqlite3.connect(str(path))
    con.execute("INSERT INTO production_projects (project_name, start_date, "
                "target_quantity) VALUES ('HEALTHY', '2026-08-02', 5)")
    con.commit()
    con.close()
    assert main(["migrate", str(path)]) == 0
    assert "SKIPPED" in capsys.readouterr().out


# ---------------------------------------------------------------- check

def test_check_healthy_db(tmp_path):
    assert main(["check", str(make_healthy_db(tmp_path / "ok.db"))]) == 0


def test_check_flags_pollution_and_empty_tables(tmp_path, capsys):
    assert main(["check", str(make_polluted_db(tmp_path / "bad.db"))]) == 2
    out = capsys.readouterr().out
    assert "HTML-polluted" in out
    assert "quality_tests is empty" in out
    assert "rework_log is empty" in out


# ---------------------------------------------------------------- clean-html

def test_clean_html_dry_run_changes_nothing(tmp_path):
    path = make_polluted_db(tmp_path / "pol.db")
    before = query(path, "SELECT * FROM production_projects ORDER BY id")
    assert main(["clean-html", str(path), "--dry-run"]) == 0
    assert query(path, "SELECT * FROM production_projects ORDER BY id") == before


def test_clean_html_merges_duplicates_without_losing_history(tmp_path):
    path = make_polluted_db(tmp_path / "pol.db")
    total_before = query(path, "SELECT COALESCE(SUM(quantity_produced), 0) "
                              "AS s FROM daily_production")[0]["s"]
    assert main(["clean-html", str(path)]) == 0

    projects = query(path, "SELECT * FROM production_projects")
    assert len(projects) == 1
    assert projects[0]["project_name"] == "Heatsink"
    assert projects[0]["target_quantity"] == 1000  # max kept
    assert projects[0]["status"] == "in_progress"

    stages = query(path, "SELECT * FROM production_stages ORDER BY id")
    assert [(s["stage_name"], s["sequence_order"]) for s in stages] == [
        ("Anodised", 1), ("Extra", 2)]
    assert projects[0]["current_stage_id"] == stages[-1]["id"]

    rows = query(path, "SELECT project_id, stage_id, quantity_produced "
                       "FROM daily_production ORDER BY quantity_produced")
    assert [r["quantity_produced"] for r in rows] == [5, 7, 10]
    assert {r["project_id"] for r in rows} == {1}
    assert sum(r["quantity_produced"] for r in rows) == total_before

    mappings = query(path, "SELECT * FROM production_part_mapping "
                           "ORDER BY part_id")
    assert len(mappings) == 2
    assert all(m["project_id"] == 1 for m in mappings)
    assert [m["stage_name"] for m in mappings] == ["Anodised", "Anodised"]
    assert all(m["project_name"] == "Heatsink" for m in mappings)

    assert main(["check", str(path)]) == 2  # only empty quality/rework left
