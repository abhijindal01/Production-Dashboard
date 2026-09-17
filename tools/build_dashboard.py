#!/usr/bin/env python3
"""Build `production-dashboard-sqlite.json` - the simple shop-floor board.

The dashboard is generated (instead of hand-edited) for two reasons:

* every SQL snippet is assembled from shared helpers, so the
  "one project vs. All" filter can never drift between tiles;
* `tests/test_dashboard.py` regenerates the file and compares it with the
  committed JSON, so a hand edit that breaks a query is caught immediately.

Reads nothing but the standard library. Run:

    python3 tools/build_dashboard.py            # write the JSON into the repo
    python3 tools/build_dashboard.py --stdout    # print it (used by the tests)
    python3 tools/build_dashboard.py --check     # fail if the file is stale
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_PATH = os.path.join(REPO_ROOT, "production-dashboard-sqlite.json")

DS = {"type": "frser-sqlite-datasource", "uid": "${DS_SQLITE}"}
PLUGIN_VERSION = "11.0.0"


def project_filter(alias):
    """Grafana's `$project` variable: a single project, or everything."""
    return (f"({alias}.project_name = '$project' "
            f"OR '$project' = 'All')")


# --------------------------------------------------------------------- SQL
# Net = additions minus stock corrections; the board never says "net", it
# just shows the number people act on.
Q_MADE = (
    "SELECT COALESCE(SUM(dp.quantity_produced), 0) AS value "
    "FROM daily_production dp "
    "JOIN production_projects pp ON pp.id = dp.project_id "
    "WHERE " + project_filter("pp")
)
Q_MADE_TODAY = Q_MADE + " AND dp.production_date = date('now', 'localtime')"
Q_MADE_WEEK = (Q_MADE +
               " AND dp.production_date >= date('now', 'localtime', '-6 days')")

Q_LEFT = (
    "SELECT MAX(0, "
    "(SELECT COALESCE(SUM(pp1.target_quantity), 0) FROM production_projects "
    "pp1 WHERE " + project_filter("pp1") + ") - "
    "(SELECT COALESCE(SUM(dp1.quantity_produced), 0) FROM daily_production "
    "dp1 JOIN production_projects pp2 ON pp2.id = dp1.project_id WHERE " +
    project_filter("pp2") + ")) AS value"
)

# No goal (target) recorded => NULL -> rendered as "goal not set yet".
Q_GOAL_PCT = (
    "SELECT ROUND("
    "(SELECT COALESCE(SUM(dp.quantity_produced), 0) FROM daily_production dp "
    "JOIN production_projects p2 ON p2.id = dp.project_id WHERE " +
    project_filter("p2") + ") * 100.0 / NULLIF("
    "(SELECT COALESCE(SUM(pp2.target_quantity), 0) FROM production_projects "
    "pp2 WHERE " + project_filter("pp2") + "), 0), 1) AS value"
)

# No checks recorded yet => NULL, which the tile renders as `noValue`
# ("no checks logged yet") instead of an alarming red 0%.
Q_PASS_RATE = (
    "SELECT ROUND(SUM(qt.quantity_passed) * 100.0 / "
    "NULLIF(SUM(qt.quantity_tested), 0), 1) AS value "
    "FROM production_projects pp "
    "JOIN quality_tests qt ON qt.project_id = pp.id "
    "WHERE " + project_filter("pp")
)

Q_REWORK = (
    "SELECT COALESCE(SUM(rl.quantity_reworked), 0) AS value "
    "FROM production_projects pp "
    "LEFT JOIN rework_log rl ON rl.project_id = pp.id "
    "WHERE " + project_filter("pp")
)

# Codes are translated to words by value mappings in the panel itself.
Q_LIVE = (
    "SELECT CASE "
    "WHEN MAX(s.last_checked) IS NULL THEN 'NONE' "
    "WHEN datetime(MAX(s.last_checked)) >= datetime('now', '-1 day') "
    "THEN 'OK' "
    "WHEN datetime(MAX(s.last_checked)) >= datetime('now', '-7 days') "
    "THEN 'CHECK' "
    "ELSE 'NONE' END AS value "
    "FROM part_stock_snapshot s "
    "JOIN production_part_mapping m ON m.part_id = s.part_id "
    "WHERE " + project_filter("m")
)

