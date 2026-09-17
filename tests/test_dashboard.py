"""Tests for production-dashboard-sqlite.json (the simple shop-floor board).

Three jobs:

1. Every panel query runs against a seeded fixture exactly as Grafana sends it
   (after $project / ${__from} / ${__to} interpolation) - for one project, for
   'All' and for a project that does not exist.
2. The tiles show the right numbers (no wrong-column / wrong-math /
   wrong-time-format regressions).
3. The board stays *simple*: few panels, plain-language labels, a help panel,
   no overlapping tiles, no jargon, and a friendly sentence instead of a raw
   status code or a misleading "0".
"""

import json
import re
import sqlite3
from datetime import datetime

import pytest

from tests.conftest import (REPO_ROOT, apply_canonical_schema, grafana_sql,  # noqa: E402
                            iter_panel_queries, load_dashboard, to_epoch_ms)

FULL_FROM = to_epoch_ms("2026-08-01T00:00:00Z")
FULL_TO = to_epoch_ms("2026-09-09T23:59:59Z")
PROJECTS = ("All", "Heatsink", "Drone Frame", "Nonexistent Project")

# Tiles that must either return a number or explain why there is none.
VALUE_TILES = ("Made today", "Made in the last 7 days", "Still to make",
               "Goal finished", "Quality: pieces that passed",
               "Sent back to be fixed")

TITLES = ["Made today", "Made in the last 7 days", "Still to make",
          "Goal finished", "Quality: pieces that passed",
          "Sent back to be fixed", "Is this page current?",
          "Each step, in order", "Every job, one line each",
          "How many we made each day", "How to read this board"]


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


def execute_panel(db_path, dashboard, title, project="All", ref=0,
                  from_ms=FULL_FROM, to_ms=FULL_TO):
    """Run one panel target with Grafana-style variable interpolation."""
    panel = panel_by_title(dashboard, title)
    sql = grafana_sql(panel["targets"][ref]["rawQueryText"], project,
                      from_ms, to_ms)
    return run(db_path, sql)


def single_value(db_path, dashboard, title, project="All"):
    rows = execute_panel(db_path, dashboard, title, project)
    assert len(rows) == 1, f"{title}: expected 1 row, got {rows}"
    assert "value" in rows[0], f"{title}: no 'value' column: {rows[0]}"
    return rows[0]["value"]


def insert_relative(db_path, project, stage, days_ago, qty):
    """Seed a production row dated relative to *today*, so time-dependent
    tiles can be tested without depending on the fixture's fixed August dates
    or on the clock of the machine running pytest."""
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO daily_production (project_id, stage_id, production_date,"
        " quantity_produced) VALUES ((SELECT id FROM production_projects"
        " WHERE project_name = ?), (SELECT id FROM production_stages"
        " WHERE project_id = (SELECT id FROM production_projects"
        " WHERE project_name = ?) AND stage_name = ?),"
        " date('now', 'localtime', ?), ?)",
        (project, project, stage, f"-{days_ago} days", qty))
    con.commit()
    con.close()


# ------------------------------------------------------------- simplicity
def test_dashboard_json_is_valid_and_short(dashboard):
    assert dashboard["title"] == "Production Board"
    assert dashboard["uid"] == "production-workflow"  # updates installs in place
    # "only need these": a short board, not a 15-panel wall.
    assert 6 <= len(dashboard["panels"]) <= 12, (
        f"{len(dashboard['panels'])} panels - keep the board small")
    assert not [p for p in dashboard["panels"] if p.get("type") == "row"], (
        "a flat board reads faster than one with collapsed rows")


def test_exactly_the_essential_tiles_are_present(dashboard):
    assert [p["title"] for p in dashboard["panels"]] == TITLES
    # The engineering-detail panels of v2 stay off this board.
    for gone in ("Units Added", "Corrections", "Current Stock by Part",
                 "Quality by Stage", "Cumulative Output vs Target",
                 "Rework Trend", "Recent Production Log",
                 "Part Mapping & Sync Status", "Net Output by Stage",
                 "Daily Output by Stage", "Project Overview",
                 "Total Produced (net)", "Total Reworked",
                 "Quality Pass Rate", "Progress vs Target"):
        assert gone not in TITLES


