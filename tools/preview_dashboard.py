#!/usr/bin/env python3
"""Preview `production-dashboard-sqlite.json` in a browser, without Grafana.

The real board runs in Grafana. This tool exists so the layout, the wording
and the numbers can be checked on any machine that has Python and the SQLite
file - it reads the dashboard JSON, runs every panel query exactly the way
Grafana would (same $project / time-range substitution) and renders the tiles
with the same titles, descriptions, units, thresholds and colour mappings.

    python3 tools/preview_dashboard.py                      # http://host:8080
    python3 tools/preview_dashboard.py --db production.db --port 8080
    python3 tools/preview_dashboard.py --project Heatsink   # start filtered

Stdlib only. Read-only: the database is opened with `mode=ro`.
"""

import argparse
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_PATH = os.path.join(REPO_ROOT, "production-dashboard-sqlite.json")

STATE = {"db": None, "dashboard": None}

RELATIVE = re.compile(r"^now-(\d+)([dwmy])?$")
UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def lookback_days(dashboard):
    """Same default range as the dashboard JSON (e.g. 'now-30d' -> 30)."""
    match = RELATIVE.match(dashboard.get("time", {}).get("from", ""))
    if not match:
        return 30
    return max(1, int(match.group(1)) * UNIT_DAYS[match.group(2) or "d"])


# --------------------------------------------------------------------- SQL
def interpolate(sql, project, from_ms, to_ms):
    """Grafana's frontend substitution for the variables used in the JSON."""
    return (sql.replace("$project", project)
               .replace("${__from}", str(from_ms))
               .replace("${__to}", str(to_ms)))


def run_query(db_path, sql, params=()):
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def panel_frame(panel):
    return {"id": panel["id"], "title": panel["title"], "type": panel["type"],
            "description": panel.get("description", ""),
            "gridPos": panel["gridPos"]}


# ------------------------------------------------------------------ values
def ordered_thresholds(defaults):
    steps = [s for s in defaults.get("thresholds", {}).get("steps", [])]
    base = [s for s in steps if s.get("value") is None]
    rest = sorted((s for s in steps if s.get("value") is not None),
                  key=lambda s: s["value"])
    return (base[0]["color"] if base else "green"), rest


def colour_for(value, defaults):
    base, rest = ordered_thresholds(defaults)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return base
    colour = base
    for step in rest:
        if value >= step["value"]:
            colour = step["color"]
    return colour


def mapped_text(value, defaults):
    """Apply Grafana `value` mappings so codes print as words + colour."""
    for entry in defaults.get("mappings", []):
        if entry.get("type") != "value":
            continue
        option = entry.get("options", {}).get(str(value))
        if option:
            return option.get("text", str(value)), option.get("color")
    return None, None


def format_value(value, unit, decimals=None):
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        number = (f"{value:.1f}" if decimals == 1
                  else f"{value:.1f}" if isinstance(value, float) and
                  abs(value - round(value)) > 1e-9
                  else f"{int(round(value)):,}")
        if unit == "percent":
            return f"{number}%"
        if unit in ("none", "short", None):
            return number
        return f"{number} {unit}"
    return str(value)


def render_stat(panel, rows):
    defaults = panel.get("fieldConfig", {}).get("defaults", {})
    value = rows[0]["value"] if rows else None
    if value is None:
        return {"kind": "stat", "text": defaults.get("noValue") or "no data",
                "colour": "#8c8c8c", "note": "nothing recorded yet"}
    mapping = mapped_text(value, defaults)
    if mapping[0]:
        return {"kind": "stat", "text": mapping[0], "colour": mapping[1],
                "note": ""}
    return {"kind": "stat",
            "text": format_value(value, defaults.get("unit", "short")),
            "colour": colour_for(value, defaults), "note": ""}