Q_STEPS = (
    "SELECT CASE WHEN '$project' = 'All' "
    "THEN pp.project_name || ' - ' || ps.sequence_order || '. ' || "
    "ps.stage_name "
    "ELSE ps.sequence_order || '. ' || ps.stage_name END AS metric, "
    "COALESCE(SUM(dp.quantity_produced), 0) AS value "
    "FROM production_stages ps "
    "JOIN production_projects pp ON pp.id = ps.project_id "
    "LEFT JOIN daily_production dp ON dp.stage_id = ps.id "
    "AND dp.project_id = pp.id "
    "WHERE " + project_filter("pp") + " "
    "GROUP BY ps.id, pp.project_name, ps.stage_name, ps.sequence_order "
    "ORDER BY pp.project_name, ps.sequence_order"
)

_MADE_FOR = ("COALESCE((SELECT SUM(dp.quantity_produced) FROM daily_production"
             " dp WHERE dp.project_id = pp.id), 0)")

Q_PROJECTS = (
    "SELECT pp.project_name AS Project, "
    "CASE pp.status WHEN 'in_progress' THEN 'Running' WHEN 'planned' "
    "THEN 'Not started' WHEN 'completed' THEN 'Finished' WHEN 'done' "
    "THEN 'Finished' ELSE COALESCE(pp.status, '-') END AS Status, "
    "COALESCE((SELECT ps.stage_name FROM production_stages ps "
    "WHERE ps.id = pp.current_stage_id), 'not decided yet') "
    "AS \"Current step\", "
    "pp.target_quantity AS Goal, "
    + _MADE_FOR + " AS \"Made so far\", "
    "MAX(0, pp.target_quantity - " + _MADE_FOR + ") AS \"Still to make\", "
    "COALESCE(ROUND(" + _MADE_FOR + " * 100.0 / NULLIF(pp.target_quantity, "
    "0), 1), 0) AS \"% done\", "
    "COALESCE((SELECT MAX(dp.production_date) FROM daily_production dp "
    "WHERE dp.project_id = pp.id), 'nothing yet') AS \"Last counted\" "
    "FROM production_projects pp "
    "WHERE " + project_filter("pp") + " "
    "ORDER BY pp.start_date DESC, pp.project_name"
)

Q_DAILY = (
    "SELECT dp.production_date || 'T00:00:00Z' AS time, 'Made' AS metric, "
    "SUM(dp.quantity_produced) AS value "
    "FROM daily_production dp "
    "JOIN production_projects pp ON pp.id = dp.project_id "
    "WHERE " + project_filter("pp") + " "
    "AND date(dp.production_date) BETWEEN date(${__from}/1000, 'unixepoch') "
    "AND date(${__to}/1000, 'unixepoch') "
    "GROUP BY dp.production_date ORDER BY dp.production_date"
)

Q_PROJECT_LIST = ("SELECT project_name FROM production_projects "
                  "ORDER BY start_date DESC, project_name")

HELP = """### Reading this board

| Tile | In plain words |
|---|---|
| **Made today** | Pieces booked since this morning. Booked = the stock went up in Part-DB. |
| **Made in the last 7 days** | The same, for the whole week (today included). |
| **Still to make** | How many pieces are left to reach the goal. 0 = goal reached. |
| **Goal finished** | How far we are. Red under 50%, yellow 50-90%, green 90% and up. |
| **Quality: pieces that passed** | Out of every 100 pieces that were checked, how many were OK. |
| **Sent back to be fixed** | Pieces that had to be redone. 0 is what you want. |
| **Is this page current?** | Green = live. Red = the sync stopped, so the numbers above are old. |
| **Each step, in order** | How many pieces each step of the job has produced. |
| **Every job, one line each** | The same story per project. |

### When a number looks wrong

1. **A tile shows 0** - most of the time nothing has been booked today yet, which is
   normal early in the morning or on a quiet shift.
2. **"Sync stopped - numbers are old"** - tell whoever runs the sync on the server
   (`partdb_sync.py`, normally every 5 minutes from cron). Until it runs, this page
   does not move.
3. **A part never shows up in the counts** - its comment in Part-DB must say
   `PROD_PROJECT=<job name> PROD_STAGE=<step name>`. No tag, no numbers.
4. **Goal finished says 0% although work was done** - the goal quantity was never
   entered for this project. Ask your supervisor to set it.
5. **Quality or rework tiles are empty** - nobody has entered a check yet. That is
   expected until quality logging starts.
6. **A bar in the chart goes below the line** - somebody reduced the stock in Part-DB
   for that day (a mis-count, a return to store). Nobody un-made finished pieces.

Use **Which project?** at the top right to look at one job only. The date picker
defaults to the last 30 days and only changes the "How many we made each day" chart - the numbers above are always the full
story.
"""