def test_tile_labels_are_plain_words(dashboard):
    """An employee who has never seen the schema must understand every tile.

    Table and column names belong in the README, not on the board. The
    dashboard-level description is the note shown while importing, so it is
    excluded on purpose.
    """
    jargon = ("delta", "net", "gross", "snapshot", "cumulative", "kpi",
              "correction", "stale", "epoch", "rfc", "schema", "query",
              "table", "raw", "null", "sql", "partdb", "uid",
              "quantity_produced", "target_quantity", "production_projects")
    for panel in dashboard["panels"]:
        text = panel.get("title", "") + " " + panel.get("description", "")
        if panel["type"] == "text":
            text += " " + panel["options"]["content"]
        low = text.lower()
        for word in jargon:
            assert not re.search(r"\b" + re.escape(word) + r"\b", low), (
                f"{panel['title']}: says {word!r} - use plain words")
        assert panel.get("description"), (
            f"{panel['title']}: every tile needs a one-line explanation")


def test_tile_titles_are_short_and_human(dashboard):
    for panel in dashboard["panels"]:
        title = panel["title"]
        assert title == title.strip()
        assert len(title) <= 34, f"{title!r}: too long to read at a glance"
        assert not re.search(r"\b[A-Z]{3,}\b", title), f"{title!r}: acronyms"
        assert not re.search(r"[_%]$|\bsum\b|\bcount\b", title.lower())


def test_the_help_panel_anchors_everything(dashboard):
    panel = panel_by_title(dashboard, "How to read this board")
    assert panel["type"] == "text"
    content = panel["options"]["content"]
    bottom = max(p["gridPos"]["y"] + p["gridPos"]["h"]
                 for p in dashboard["panels"])
    assert panel["gridPos"]["y"] + panel["gridPos"]["h"] == bottom, (
        "the guide belongs at the end, out of the way")
    for must in ("Made today", "Still to make", "PROD_PROJECT", "sync"):
        assert must in content, f"the guide never explains {must!r}"
    assert len(content) < 4000, "the guide should be a page, not a manual"


def test_layout_is_a_clean_grid_in_reading_order(dashboard):
    cells = {}
    for panel in dashboard["panels"]:
        pos = panel["gridPos"]
        assert pos["x"] >= 0 and pos["x"] + pos["w"] <= 24, panel["title"]
        assert pos["h"] >= 4, f"{panel['title']}: too short to read"
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                assert (x, y) not in cells, (
                    f"{panel['title']} overlaps {cells[(x, y)]}")
                cells[(x, y)] = panel["title"]
    first_row = [p["title"] for p in dashboard["panels"]
                 if p["gridPos"]["y"] == 0]
    assert first_row == TITLES[:4], (
        "the four headline numbers come first, left to right")


def test_tiles_are_readable_from_a_distance(dashboard):
    for panel in dashboard["panels"]:
        if panel["type"] not in ("stat", "gauge"):
            continue
        assert panel["gridPos"]["h"] >= 5, f"{panel['title']}: make it taller"
        assert panel["gridPos"]["w"] >= 6, f"{panel['title']}: make it wider"
        if panel["type"] == "stat":
            assert panel["options"]["textMode"] == "value"
            assert panel["options"]["graphMode"] == "none"


def test_a_zero_is_never_a_false_alarm(dashboard):
    """A red tile on a shop floor panics people.

    A tile that colours its low end red must explain the empty case (noValue)
    or say so in its description - and a value that is merely *unknown* must
    not be printed as 0.
    """
    alarming = ("red", "dark-red", "semi-dark-red")
    for panel in dashboard["panels"]:
        if panel["type"] not in ("stat", "gauge"):
            continue
        defaults = panel["fieldConfig"]["defaults"]
        steps = defaults["thresholds"]["steps"]
        assert steps[0]["value"] is None, panel["title"]
        if steps[0]["color"] in alarming:
            assert (defaults.get("noValue")
                    or re.search(r"(does not mean|not a failure|never|yet)",
                                 panel["description"], re.IGNORECASE)), (
                f"{panel['title']}: red low end is unexplained")