def render_gauge(panel, rows):
    defaults = panel.get("fieldConfig", {}).get("defaults", {})
    value = rows[0]["value"] if rows else None
    if value is None:
        return {"kind": "gauge", "text": defaults.get("noValue") or "no data",
                "pct": 0, "colour": "#8c8c8c", "note": ""}
    lo = defaults.get("min", 0)
    hi = defaults.get("max", 100)
    pct = max(0, min(100, (value - lo) * 100.0 / (hi - lo or 1)))
    return {"kind": "gauge",
            "text": format_value(value, defaults.get("unit", "percent")),
            "pct": pct, "colour": colour_for(value, defaults), "note": ""}


def render_bargauge(panel, rows):
    defaults = panel.get("fieldConfig", {}).get("defaults", {})
    top = max([abs(r["value"]) for r in rows] + [1])
    return {"kind": "bargauge", "rows": [
        {"label": r["metric"],
         "text": format_value(r["value"], defaults.get("unit", "short")),
         "pct": abs(r["value"]) * 100.0 / top} for r in rows]}


def render_table(panel, rows):
    if not rows:
        return {"kind": "table", "columns": [], "rows": []}
    defaults = panel.get("fieldConfig", {}).get("defaults", {})
    overrides = {}
    for entry in panel.get("fieldConfig", {}).get("overrides", []):
        if entry["matcher"]["id"] == "byName":
            props = {p["id"]: p["value"] for p in entry["properties"]}
            overrides[entry["matcher"]["options"]] = props
    columns = list(rows[0].keys())
    out = []
    for row in rows:
        cells = []
        for column in columns:
            value = row[column]
            props = overrides.get(column, {})
            unit = props.get("unit", defaults.get("unit", "short"))
            text = value if isinstance(value, str) else format_value(
                value, unit)
            if text is None:
                text = ""
            colour = None
            cell_type = props.get("custom.cellOptions", {})
            if cell_type.get("type") in ("color-background", "color-text"):
                merged = dict(defaults)
                merged["thresholds"] = props.get("thresholds",
                                                 defaults["thresholds"])
                colour = colour_for(value, merged)
            cells.append({"text": text, "colour": colour,
                          "type": cell_type.get("type", "auto")})
        out.append(cells)
    return {"kind": "table", "columns": columns, "rows": out}


def render_timeseries(panel, rows):
    top = max([abs(r["value"]) for r in rows] + [1])
    return {"kind": "timeseries", "rows": [
        {"day": r["time"][:10], "value": r["value"],
         "pct": abs(r["value"]) * 100.0 / top} for r in rows]}


# ------------------------------------------------------------------ data API
def panel_payload(panel, project, from_ms, to_ms):
    payload = panel_frame(panel)
    if panel["type"] == "text":
        payload["data"] = {"kind": "text",
                           "html": markdown(panel["options"]["content"])}
        return payload
    rows = []
    for target in panel.get("targets", []):
        sql = interpolate(target["rawQueryText"], project, from_ms, to_ms)
        rows.extend(run_query(STATE["db"], sql))
        if panel["type"] != "table" or len(panel.get("targets", [])) == 1:
            break
    renderer = {"stat": render_stat, "gauge": render_gauge,
                "bargauge": render_bargauge, "table": render_table,
                "timeseries": render_timeseries}.get(panel["type"])
    payload["data"] = renderer(panel, rows) if renderer else {
        "kind": "raw", "rows": rows[:50]}
    return payload


def board_payload(project=None):
    dashboard = STATE["dashboard"]
    projects = [r["project_name"] for r in run_query(
        STATE["db"], dashboard["templating"]["list"][1]["query"])]
    chosen = project or "All"
    if chosen not in projects and chosen != "All":
        chosen = "All"
    now = datetime.now(timezone.utc)
    to_ms = int(now.timestamp() * 1000)
    from_ms = int((now - timedelta(days=lookback_days(dashboard)))
                  .timestamp() * 1000)
    return {"title": dashboard["title"], "projects": ["All"] + projects,
            "project": chosen, "panels": [
                panel_payload(panel, chosen, from_ms, to_ms)
                for panel in dashboard["panels"]],
            "generated": now.strftime("%Y-%m-%d %H:%M:%S") + " UTC",
            "db": os.path.basename(STATE["db"])}


