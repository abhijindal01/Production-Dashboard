"""End-to-end tests for partdb_sync.main() against fixture databases."""

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import partdb_sync  # noqa: E402
from partdb_sync import main  # noqa: E402
from tests.conftest import make_partdb, set_part_lots  # noqa: E402

TODAY = date.today().isoformat()


def run_sync(partdb_path, prod_path, *extra):
    lock = str(prod_path) + ".test-lock"
    return main([str(partdb_path), str(prod_path),
                 "--lock-file", lock, *extra])


def query(prod_path, sql, params=()):
    con = sqlite3.connect(str(prod_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------- core deltas

def test_baseline_addition_subtraction_no_change(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Widget PROD_STAGE=Assembly",
         "lots": [10]},
    ])
    prod = tmp_path / "production.db"

    assert run_sync(partdb, prod) == 0  # baseline: snapshot only
    assert query(prod, "SELECT * FROM daily_production") == []
    assert query(prod, "SELECT * FROM part_stock_snapshot") == [
        {"part_id": 1, "last_known_quantity": 10.0,
         "last_checked": query(
             prod, "SELECT last_checked FROM part_stock_snapshot")[0][
             "last_checked"]}
    ]
    # Regression test for the v1 crash: new projects must be created WITH
    # target_quantity/status (NOT NULL constraint killed ~89% of v1 runs).
    projects = query(prod, "SELECT * FROM production_projects")
    assert len(projects) == 1
    assert projects[0]["project_name"] == "Widget"
    assert projects[0]["target_quantity"] == 0
    assert projects[0]["status"] == "in_progress"

    set_part_lots(partdb, 1, [25])
    assert run_sync(partdb, prod) == 0  # addition
    rows = query(prod, "SELECT quantity_produced, production_date "
                       "FROM daily_production")
    assert rows == [{"quantity_produced": 15, "production_date": TODAY}]

    set_part_lots(partdb, 1, [20])
    assert run_sync(partdb, prod) == 0  # subtraction
    rows = query(prod, "SELECT quantity_produced FROM daily_production "
                       "ORDER BY id")
    assert [r["quantity_produced"] for r in rows] == [15, -5]

    assert run_sync(partdb, prod) == 0  # no change -> no new rows
    assert len(query(prod, "SELECT * FROM daily_production")) == 2
    assert query(prod, "SELECT last_known_quantity FROM part_stock_snapshot "
                       "WHERE part_id = 1")[0]["last_known_quantity"] == 20.0


def test_default_target_flag(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Widget PROD_STAGE=Assembly",
         "lots": [5]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod, "--default-target", "500") == 0
    assert query(prod, "SELECT target_quantity FROM production_projects")[0][
        "target_quantity"] == 500