def test_unknown_does_not_look_like_zero(dashboard):
    quality = panel_by_title(dashboard, "Quality: pieces that passed")
    assert quality["fieldConfig"]["defaults"]["noValue"]
    assert "COALESCE" not in quality["targets"][0]["rawQueryText"]
    goal = panel_by_title(dashboard, "Goal finished")
    assert goal["fieldConfig"]["defaults"]["noValue"]
    # ...while a genuinely zero counter still prints a zero.
    for title in ("Made today", "Made in the last 7 days", "Still to make",
                  "Sent back to be fixed"):
        sql = panel_by_title(dashboard, title)["targets"][0]["rawQueryText"]
        assert "COALESCE(SUM" in sql, title


def test_units_are_declared(dashboard):
    for panel in dashboard["panels"]:
        if "targets" not in panel:
            continue
        unit = panel["fieldConfig"]["defaults"].get("unit")
        assert unit in ("short", "percent", "none"), (
            f"{panel['title']}: unit is {unit!r}")
    assert panel_by_title(
        dashboard, "Goal finished")["fieldConfig"]["defaults"]["max"] == 100


def test_board_refreshes_and_defaults_to_two_weeks(dashboard):
    assert dashboard["refresh"] in ("30s", "1m")
    assert dashboard["time"]["from"] in ("now-7d", "now-14d", "now-30d"), (
        "a range this short keeps the daily chart readable")
    assert dashboard["timezone"] == "browser"


# ---------------------------------------------------------------- plumbing
def test_no_hardcoded_datasource_uid(dashboard):
    text = (REPO_ROOT / "production-dashboard-sqlite.json").read_text()
    assert "bfvn5qddvug3kd" not in text  # the old machine-specific UID
    names = [v["name"] for v in dashboard["templating"]["list"]]
    assert "DS_SQLITE" in names and "project" in names
    for panel in dashboard["panels"]:
        if "targets" not in panel:
            continue
        assert panel["datasource"]["uid"] == "${DS_SQLITE}", panel["title"]
        for target in panel["targets"]:
            assert "${DS_SQLITE}" not in target["rawQueryText"]


def test_the_only_knob_a_viewer_gets_is_the_project_box(dashboard):
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert len(variables) == 2, "no extra dropdowns to fiddle with"
    project = variables["project"]
    assert project["label"] == "Which project?"
    assert project["includeAll"] is True and project["allValue"] == "All"
    assert project["multi"] is False
    assert project["current"]["text"] == "All"
    assert variables["DS_SQLITE"]["label"].lower().startswith("sqlite")


def test_project_variable_query(seeded_proddb, dashboard):
    var = [v for v in dashboard["templating"]["list"]
           if v["name"] == "project"][0]
    rows = run(seeded_proddb, var["query"])
    assert sorted(r["project_name"] for r in rows) == ["Drone Frame",
                                                        "Heatsink"]


def test_file_is_pure_json_without_trailing_junk():
    text = (REPO_ROOT / "production-dashboard-sqlite.json").read_text()
    assert text.endswith("}\n")
    json.loads(text)  # a stray '\' once made every import fail