# ------------------------------------------------------------------- panels
def thresholds(steps):
    return {"mode": "absolute",
            "steps": [{"color": color, "value": value}
                      for value, color in steps]}


def stat(panel_id, title, desc, sql, grid, unit="short",
         steps=((None, "green"),), color_mode="value", mappings=None,
         no_value=None):
    """A big number tile: text is the value, colour comes from thresholds."""
    defaults = {"mappings": mappings or [],
                "thresholds": thresholds(steps), "unit": unit}
    if no_value:
        # Grafana prints this instead of "No data" when the query returns NULL.
        defaults["noValue"] = no_value
    panel = {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": grid,
        "id": panel_id,
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                              "values": False},
            "showPercentChange": False,
            "textMode": "value",
            "wideLayout": True,
        },
        "pluginVersion": PLUGIN_VERSION,
        "targets": [{"queryType": "table", "rawQueryText": sql,
                     "refId": "A"}],
        "title": title,
        "type": "stat",
    }
    return panel


def build_panels():
    panels = [
        # --- the four numbers everybody asks about -------------------------
        stat(1, "Made today",
             "How many pieces were counted today. A piece is counted when its "
             "stock goes up in Part-DB, so 0 usually just means nothing has "
             "been booked yet today.",
             Q_MADE_TODAY, {"h": 5, "w": 6, "x": 0, "y": 0},
             steps=((None, "blue"), (1, "green"))),
        stat(2, "Made in the last 7 days",
             "Pieces counted in the last week, today included. Handy when "
             "today is still empty.",
             Q_MADE_WEEK, {"h": 5, "w": 6, "x": 6, "y": 0},
             steps=((None, "blue"), (1, "green"))),
        stat(3, "Still to make",
             "How many pieces are left before the goal is reached. 0 means the "
             "goal is done.",
             Q_LEFT, {"h": 5, "w": 6, "x": 12, "y": 0},
             steps=((None, "blue"),)),
        {
            "datasource": DS,
            "description": "How much of the goal is finished. The goal is the "
                           "target quantity set for the job; if it says 'goal "
                           "not set yet', nobody entered a goal - that does "
                           "not mean production stopped.",
            "fieldConfig": {
                "defaults": {"mappings": [], "max": 100, "min": 0,
                             "noValue": "goal not set yet",
                             "thresholds": thresholds([(None, "red"),
                                                       (50, "#EAB839"),
                                                       (90, "green")]),
                             "unit": "percent"},
                "overrides": [],
            },
            "gridPos": {"h": 5, "w": 6, "x": 18, "y": 0},
            "id": 4,
            "options": {
                "minVizHeight": 75, "minVizWidth": 75,
                "orientation": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                  "values": False},
                "showThresholdLabels": False, "showThresholdMarkers": True,
                "sizing": "auto",
            },
            "pluginVersion": PLUGIN_VERSION,
            "targets": [{"queryType": "table", "rawQueryText": Q_GOAL_PCT,
                         "refId": "A"}],
            "title": "Goal finished",
            "type": "gauge",
        },
        # --- quality + "can I trust this page" ----------------------------
        stat(5, "Quality: pieces that passed",
             "Out of every 100 pieces that were checked, how many passed. If "
             "it says 'no checks logged yet', nobody has entered a check - "
             "that is not a failure.",
             Q_PASS_RATE, {"h": 5, "w": 8, "x": 0, "y": 5}, unit="percent",
             steps=((None, "red"), (90, "#EAB839"), (98, "green")),
             no_value="no checks logged yet"),
        stat(6, "Sent back to be fixed",
             "Pieces that had to be redone. 0 is what you want to see; a "
             "growing number means something at the last step is off.",
             Q_REWORK, {"h": 5, "w": 8, "x": 8, "y": 5},
             steps=((None, "green"), (1, "#EAB839"), (100, "red"))),
        stat(7, "Is this page current?",
             "This board only moves when the Part-DB sync runs (a small job on "
             "the server, every 5 minutes). Red means that job stopped - the "
             "numbers above are then old, not zero. Tell whoever runs the "
             "sync.",
             Q_LIVE, {"h": 5, "w": 8, "x": 16, "y": 5}, unit="none",
             color_mode="background",
             mappings=[{"type": "value", "options": {
                 "OK": {"color": "green", "index": 0,
                        "text": "Yes, up to date"},
                 "CHECK": {"color": "orange", "index": 1,
                           "text": "Last check over a day ago"},
                 "NONE": {"color": "red", "index": 2,
                          "text": "Sync stopped - numbers are old"},
             }}]),
        # --- where the work is --------------------------------------------
        {
            "datasource": DS,
            "description": "How many pieces each step of the job has produced, "
                           "in the order the steps happen. A step with no bar "
                           "has not been reached yet. With several projects "
                           "shown, the bar names say which job they belong to.",
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "continuous-BlPu"},
                    "mappings": [], "min": 0,
                    "thresholds": thresholds([(None, "green")]),
                    "unit": "short"},
                "overrides": [],
            },
            "gridPos": {"h": 9, "w": 10, "x": 0, "y": 10},
            "id": 8,
            "options": {
                "displayMode": "gradient", "maxVizHeight": 300,
                "minVizHeight": 24, "minVizWidth": 8,
                "namePlacement": "auto", "orientation": "horizontal",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                  "values": False},
                "showUnfilled": True, "sizing": "auto", "valueMode": "color",
            },
            "pluginVersion": PLUGIN_VERSION,
            "targets": [{"queryType": "table", "rawQueryText": Q_STEPS,
                         "refId": "A"}],
            "title": "Each step, in order",
            "type": "bargauge",
        },
        {
            "datasource": DS,
            "description": "One line per job: what we promised, what is done, "
                           "what is left, which step we are on and when the "
                           "last piece was counted.",
            "fieldConfig": {
                "defaults": {
                    "custom": {"align": "auto",
                               "cellOptions": {"type": "auto"},
                               "inspect": False},
                    "mappings": [],
                    "thresholds": thresholds([(None, "green")]),
                    "unit": "short"},
                "overrides": [
                    {"matcher": {"id": "byName", "options": "% done"},
                     "properties": [
                         {"id": "unit", "value": "percent"},
                         {"id": "custom.cellOptions",
                          "value": {"mode": "gradient",
                                    "type": "color-background"}},
                         {"id": "custom.width", "value": 110},
                         {"id": "thresholds",
                          "value": thresholds([(None, "red"),
                                               (50, "#EAB839"),
                                               (90, "green")])}]},
                    {"matcher": {"id": "byName", "options": "Still to make"},
                     "properties": [
                         {"id": "custom.cellOptions",
                          "value": {"type": "color-text"}},
                         {"id": "thresholds",
                          "value": thresholds([(None, "green"),
                                               (1, "#EAB839")])}]},
                    {"matcher": {"id": "byName", "options": "Status"},
                     "properties": [
                         {"id": "custom.cellOptions",
                          "value": {"type": "color-background"}}]},
                ],
            },
            "gridPos": {"h": 9, "w": 14, "x": 10, "y": 10},
            "id": 9,
            "options": {
                "cellHeight": "sm",
                "footer": {"countRows": False, "fields": "",
                           "reducer": ["sum"], "show": False},
                "showHeader": True, "sortBy": [],
            },
            "pluginVersion": PLUGIN_VERSION,
            "targets": [{"queryType": "table", "rawQueryText": Q_PROJECTS,
                         "refId": "A"}],
            "title": "Every job, one line each",
            "type": "table",
        },
        # --- the one chart -------------------------------------------------
        {
            "datasource": DS,
            "description": "Pieces counted per day, all steps added together, "
                           "for the time range chosen at the top right (default: "
                           "the last 30 days). A bar below the line means stock "
                           "was reduced in Part-DB that day - nobody un-made a "
                           "piece. A piece that passes two steps on the same day "
                           "is counted twice here; 'Each step, in order' shows "
                           "the split. No bars at all simply means nothing was "
                           "booked in the chosen range.",
            "fieldConfig": {
                "defaults": {
                    "color": {"fixedColor": "green", "mode": "fixed"},
                    "custom": {
                        "axisBorderShow": False, "axisCenteredZero": True,
                        "axisColorMode": "text", "axisLabel": "pieces",
                        "axisPlacement": "auto", "barAlignment": 0,
                        "barWidthFactor": 0.7, "drawStyle": "bars",
                        "fillOpacity": 70, "gradientMode": "none",
                        "hideFrom": {"legend": False, "tooltip": False,
                                     "viz": False},
                        "insertNulls": False, "lineInterpolation": "linear",
                        "lineWidth": 1, "pointSize": 5,
                        "scaleDistribution": {"type": "linear"},
                        "showPoints": "never", "spanNulls": False,
                        "stacking": {"group": "A", "mode": "none"},
                        "thresholdsStyle": {"mode": "off"},
                    },
                    "mappings": [],
                    "thresholds": thresholds([(None, "green")]),
                    "unit": "short"},
                "overrides": [],
            },
            "gridPos": {"h": 8, "w": 24, "x": 0, "y": 19},
            "id": 10,
            "options": {
                "legend": {"calcs": [], "displayMode": "list",
                           "placement": "bottom", "showLegend": False},
                "tooltip": {"hideZeros": False, "mode": "single",
                             "sort": "none"},
            },
            "pluginVersion": PLUGIN_VERSION,
            "targets": [{"queryType": "time_series",
                         "rawQueryText": Q_DAILY, "refId": "A"}],
            "title": "How many we made each day",
            "type": "timeseries",
        },
        # --- self-service help, so nobody has to ask ----------------------
        {
            "description": "Plain-language guide to the tiles and what to do "
                           "when a number looks odd.",
            "gridPos": {"h": 11, "w": 24, "x": 0, "y": 27},
            "id": 11,
            "options": {"content": HELP, "mode": "markdown"},
            "pluginVersion": PLUGIN_VERSION,
            "title": "How to read this board",
            "type": "text",
        },
    ]
    return panels


