# Production Dashboard

Live manufacturing dashboard: **Part-DB stock → `partdb_sync.py` → `production.db` → Grafana**.

```
Part-DB (parts + part_lots) ──read-only──▶ partdb_sync.py ──▶ production.db ──▶ Grafana
        ▲                                        │                    ▲
        │ comment: PROD_PROJECT / PROD_STAGE     │ deltas, snapshots  │ frser-sqlite-datasource
        │                                        │ mappings           │ "Production Workflow" dashboard
```

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
datasource when asked for **Database**. Done — no UID editing needed.

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

The sync tracks **stock**. Quality and rework come from your QC process — log them so the
Quality/Rework panels light up:

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
- **Time picker works on trend panels** via `${__from}`/`${__to}`; KPI totals are
  all-time by design. (The plugin only implements `$__unixEpochGroupSeconds` as a macro,
  so plain global variables are used instead.)
- `sync.log`, `*.lock`, `*.db-wal`/`*.db-shm` are git-ignored; rotate logs with logrotate.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| KPI shows 0 but stock exists | Part comment lacks `PROD_PROJECT`, or sync failing | `check` + sync `--log-file` output |
| Part missing from “Current Stock by Part” | Not tagged / sync never ran | Tag comment, run sync |
| “stale” in Sync Status | No sync for 7+ days | Check cron + exit codes |
| Progress gauge 0% | `target_quantity` is 0 | `UPDATE production_projects SET target_quantity=…` |
| Quality/Rework blank | Tables empty (expected until QC logs) | See SQL above |
| Garbage names with `</span>` | Legacy v1 pollution | `clean-html` flow above |

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q     # 61 tests: parser, sync e2e, every dashboard
                                # query (single + All project), maintenance, schema parity
```

## Files

| File | Purpose |
|---|---|
| `partdb_sync.py` | The sync (stdlib only) |
| `db_maintenance.py` | `migrate` / `check` / `clean-html` (stdlib only) |
| `production_schema.sql` | Canonical schema (fresh installs) |
| `production-dashboard-sqlite.json` | Grafana dashboard (import-ready) |
| `grafana/provisioning/` | Datasource + dashboard provisioning samples |
| `tests/` | pytest suite |
| `production_schema_addon.sql` | Legacy (v1) — superseded by `production_schema.sql` |
| `partdb_sync.py.backup` | Legacy (v1) backup, kept for reference |
| `production.db` / `partdb.db` | Live data files |
