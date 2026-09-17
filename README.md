# Production Dashboard

Live manufacturing dashboard: **Part-DB stock → `partdb_sync.py` → `production.db` → Grafana**.

```
Part-DB (parts + part_lots) ──read-only──▶ partdb_sync.py ──▶ production.db ──▶ Grafana
        ▲                                        │                    ▲
        │ comment: PROD_PROJECT / PROD_STAGE     │ deltas, snapshots  │ frser-sqlite-datasource
        │                                        │ mappings           │ "Production Board" dashboard
```

## The board: 11 tiles, plain words

`production-dashboard-sqlite.json` is deliberately small - it is written for
someone who walks past a screen and needs the answer in five seconds, not for
the engineer who owns the schema.

| Tile | What it answers |
|---|---|
| **Made today** | Did we book anything today? |
| **Made in the last 7 days** | How is the week going? |
| **Still to make** | How many are left? (0 = goal reached) |
| **Goal finished** | How far are we, in percent? |
| **Quality: pieces that passed** | Out of 100 checked, how many were OK? |
| **Sent back to be fixed** | How much had to be redone? |
| **Is this page current?** | Green = live, red = the sync stopped and these numbers are old |
| **Each step, in order** | Which step of the job the pieces are at |
| **Every job, one line each** | The same story per project |
| **How many we made each day** | Day-by-day bars (the only panel the time picker moves) |
| **How to read this board** | Self-service help, so nobody has to ask |

Design rules the tests enforce (see `tests/test_dashboard.py`):

* at most 12 panels, no collapsed rows, the four headline numbers top-left;
* no schema words on the board (`delta`, `net`, `stale`, `snapshot`, column
  names ...) - every tile carries a one-line plain-language explanation;
* an unknown value is never printed as `0`: no checks logged and "no goal set"
  have their own wording (`noValue`);
* a `0` is never red, so a quiet morning does not look like a breakdown;
* the only knob a viewer gets is **Which project?** (defaults to *All*).

Detail panels that v2 had (gross vs corrected units, live Part-DB stock per
part, per-stage quality, cumulative-vs-target line, rework trend, raw log,
part mapping/sync health) are **not** on the board any more - they live in
`production.db` and can be queried directly, or re-added to a second
"engineers" dashboard.

## What was fixed in v2

| # | Root cause | Symptom on the dashboard | Fix |
|---|-----------|--------------------------|-----|
| 1 | New projects were inserted **without `target_quantity`** (`NOT NULL` column) → `IntegrityError` → **whole sync rolled back**. 9,953 of 11,177 logged runs failed this way. | Stale / missing data; new projects never appeared | Inserts include `target_quantity` + `status`; per-part savepoints isolate failures; schema self-heals |
| 2 | Comment parser only stripped `<br>`/`&nbsp;`, leaking `</span> <span style="…">` into names | Garbage project/stage names, duplicate projects | Full HTML strip + entity decode; `clean-html` repair tool merges existing dupes without losing history |
| 3 | `production-dashboard-sqlite.json` ended with a stray `\` → **invalid JSON** | Import unreliable / broken | File rewritten and validated by tests |
| 4 | Progress gauge used `MAX(target)` | Wrong % with “All” projects | Uses `SUM(target)` via duplication-safe subqueries |
| 5 | Time-series panels returned bare `YYYY-MM-DD` as `time` | “Data is missing a time field” (plugin needs RFC3339) | Emits `YYYY-MM-DDT00:00:00Z` |
| 6 | `current_stage_id` = last part processed | Current stage flapped on multi-part projects | Set once per run to the most advanced stage |
| 7 | Dashboard hardcoded one datasource UID | Import fails on any other Grafana | Portable `${DS_SQLITE}` datasource variable |

## Quick start

### 1. Install the Grafana plugin

```bash
grafana-cli plugins install frser-sqlite-datasource
# restart grafana-server
```

### 2. Add the datasource

Grafana → Connections → Data sources → **SQLite** (`frser-sqlite-datasource`), path = your
`production.db`. Or provision it (see `grafana/provisioning/`).

### 3. Import the dashboard

Dashboards → New → Import → upload `production-dashboard-sqlite.json` → pick your SQLite
datasource when asked for **SQLite database (pick ProductionDB)**. Done — no UID editing
needed. Re-importing updates the board in place (same UID `production-workflow`); it shows
up as **Production Board**.

Not on a Grafana box yet? Preview the same JSON, same queries, same layout:

```bash
python3 tools/preview_dashboard.py --db production.db --port 8080
# -> http://localhost:8080  (read-only, auto-refreshes every 30 s)
```

The dashboard JSON is generated, so editing it by hand is not needed:

```bash
python3 tools/build_dashboard.py --check   # is the committed JSON up to date?
python3 tools/build_dashboard.py           # regenerate it after a generator change
```

### 4. Prepare the database (safe, idempotent)

```bash
python3 db_maintenance.py migrate production.db
python3 db_maintenance.py check production.db
```

### 5. Run the sync (cron)

```bash
*/5 * * * * /usr/bin/python3 /opt/Production-Dashboard/partdb_sync.py /path/to/partdb.db /path/to/production.db --log-file /var/log/partdb_sync.log
```

Useful flags: `--dry-run`, `--verbose`, `--log-file PATH`, `--lock-file PATH`,
`--default-target N` (target for newly created projects), `--no-wal`.

Newest run time is what the **Is this page current?** tile reads, so a dead cron
job turns that tile red instead of showing zeros everywhere.

Exit codes: `0` ok · `1` fatal (nothing committed) · `2` completed with per-part
errors (good parts committed — check the log) · `3` another sync already running.

## Tagging parts in Part-DB

Put this in the part's **comment** field (rich-text/HTML is fine — markup is stripped):

```
PROD_PROJECT=Heatsink PROD_STAGE=Anodised
```

`PROD_STAGE` is optional; without it the delta is booked to the project's current stage
(or retried next run if the project has no stage yet). Stock changes become `+` additions
or `−` corrections in `daily_production`; first sight of a part only sets a baseline.

## Recording quality tests & rework

The sync tracks **stock**. Quality and rework come from your QC process — log them and the
**Quality: pieces that passed** and **Sent back to be fixed** tiles start showing numbers:

```sql
-- 50 tested, 48 passed, 2 failed for Heatsink / Anodised on 2026-09-09
INSERT INTO quality_tests (project_id, stage_id, test_date,
                           quantity_tested, quantity_passed, quantity_failed)