def build_dashboard():
    return {
        "annotations": {"list": [{
            "builtIn": 1,
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard",
        }]},
        "description": "Simple production board for the shop floor: what we "
                       "made, what is left, whether the quality is OK and "
                       "whether the data is live. Needs the "
                       "frser-sqlite-datasource plugin pointed at "
                       "production.db.",
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 0,
        "links": [],
        "panels": build_panels(),
        "refresh": "1m",
        "schemaVersion": 39,
        "tags": ["production", "shop-floor", "partdb"],
        "templating": {"list": [
            {
                "current": {},
                "datasource": {"type": "datasource", "uid": "grafana"},
                "definition": "frser-sqlite-datasource",
                "hide": 0,
                "includeAll": False,
                "label": "SQLite database (pick ProductionDB)",
                "multi": False,
                "name": "DS_SQLITE",
                "options": [],
                "query": "frser-sqlite-datasource",
                "refresh": 1,
                "regex": "",
                "skipUrlSync": False,
                "type": "datasource",
            },
            {
                "current": {"selected": True, "text": "All", "value": "All"},
                "datasource": DS,
                "definition": Q_PROJECT_LIST,
                "hide": 0,
                "includeAll": True,
                "allValue": "All",
                "label": "Which project?",
                "multi": False,
                "name": "project",
                "options": [],
                "query": Q_PROJECT_LIST,
                "refresh": 1,
                "regex": "",
                "skipUrlSync": False,
                "sort": 0,
                "type": "query",
            },
        ]},
        "time": {"from": "now-30d", "to": "now"},
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "1h"]},
        "timezone": "browser",
        "title": "Production Board",
        "uid": "production-workflow",
        "version": 3,
        "weekStart": "",
    }


def render():
    return json.dumps(build_dashboard(), indent=2) + "\n"


def main(argv):
    if "--stdout" in argv:
        sys.stdout.write(render())
        return 0
    text = render()
    if "--check" in argv:
        current = open(DASHBOARD_PATH).read()
        if current != text:
            print("production-dashboard-sqlite.json is stale - run "
                  "python3 tools/build_dashboard.py", file=sys.stderr)
            return 1
        print("dashboard JSON is up to date")
        return 0
    with open(DASHBOARD_PATH, "w") as fh:
        fh.write(text)
    print(f"wrote {DASHBOARD_PATH} ({len(text)} bytes, "
          f"{len(build_dashboard()['panels'])} panels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