def test_generated_json_is_not_edited_by_hand():
    """The committed file must be exactly what tools/build_dashboard.py emits."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "build_dashboard.py"),
         "--check"], capture_output=True, text=True, check=True)
    assert "up to date" in out.stdout
    assert json.loads((REPO_ROOT / "production-dashboard-sqlite.json")
                      .read_text()) == load_dashboard()


def test_every_query_runs_for_all_single_and_unknown_project(seeded_proddb,
                                                             dashboard):
    failures = []
    count = 0
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        for project in PROJECTS:
            count += 1
            try:
                run(seeded_proddb,
                    grafana_sql(sql, project, FULL_FROM, FULL_TO))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"panel {pid} ({title}) [{ref}] "
                                f"project={project}: {exc}")
    assert count >= 10
    assert failures == [], "\n".join(failures)


@pytest.mark.parametrize("project", PROJECTS)
def test_number_tiles_answer_or_say_why(seeded_proddb, dashboard, project):
    """No bare 'No data' on the board: a tile returns a number, or it has a
    sentence ready for the empty case."""
    for title in VALUE_TILES:
        panel = panel_by_title(dashboard, title)
        value = single_value(seeded_proddb, dashboard, title, project)
        if value is None:
            assert panel["fieldConfig"]["defaults"].get("noValue"), (
                f"{title}: NULL with no explanation (project={project})")
        else:
            assert isinstance(value, (int, float)), title


# ---------------------------------------------------------- headline tiles
def test_made_today_counts_only_today(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard, "Made today") == 0
    insert_relative(seeded_proddb, "Heatsink", "Anodised", 0, 25)
    insert_relative(seeded_proddb, "Heatsink", "Anodised", 0, 5)
    insert_relative(seeded_proddb, "Drone Frame", "Cutting", 1, 77)
    assert single_value(seeded_proddb, dashboard, "Made today") == 30
    assert single_value(seeded_proddb, dashboard, "Made today",
                        "Heatsink") == 30
    assert single_value(seeded_proddb, dashboard, "Made today",
                        "Drone Frame") == 0


def test_made_in_the_last_seven_days(seeded_proddb, dashboard):
    insert_relative(seeded_proddb, "Drone Frame", "Cutting", 6, 11)
    insert_relative(seeded_proddb, "Drone Frame", "Cutting", 7, 500)
    assert single_value(seeded_proddb, dashboard,
                        "Made in the last 7 days", "Drone Frame") == 11
    insert_relative(seeded_proddb, "Drone Frame", "Cutting", 0, 4)
    assert single_value(seeded_proddb, dashboard,
                        "Made in the last 7 days", "Drone Frame") == 15
    assert single_value(seeded_proddb, dashboard,
                        "Made in the last 7 days", "Heatsink") == 0


def test_still_to_make_never_goes_negative(seeded_proddb, dashboard):
    # fixture: Heatsink 96/1000, Drone Frame 155/500
    assert single_value(seeded_proddb, dashboard, "Still to make") == 1249
    assert single_value(seeded_proddb, dashboard, "Still to make",
                        "Heatsink") == 904
    assert single_value(seeded_proddb, dashboard, "Still to make",
                        "Drone Frame") == 345
    assert single_value(seeded_proddb, dashboard, "Still to make",
                        "Nonexistent Project") == 0
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("UPDATE daily_production SET quantity_produced = 5000"
                " WHERE project_id = 2")  # overshoot the 500 goal
    con.commit()
    con.close()
    assert single_value(seeded_proddb, dashboard, "Still to make",
                        "Drone Frame") == 0


def test_goal_finished_uses_sum_of_targets(seeded_proddb, dashboard):
    # SUM semantics: 251/1500=16.7 (the v1 MAX(target) bug gave 251/1000).
    assert single_value(seeded_proddb, dashboard, "Goal finished") == 16.7
    assert single_value(seeded_proddb, dashboard, "Goal finished",
                        "Heatsink") == 9.6
    assert single_value(seeded_proddb, dashboard, "Goal finished",
                        "Drone Frame") == 31.0


def test_goal_without_a_target_says_so(seeded_proddb, dashboard):
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("UPDATE production_projects SET target_quantity = 0")
    con.commit()
    con.close()
    assert single_value(seeded_proddb, dashboard, "Goal finished") is None
    assert single_value(seeded_proddb, dashboard, "Goal finished",
                        "Nonexistent Project") is None


def test_quality_and_rework_tiles(seeded_proddb, dashboard):
    assert single_value(seeded_proddb, dashboard,
                        "Quality: pieces that passed") == 98.2
    assert single_value(seeded_proddb, dashboard,
                        "Quality: pieces that passed",
                        "Heatsink") == 96.0
    assert single_value(seeded_proddb, dashboard,
                        "Quality: pieces that passed",
                        "Drone Frame") == 100.0
    assert single_value(seeded_proddb, dashboard,
                        "Sent back to be fixed") == 3
    assert single_value(seeded_proddb, dashboard, "Sent back to be fixed",
                        "Drone Frame") == 0


def test_quality_and_rework_with_an_empty_log(seeded_proddb, dashboard):
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("DELETE FROM quality_tests")
    con.execute("DELETE FROM rework_log")
    con.commit()
    con.close()
    # quality: unknown, so the tile prints 'no checks logged yet'
    assert single_value(seeded_proddb, dashboard,
                        "Quality: pieces that passed") is None
    # rework: a real zero
    assert single_value(seeded_proddb, dashboard,
                        "Sent back to be fixed") == 0


def test_quality_tile_for_a_project_that_was_never_tested(seeded_proddb,
                                                           dashboard):
    assert single_value(seeded_proddb, dashboard,
                        "Quality: pieces that passed",
                        "Nonexistent Project") is None


# ------------------------------------------------------------- sync status
def test_is_this_page_current_translates_freshness(seeded_proddb, dashboard):
    # fixture: part 225 (Heatsink) checked just now, part 339 in 2026-01-01.
    assert single_value(seeded_proddb, dashboard, "Is this page current?",
                        "Heatsink") == "OK"
    assert single_value(seeded_proddb, dashboard, "Is this page current?",
                        "All") == "OK"
    assert single_value(seeded_proddb, dashboard, "Is this page current?",
                        "Drone Frame") == "NONE"
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("UPDATE part_stock_snapshot SET last_checked ="
                " datetime('now', '-3 days') WHERE part_id = 339")
    con.commit()
    con.close()
    assert single_value(seeded_proddb, dashboard, "Is this page current?",
                        "Drone Frame") == "CHECK"


def test_is_this_page_current_shows_words_not_codes(dashboard):
    panel = panel_by_title(dashboard, "Is this page current?")
    mappings = panel["fieldConfig"]["defaults"]["mappings"]
    assert mappings and mappings[0]["type"] == "value"
    options = mappings[0]["options"]
    assert set(options) == {"OK", "CHECK", "NONE"}
    assert all(o["text"] and o["color"] for o in options.values())
    assert panel["options"]["colorMode"] == "background"
    assert panel["fieldConfig"]["defaults"]["thresholds"]["steps"][0][
        "color"] == "green", "must not flash red on a text value"


def test_is_this_page_current_survives_a_fresh_database(tmp_path, dashboard):
    db = tmp_path / "empty.db"
    con = sqlite3.connect(str(db))
    apply_canonical_schema(con)
    con.commit()
    con.close()
    assert single_value(db, dashboard, "Is this page current?") == "NONE"


# ------------------------------------------------------------- steps + jobs
def test_each_step_is_listed_in_order_with_plain_labels(seeded_proddb,
                                                         dashboard):
    rows = execute_panel(seeded_proddb, dashboard, "Each step, in order",
                         "Heatsink")
    assert [r["metric"] for r in rows] == ["1. After sand blasting",
                                           "2. Anodised"]
    assert [r["value"] for r in rows] == [40, 56]

    rows = execute_panel(seeded_proddb, dashboard, "Each step, in order",
                         "All")
    metrics = [r["metric"] for r in rows]
    assert len(metrics) == len(set(metrics)) == 4  # prefixed: no collisions
    assert all(re.match(r"^(Drone Frame|Heatsink) - \d+\. ", m)
               for m in metrics), metrics
    assert sum(r["value"] for r in rows) == 251


def test_every_job_table_reads_like_a_sentence(seeded_proddb, dashboard):
    rows = execute_panel(seeded_proddb, dashboard, "Every job, one line each")
    assert list(rows[0]) == ["Project", "Status", "Current step", "Goal",
                             "Made so far", "Still to make", "% done",
                             "Last counted"]
    by_name = {r["Project"]: r for r in rows}
    heatsink = by_name["Heatsink"]
    assert heatsink["Status"] == "Running"        # not 'in_progress'
    assert heatsink["Current step"] == "Anodised"
    assert heatsink["Goal"] == 1000
    assert heatsink["Made so far"] == 96
    assert heatsink["Still to make"] == 904
    assert heatsink["% done"] == 9.6
    assert heatsink["Last counted"] == "2026-08-26"
    assert by_name["Drone Frame"]["% done"] == 31.0


def test_every_job_table_translates_codes(seeded_proddb, dashboard):
    con = sqlite3.connect(str(seeded_proddb))
    con.execute("UPDATE production_projects SET status = 'completed'")
    con.execute("UPDATE production_projects SET current_stage_id = NULL"
                " WHERE id = 1")
    con.commit()
    con.close()
    rows = execute_panel(seeded_proddb, dashboard, "Every job, one line each")
    by_name = {r["Project"]: r for r in rows}
    assert by_name["Drone Frame"]["Status"] == "Finished"
    # a job whose step was never decided must not show an empty cell
    assert by_name["Heatsink"]["Current step"] == "not decided yet"
    assert all(None not in r.values() for r in rows)


def test_every_job_table_only_shows_the_selected_project(seeded_proddb,
                                                         dashboard):
    rows = execute_panel(seeded_proddb, dashboard, "Every job, one line each",
                         "Heatsink")
    assert [r["Project"] for r in rows] == ["Heatsink"]
    assert execute_panel(seeded_proddb, dashboard, "Every job, one line each",
                         "Nonexistent Project") == []


# ------------------------------------------------------------------ chart
def test_daily_chart_respects_the_time_picker(seeded_proddb, dashboard):
    one_day = execute_panel(seeded_proddb, dashboard,
                            "How many we made each day", "All",
                            from_ms=to_epoch_ms("2026-08-26T00:00:00Z"),
                            to_ms=to_epoch_ms("2026-08-26T23:59:59Z"))
    assert {r["time"] for r in one_day} == {"2026-08-26T00:00:00Z"}
    assert sum(r["value"] for r in one_day) == 0  # 10 + 15 - 15 - 10

    whole = execute_panel(seeded_proddb, dashboard,
                          "How many we made each day", "All")
    assert len(whole) == 6  # one combined bar per day, not one per stage
    assert {r["metric"] for r in whole} == {"Made"}
    assert [r["time"] for r in whole] == sorted(r["time"] for r in whole)


def test_time_columns_are_rfc3339(seeded_proddb, dashboard):
    checked = 0
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        if qtype != "time_series":
            continue
        for row in run(seeded_proddb,
                       grafana_sql(sql, "All", FULL_FROM, FULL_TO)):
            datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%SZ")
            checked += 1
    assert checked > 5  # the fixture must actually exercise the parser


def test_daily_chart_is_one_easy_series(dashboard):
    panel = panel_by_title(dashboard, "How many we made each day")
    assert len(panel["targets"]) == 1, "one line, not a legend of eight"
    custom = panel["fieldConfig"]["defaults"]["custom"]
    assert custom["drawStyle"] == "bars"
    assert custom["stacking"]["mode"] == "none"
    assert panel["fieldConfig"]["defaults"]["color"]["mode"] == "fixed"
    assert not panel["options"]["legend"]["showLegend"], (
        "one series does not need a legend")


# ------------------------------------------------------------ resilience
def test_an_empty_database_still_renders_the_whole_board(tmp_path,
                                                         dashboard):
    """A fresh install must not show query errors or 'No data' tiles."""
    db = tmp_path / "fresh.db"
    con = sqlite3.connect(str(db))
    apply_canonical_schema(con)
    con.execute("DELETE FROM production_projects")
    con.execute("DELETE FROM production_stages")
    con.execute("DELETE FROM daily_production")
    con.commit()
    con.close()
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        rows = run(db, grafana_sql(sql, "All", FULL_FROM, FULL_TO))
        if qtype == "table" and title in VALUE_TILES:
            assert len(rows) == 1, title
        else:
            assert isinstance(rows, list), title
    assert single_value(db, dashboard, "Made today") == 0
    assert single_value(db, dashboard, "Still to make") == 0
    assert single_value(db, dashboard, "Goal finished") is None
    assert single_value(db, dashboard, "Is this page current?") == "NONE"


def test_live_committed_database_still_answers_every_query(tmp_path,
                                                            dashboard):
    """Run the board against production.db as it ships in the repo."""
    src = REPO_ROOT / "production.db"
    if not src.exists():
        pytest.skip("no production.db in this checkout")
    db = tmp_path / "production.db"
    db.write_bytes(src.read_bytes())
    failures = []
    for pid, title, qtype, ref, sql in iter_panel_queries(dashboard):
        for project in ("All", "Heatsink"):
            try:
                run(db, grafana_sql(sql, project, FULL_FROM, FULL_TO))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"panel {pid} ({title}) {project}: {exc}")
    assert failures == [], "\n".join(failures)


def test_panel_ids_are_unique_and_ordered(dashboard):
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids), "tile ids should read top-to-bottom"
    for panel in dashboard["panels"]:
        if "targets" in panel:
            for target in panel["targets"]:
                assert target["queryType"] in ("table", "time_series")
                assert target["rawQueryText"].strip().upper().startswith(
                    "SELECT"), panel["title"]