SELECT pp.id, ps.id, '2026-09-09', 50, 48, 2
FROM production_projects pp JOIN production_stages ps ON ps.project_id = pp.id
WHERE pp.project_name = 'Heatsink' AND ps.stage_name = 'Anodised';

-- 3 units reworked
INSERT INTO rework_log (project_id, stage_id, rework_date, quantity_reworked, reason)
SELECT pp.id, ps.id, '2026-09-09', 3, 'scratch marks'
FROM production_projects pp JOIN production_stages ps ON ps.project_id = pp.id
WHERE pp.project_name = 'Heatsink' AND ps.stage_name = 'After sand blasting';

-- set a project target (drives Progress vs Target)
UPDATE production_projects SET target_quantity = 1000 WHERE project_name = 'Heatsink';
```

## Repairing a live DB polluted by the old parser

```bash
cp production.db production.db.bak            # always back up first
python3 db_maintenance.py clean-html production.db --dry-run   # inspect the plan
python3 db_maintenance.py clean-html production.db             # rename + merge dupes
python3 db_maintenance.py check production.db
```

Merges re-point production history onto the surviving rows — totals are preserved
(covered by `tests/test_maintenance.py`).

## Operations notes

- **Part-DB is opened read-only** (`mode=ro`); the sync can never write to it (tested).
- **WAL mode** is enabled on `production.db` so Grafana can read while the sync writes;
  pass `--no-wal` if your setup forbids the `-wal`/`-shm` sidecars.
- **Lock file** (`<production>.lock`) prevents overlapping cron runs from interleaving.
- **The time picker only moves the "How many we made each day" chart** (via
  `${__from}`/`${__to}`); every tile above it is all-time by design, so nobody has to
  set a range to know where the job stands. (The plugin only implements
  `$__unixEpochGroupSeconds` as a macro, so plain global variables are used instead.)
- **"Is this page current?" is the health check**: it reads the newest sync timestamp,
  so a silent cron failure shows up as a red tile instead of a board full of zeros.
- `sync.log`, `*.lock`, `*.db-wal`/`*.db-shm` are git-ignored; rotate logs with logrotate.

## Troubleshooting

| Symptom on the board | Likely cause | Action |
|---|---|---|
| “Made today” is 0 but stock exists | Part comment lacks `PROD_PROJECT`, or sync failing | `check` + sync `--log-file` output |
| “Is this page current?” is red/orange | No sync since 7+ days (red) or 1+ day (orange) | Check cron + exit codes |
| “Goal finished” says *goal not set yet* | `target_quantity` is 0 | `UPDATE production_projects SET target_quantity=…` |
| Quality tile says *no checks logged yet* | `quality_tests` is empty (expected until QC logs) | See SQL above |
| Bar chart empty but numbers are non-zero | Chosen time range is too short | Widen the picker (default 30 days) |
| Garbage names with `</span>` | Legacy v1 pollution | `clean-html` flow above |

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q     # 78 tests: parser, sync e2e, every dashboard
                                # query (single + All + unknown project), readability
                                # rules, maintenance, schema parity
```

## Files

| File | Purpose |
|---|---|
| `partdb_sync.py` | The sync (stdlib only) |
| `db_maintenance.py` | `migrate` / `check` / `clean-html` (stdlib only) |
| `production_schema.sql` | Canonical schema (fresh installs) |
| `production-dashboard-sqlite.json` | Grafana dashboard (import-ready, generated) |
| `tools/build_dashboard.py` | Generates that JSON from one readable source |
| `tools/preview_dashboard.py` | Browser preview of the board without Grafana |
| `grafana/provisioning/` | Datasource + dashboard provisioning samples |
| `tests/` | pytest suite |
| `production_schema_addon.sql` | Legacy (v1) — superseded by `production_schema.sql` |
| `partdb_sync.py.backup` | Legacy (v1) backup, kept for reference |
| `production.db` / `partdb.db` | Live data files |