def test_html_comment_end_to_end(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 7,
         "comment": '<p>PROD_PROJECT=Drone Soccer Balls with RC</p>'
                    '<p>PROD_STAGE=Ready to Delivery</p>',
         "lots": [3]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0
    assert query(prod, "SELECT project_name FROM production_projects")[0][
        "project_name"] == "Drone Soccer Balls with RC"
    assert query(prod, "SELECT stage_name FROM production_stages")[0][
        "stage_name"] == "Ready to Delivery"


def test_multi_part_project_current_stage_is_most_advanced(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=H PROD_STAGE=First", "lots": [1]},
        {"id": 2, "comment": "PROD_PROJECT=H PROD_STAGE=Second", "lots": [2]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0
    stages = {r["stage_name"]: r["id"]
              for r in query(prod, "SELECT * FROM production_stages")}
    current = query(prod, "SELECT current_stage_id FROM production_projects")[0][
        "current_stage_id"]
    assert current == stages["Second"]  # most advanced, not last processed
    assert len(query(prod, "SELECT * FROM production_part_mapping")) == 2
    # Second run must not flap the current stage either.
    assert run_sync(partdb, prod) == 0
    assert query(prod, "SELECT current_stage_id FROM production_projects")[0][
        "current_stage_id"] == stages["Second"]


def test_missing_stage_skips_and_retries_delta(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Lonely", "lots": [5]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0  # baseline (no stage anywhere yet)
    set_part_lots(partdb, 1, [8])
    assert run_sync(partdb, prod) == 0  # skipped: nowhere to book +3
    assert query(prod, "SELECT * FROM daily_production") == []
    # Snapshot untouched so the delta is retried later.
    assert query(prod, "SELECT last_known_quantity FROM part_stock_snapshot "
                       "WHERE part_id = 1")[0]["last_known_quantity"] == 5.0
    # A stage appears (second part of the same project carries one)...
    con = sqlite3.connect(str(partdb))
    con.execute("INSERT INTO parts (id, comment) VALUES (2, ?)",
                ("PROD_PROJECT=Lonely PROD_STAGE=Main",))
    con.execute("INSERT INTO part_lots (id_part, amount) VALUES (2, 0)")
    con.commit()
    con.close()
    assert run_sync(partdb, prod) == 0
    qtys = sorted(r["quantity_produced"]
                  for r in query(prod, "SELECT quantity_produced "
                                       "FROM daily_production"))
    assert qtys == [3]  # the earlier +3 delta finally booked


def test_case_insensitive_reuse_no_duplicates(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Heatsink PROD_STAGE=Anodised",
         "lots": [1]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0
    con = sqlite3.connect(str(partdb))
    con.execute("UPDATE parts SET comment = ? WHERE id = 1",
                ("PROD_PROJECT=HEATSINK PROD_STAGE=anodised",))
    con.commit()
    con.close()
    assert run_sync(partdb, prod) == 0
    assert len(query(prod, "SELECT * FROM production_projects")) == 1
    assert len(query(prod, "SELECT * FROM production_stages")) == 1


def test_fractional_stock(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Cable PROD_STAGE=Cut",
         "lots": [10.5]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0
    set_part_lots(partdb, 1, [10.7])  # sub-unit drift: absorbed, no row
    assert run_sync(partdb, prod) == 0
    assert query(prod, "SELECT * FROM daily_production") == []
    assert query(prod, "SELECT last_known_quantity FROM part_stock_snapshot "
                       "WHERE part_id = 1")[0]["last_known_quantity"] == 10.7
    set_part_lots(partdb, 1, [12.0])  # +1.3 -> +1 booked
    assert run_sync(partdb, prod) == 0
    assert [r["quantity_produced"]
            for r in query(prod, "SELECT quantity_produced "
                                 "FROM daily_production")] == [1]


# ------------------------------------------------------------- robustness

def test_per_part_error_isolation(tmp_path, monkeypatch):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=Good PROD_STAGE=OK", "lots": [5]},
        {"id": 2, "comment": "PROD_PROJECT=Bad PROD_STAGE=BOOM", "lots": [5]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0  # baselines

    real_stage_fn = partdb_sync.get_or_create_stage

    def flaky(cur, project_id, stage_name):
        if stage_name == "BOOM":
            raise RuntimeError("simulated stage failure")
        return real_stage_fn(cur, project_id, stage_name)

    monkeypatch.setattr(partdb_sync, "get_or_create_stage", flaky)
    set_part_lots(partdb, 1, [9])
    set_part_lots(partdb, 2, [9])
    assert run_sync(partdb, prod) == 2  # completed WITH errors
    # The good part's delta survived (v1 would have rolled it back).
    rows = query(prod, "SELECT project_id, quantity_produced "
                       "FROM daily_production")
    assert len(rows) == 1 and rows[0]["quantity_produced"] == 4
    good_id = query(prod, "SELECT id FROM production_projects "
                          "WHERE project_name = 'Good'")[0]["id"]
    assert rows[0]["project_id"] == good_id


def test_dry_run_changes_nothing(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=W PROD_STAGE=S", "lots": [5]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod, "--dry-run") == 0
    assert query(prod, "SELECT * FROM production_projects") == []
    assert query(prod, "SELECT * FROM part_stock_snapshot") == []


def test_lock_blocks_concurrent_run(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=W PROD_STAGE=S", "lots": [5]},
    ])
    prod = tmp_path / "production.db"
    lock_path = tmp_path / "sync.lock"
    held = partdb_sync.SyncLock(str(lock_path))
    assert held.acquire()
    try:
        rc = main([str(partdb), str(prod),
                   "--lock-file", str(lock_path)])
        assert rc == 3
    finally:
        held.release()


def test_partdb_opened_read_only(tmp_path):
    partdb_path = tmp_path / "partdb.db"
    make_partdb(partdb_path, [
        {"id": 1, "comment": "PROD_PROJECT=W PROD_STAGE=S", "lots": [5]},
    ])
    before = partdb_path.read_bytes()
    prod = tmp_path / "production.db"
    assert run_sync(partdb_path, prod) == 0
    assert partdb_path.read_bytes() == before  # byte-identical


def test_missing_partdb_is_fatal(tmp_path):
    rc = main([str(tmp_path / "nope.db"), str(tmp_path / "production.db"),
               "--lock-file", str(tmp_path / "x.lock")])
    assert rc == 1


def test_idempotent_rerun(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=W PROD_STAGE=S", "lots": [7]},
    ])
    prod = tmp_path / "production.db"
    assert run_sync(partdb, prod) == 0
    assert run_sync(partdb, prod) == 0
    assert query(prod, "SELECT * FROM daily_production") == []
    assert query(prod, "SELECT last_known_quantity FROM part_stock_snapshot "
                       "WHERE part_id = 1")[0]["last_known_quantity"] == 7.0


def test_log_file_written(tmp_path):
    partdb = make_partdb(tmp_path / "partdb.db", [
        {"id": 1, "comment": "PROD_PROJECT=W PROD_STAGE=S", "lots": [7]},
    ])
    prod = tmp_path / "production.db"
    log = tmp_path / "sync.log"
    assert run_sync(partdb, prod, "--log-file", str(log)) == 0
    content = log.read_text()
    assert "SYNC COMPLETE" in content
    assert "[BASELINE]" in content
