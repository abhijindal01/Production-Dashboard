"""Tests for production-dashboard-sqlite.json.

Every panel query is executed against a seeded fixture database exactly as
Grafana would send it (after $project / ${__from} / ${__to} interpolation),
for both a single project and 'All'. This catches wrong-column, wrong-math
and wrong-time-format bugs before the JSON ever reaches Grafana.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.conftest import (REPO_ROOT, grafana_sql, iter_panel_queries,  # noqa: E402
                            to_epoch_ms)

FULL_FROM = to_epoch_ms("2026-08-01T00:00:00Z")
FULL_TO = to_epoch_ms("2026-09-09T00:00:00Z")


def run(db_path, sql):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def panel_by_title(dashboard, title):
    for panel in dashboard["panels"]:
        if panel.get("title") == title:
            return panel
    raise AssertionError(f"panel not found: {title}")


def single_value(db_path, dashboard, title, project):
    panel = panel_by_title(dashboard, title)
    sql = grafana_sql(panel["targets"][0]["rawQueryText"], project,
                      FULL_FROM, FULL_TO)
    rows = run(db_path, sql)
    assert len(rows) == 1, f"{title}: expected 1 row, got {rows}"
    assert "value" in rows[0], f"{title}: no 'value' column: {rows[0]}"
    return rows[0]["value"]


# ------------------------------------------------------------- validity

def test_dashboard_json_is_valid(dashboard):
    assert dashboard["title"] == "Production Workflow"
    assert dashboard["uid"] == "production-workflow"
    assert len(dashboard["panels"]) > 10


def test_no_hardcoded_datasource_uid(dashboard):
    text = (REPO_ROOT / "production-dashboard-sqlite.json").read_text()
    assert "bfvn5qddvug3kd" not in text  # the old machine-specific UID
    names = [v["name"] for v in dashboard["templating"]["list"]]
    assert "DS_SQLITE" in names and "project" in names
    for panel in dashboard["panels"]:
        if panel.get("type") == "row" or "targets" not in panel:
            continue
        assert panel["datasource"]["uid"] == "${DS_SQLITE}", panel["title"]


def test_layout_and_metadata(dashboard):
    seen_ids = set()
    for panel in dashboard["panels"]:
        assert panel["id"] not in seen_ids
        seen_ids.add(panel["id"])
        pos = panel["gridPos"]
        assert pos["x"] + pos["w"] <= 24, panel["title"]
        if panel.get("type") != "row":
            assert panel.get("description"), f"{panel['title']}: no description"


def test_project_variable_query(seeded_proddb, dashboard):
    var = [v for v in dashboard["templating"]["list"]
           if v["name"] == "project"][0]
    rows = run(seeded_proddb, var["query"])
    names = sorted(r["project_name"] for r in rows)
    assert names == ["Drone Frame", "Heatsink"]


# ------------------------------------------------------------- all queries run

def test_every_query_runs_for_all_and_single_project(seeded_proddb, dashboard):
    failures = []
    count = 0
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        for project in ("All", "Heatsink", "Drone Frame"):
            count += 1
            try:
                run(seeded_proddb,
                    grafana_sql(sql, project, FULL_FROM, FULL_TO))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"panel {pid} ({title}) [{ref}] "
                                f"project={project}: {exc}")
    assert count > 10
    assert failures == [], "\n".join(failures)


def test_time_columns_are_rfc3339(seeded_proddb, dashboard):
    checked = 0
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        if qtype != "time_series":
            continue
        rows = run(seeded_proddb,
                   grafana_sql(sql, "All", FULL_FROM, FULL_TO))
        for row in rows:
            datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%SZ")
            checked += 1
    assert checked > 5  # fixture must actually exercise the parsers


# ------------------------------------------------------------- correct values

def test_total_produced_net(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard,
                        "Total Produced (net)", "All") == 251
    assert single_value(seeded_proddb, dashboard,
                        "Total Produced (net)", "Heatsink") == 96
    assert single_value(seeded_proddb, dashboard,
                        "Total Produced (net)", "Drone Frame") == 155


def test_added_and_corrections_split(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard, "Units Added", "All") == 281
    assert single_value(seeded_proddb, dashboard, "Corrections", "All") == 30


def test_quality_pass_rate(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard,
                        "Quality Pass Rate", "All") == 98.2
    assert single_value(seeded_proddb, dashboard,
                        "Quality Pass Rate", "Heatsink") == 96.0
    assert single_value(seeded_proddb, dashboard,
                        "Quality Pass Rate", "Drone Frame") == 100.0


def test_total_reworked(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard, "Total Reworked", "All") == 3
    assert single_value(seeded_proddb, dashboard,
                        "Total Reworked", "Drone Frame") == 0


def test_progress_vs_target_uses_sum_not_max(seeded_proddb, dashboard):
    # SUM semantics: 251/1500=16.7 (the v1 MAX(target) bug gave 251/1000).
    assert single_value(seeded_proddb, dashboard,
                        "Progress vs Target", "All") == 16.7
    assert single_value(seeded_proddb, dashboard,
                        "Progress vs Target", "Heatsink") == 9.6
    assert single_value(seeded_proddb, dashboard,
                        "Progress vs Target", "Drone Frame") == 31.0


def test_output_by_stage_no_collisions(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Net Output by Stage")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    metrics = [r["metric"] for r in rows]
    assert len(metrics) == len(set(metrics)) == 4  # project-prefixed, unique
    total = sum(r["value"] for r in rows)
    assert total == 251


def test_quality_by_stage(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Quality by Stage")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    assert sum(r["Tested"] for r in rows) == 110
    assert sum(r["Failed"] for r in rows) == 2


def test_current_stock_by_part(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Current Stock by Part")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    assert len(rows) == 3
    by_part = {r["Part ID"]: r for r in rows}
    assert by_part[225]["Current Stock"] == 56
    assert by_part[225]["Project"] == "Heatsink"


def test_daily_output_respects_time_range(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Daily Output by Stage")
    sql = panel["targets"][0]["rawQueryText"]
    one_day = run(seeded_proddb, grafana_sql(
        sql, "All", to_epoch_ms("2026-08-26T00:00:00Z"),
        to_epoch_ms("2026-08-26T23:59:59Z")))
    assert {r["time"] for r in one_day} == {"2026-08-26T00:00:00Z"}
    # 10+15-15-10 = 0 net on that day for Anodised.
    assert sum(r["value"] for r in one_day) == 0


def test_cumulative_and_target_series(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Cumulative Output vs Target")
    assert len(panel["targets"]) == 2
    cum = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    tgt = run(seeded_proddb, grafana_sql(
        panel["targets"][1]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    assert cum[-1]["value"] == 251  # running total ends at the net total
    assert all(r["value"] == 1500 for r in tgt)  # summed target line
    assert [r["metric"] for r in cum] and tgt[0]["metric"] == "Target"


def test_rework_trend(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Rework Trend")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    assert sum(r["value"] for r in rows) == 3


def test_recent_production_log(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Recent Production Log")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    assert len(rows) == 9
    assert rows[0]["Date"] == "2026-08-26"  # newest first


def test_project_overview(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Project Overview")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    by_name = {r["Project"]: r for r in rows}
    assert by_name["Heatsink"]["Produced"] == 96
    assert by_name["Heatsink"]["Remaining"] == 904
    assert by_name["Heatsink"]["Progress %"] == 9.6
    assert by_name["Heatsink"]["Current Stage"] == "Anodised"
    assert by_name["Drone Frame"]["Progress %"] == 31.0


def test_part_mapping_sync_status(seeded_proddb, dashboard):
    panel = panel_by_title(dashboard, "Part Mapping & Sync Status")
    rows = run(seeded_proddb, grafana_sql(
        panel["targets"][0]["rawQueryText"], "All", FULL_FROM, FULL_TO))
    by_part = {r["Part ID"]: r for r in rows}
    assert by_part[225]["Status"] == "ok"
    assert by_part[339]["Status"] == "stale"  # last checked 2026-01-01


def test_empty_quality_rework_tables_yield_zero_not_null(seeded_proddb,
                                                         dashboard):
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("DELETE FROM quality_tests")
    con.execute("DELETE FROM rework_log")
    con.commit()
    con.close()
    assert single_value(seeded_proddb, dashboard,
                        "Quality Pass Rate", "All") == 0
    assert single_value(seeded_proddb, dashboard, "Total Reworked", "All") == 0


@pytest.mark.parametrize("project", ["All", "Heatsink", "Drone Frame",
                                     "Nonexistent Project"])
def test_kpi_panels_never_return_null(seeded_proddb, dashboard, project):
    for title in ("Total Produced (net)", "Units Added", "Corrections",
                  "Quality Pass Rate", "Total Reworked", "Progress vs Target"):
        assert single_value(seeded_proddb, dashboard, title, project) is not None