# ----------------------------------------------------------------- markdown
def markdown(text):
    """Just enough Markdown for the help panel: headings, bold, lists, tables."""
    lines, out, in_table, list_open = text.split("\n"), [], False, False
    def inline(raw):
        escaped = html.escape(raw)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
        return escaped
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [inline(c.strip()) for c in
                     stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in
                   [x.strip() for x in stripped.strip("|").split("|")]):
                continue
            if not in_table:
                out.append("<table class='md'><tr>" +
                           "".join(f"<th>{c}</th>" for c in cells) +
                           "</tr>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{c}</td>"
                                           for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if list_open and not stripped.startswith(("- ", "1.", "2.", "3.",
                                                   "4.", "5.", "6.", "7.")):
            out.append("</ul>")
            list_open = False
        if stripped.startswith("### "):
            out.append(f"<h3>{inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{inline(stripped[3:])}</h2>")
        elif stripped.startswith("- "):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
        elif re.match(r"^\d+\.\s", stripped):
            out.append(f"<p class='num'>{inline(stripped)}</p>")
        elif stripped:
            out.append(f"<p>{inline(stripped)}</p>")
    if in_table:
        out.append("</table>")
    if list_open:
        out.append("</ul>")
    return "\n".join(out)


# ------------------------------------------------------------------- the page
CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0b0c0e; color:#d6d9de; font:14px/1.45
       "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
header { display:flex; align-items:baseline; gap:16px; flex-wrap:wrap;
         padding:14px 18px; background:#14161a; border-bottom:1px solid #24262b; }
header h1 { font-size:19px; margin:0; font-weight:600; letter-spacing:.2px; }
header .meta { color:#8a9099; font-size:12.5px; }
select { background:#1c1f24; color:#d6d9de; border:1px solid #2c3038;
         border-radius:6px; padding:5px 8px; font-size:13.5px; }
.board { display:grid; grid-template-columns:repeat(24, 1fr); gap:8px;
         padding:12px; align-items:stretch; }
.panel { background:#14161a; border:1px solid #22252b; border-radius:8px;
         padding:10px 12px; display:flex; flex-direction:column;
         grid-auto-flow:row; min-height:0; overflow:hidden; }
.panel > .phead { display:flex; justify-content:space-between; align-items:center;
                  gap:8px; font-size:13px; color:#b9bfc7; font-weight:600;
                  text-transform:none; margin-bottom:6px; }
.panel > .phead .info { color:#5f6672; font-size:11px; cursor:help; }
.value { font-size:40px; font-weight:700; line-height:1.05;
         display:flex; flex-direction:column; justify-content:center;
         flex:1; text-align:center; }
.value small { font-size:12px; font-weight:500; color:#868d97;
               margin-top:6px; text-align:center; }
.big { font-size:34px; }
.gaugebar { height:12px; border-radius:7px; background:#22252b; margin:8px 4px 2px;
            overflow:hidden; }
.gaugebar > div { height:100%; border-radius:7px; }
table.data { width:100%; border-collapse:collapse; font-size:13px; flex:1; }
table.data th { text-align:left; color:#8a9099; font-weight:600; padding:4px 6px;
                border-bottom:1px solid #262a31; white-space:nowrap; }
table.data td { padding:5px 6px; border-bottom:1px solid #1c1f24;
                white-space:nowrap; }
td.cell { border-radius:5px; }
.bars { flex:1; display:flex; flex-direction:column; justify-content:center;
        gap:9px; }
.bars .row { display:grid; grid-template-columns:minmax(90px, 32%) 1fr 54px;
             gap:8px; align-items:center; font-size:13px; }
.bars .track { background:#1c1f24; border-radius:4px; height:16px;
               overflow:hidden; }
.bars .fill { height:100%; background:linear-gradient(90deg,#3274d9,#5794f2);
              border-radius:4px; }
.bars .num { text-align:right; color:#c8cdd4; font-variant-numeric:tabular-nums; }
.days { flex:1; display:flex; align-items:flex-end; gap:6px; padding-top:6px; }
.days .day { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
             align-items:center; height:100%; gap:4px; min-width:0; }
.days .stack { display:flex; flex-direction:column; justify-content:flex-end;
               align-items:center; width:100%; height:100%; }
.days .bar { width:70%; border-radius:3px 3px 0 0; background:#56a659;
             min-height:2px; }
.days .bar.neg { background:#e02f44; border-radius:0 0 3px 3px; }
.days .lbl { font-size:10px; color:#7d848e; writing-mode:vertical-rl;
             transform:rotate(180deg); white-space:nowrap; }
.md { flex:1; overflow:auto; font-size:13.5px; color:#c3c8d0; }
.md h3 { color:#e6e9ee; font-size:15px; margin:14px 0 8px; }
.md h3:first-child { margin-top:2px; }
.md p { margin:6px 0; }
.md code { background:#1c1f24; padding:1px 5px; border-radius:4px;
           font-size:12.5px; color:#9fd3ff; }
table.md { border-collapse:collapse; margin:8px 0; width:100%; }
table.md th, table.md td { border:1px solid #262a31; padding:6px 8px;
                           text-align:left; vertical-align:top; }
table.md th { background:#1a1d22; color:#aeb4bd; }
.warn { margin:0 12px; padding:8px 12px; border-radius:8px; font-size:12.5px;
        background:#1d1a10; border:1px solid #4a3b12; color:#d8c48c; }
footer { padding:0 12px 14px; color:#5f6672; font-size:11.5px; }
"""

JS = """
const REFRESH_MS = 30000;
async function load() {
  const project = new URLSearchParams(location.search).get('project') || '';
  const res = await fetch('/data' + (project ? '?project=' +
                            encodeURIComponent(project) : ''));
  const board = await res.json();
  document.getElementById('meta').textContent =
      'database ' + board.db + ' · refreshed ' + board.generated +
      ' · every ' + (REFRESH_MS/1000) + 's';
  const sel = document.getElementById('project');
  if (sel.options.length !== board.projects.length) {
    sel.innerHTML = board.projects.map(p =>
      `<option ${p === board.project ? 'selected' : ''}>${p}</option>`)
      .join('');
  }
  document.getElementById('board').innerHTML =
      board.panels.map(render).join('');
  document.getElementById('stamp').textContent =
      'Preview of production-dashboard-sqlite.json - import that file into ' +
      'Grafana for the real board.';
}
function frame(p, inner) {
  const g = p.gridPos;
  return `<section class="panel" style="grid-column:${g.x + 1} / span ${g.w};
      grid-row:${g.y + 1} / span ${g.h}">
      <div class="phead"><span>${esc(p.title)}</span>
        <span class="info" title="${esc(p.description)}">?</span></div>
      ${inner}</section>`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function render(p) {
  const d = p.data || {};
  if (d.kind === 'text') return frame(p, `<div class="md">${d.html}</div>`);
  if (d.kind === 'stat') return frame(p,
    `<div class="value" style="color:${d.colour}">${esc(d.text)}
       ${d.note ? `<small>${esc(d.note)}</small>` : ''}</div>`);
  if (d.kind === 'gauge') return frame(p,
    `<div class="value" style="color:${d.colour}">${esc(d.text)}</div>
     <div class="gaugebar"><div style="width:${d.pct}%;background:${d.colour}">
     </div></div>`);
  if (d.kind === 'bargauge') return frame(p, `<div class="bars">` +
    (d.rows.length ? d.rows.map(r => `<div class="row"><span>${esc(r.label)}
      </span><span class="track"><span class="fill" style="width:${r.pct}%">
      </span></span><span class="num">${esc(r.text)}</span></div>`).join('')
      : '<div class="value small">nothing yet</div>') + '</div>');
  if (d.kind === 'table') return frame(p, d.columns.length ?
    `<table class="data"><tr>${d.columns.map(c => `<th>${esc(c)}</th>`)
      .join('')}</tr>${d.rows.map(row => '<tr>' + row.map(cell =>
      `<td class="cell" style="${cell.type === 'color-background'
          ? 'background:' + (cell.colour || 'transparent') + ';color:#0b0c0e'
          : ''}">${cell.type === 'color-background' || !cell.colour
          ? esc(cell.text)
          : `<span style="color:${cell.colour};font-weight:600">
             ${esc(cell.text)}</span>`}</td>`).join('') + '</tr>').join('')}
     </table>` : '<div class="value">no rows yet</div>');
  if (d.kind === 'timeseries') return frame(p, `<div class="days">` +
    (d.rows.length ? d.rows.map(r => `<div class="day">
       <div class="stack">${r.value >= 0
         ? `<span class="bar" style="height:${r.pct}%"></span>` : ''}</div>
       <div class="stack">${r.value < 0
         ? `<span class="bar neg" style="height:${r.pct}%"></span>` : ''}</div>
       <span class="lbl">${esc(r.day.slice(5))} · ${esc(r.value)}</span>
     </div>`).join('') : '<div class="value">no days in range</div>') +
    '</div>');
  return frame(p, `<pre>${esc(JSON.stringify(d, null, 1))}</pre>`);
}
document.getElementById('project').addEventListener('change', e => {
  const url = new URL(location.href);
  if (e.target.value === 'All') url.searchParams.delete('project');
  else url.searchParams.set('project', e.target.value);
  history.replaceState(null, '', url); load();
});
load();
setInterval(load, REFRESH_MS);
"""

PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Production Board - preview</title><style>{CSS}</style></head>
<body>
<header>
  <h1>Production Board</h1>
  <label>Which project?
    <select id="project"><option>All</option></select></label>
  <span class="meta" id="meta">loading&hellip;</span>
</header>
<p class="warn">Preview of the layout and the wording only - the real board is
   Grafana reading <code>production-dashboard-sqlite.json</code>. Charts here
   are simplified; tiles, titles, numbers and colours come from the same JSON.</p>
<main class="board" id="board"></main>
<footer><span id="stamp"></span></footer>
<script>{JS}</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, content_type="text/html; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                default = STATE.get("project") or "All"
                if default != "All" and not parsed.query:
                    self.send_response(302)
                    self.send_header("Location", f"/?project={default}")
                    self.end_headers()
                    return
                return self._send(PAGE)
            if parsed.path == "/data":
                project = (parse_qs(parsed.query).get("project")
                           or [None])[0]
                return self._send(json.dumps(board_payload(project)),
                                  "application/json")
            if parsed.path == "/healthz":
                return self._send("ok", "text/plain")
            return self._send("not found", "text/plain", 404)
        except Exception as exc:  # surface SQL problems in the browser
            return self._send(json.dumps({"error": f"{type(exc).__name__}: "
                                                   f"{exc}"}),
                              "application/json", 500)

    def log_message(self, *args):
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.path.join(REPO_ROOT,
                                                     "production.db"),
                        help="path to production.db")
    parser.add_argument("--dashboard", default=DASHBOARD_PATH)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--project", default="All")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"database not found: {args.db}")
    with open(args.dashboard) as fh:
        STATE["dashboard"] = json.load(fh)
    STATE["db"] = os.path.abspath(args.db)
    STATE["project"] = args.project

    print(f"[preview] {STATE['db']}  ->  http://{args.host}:{args.port}/"
          f"?project={args.project}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
