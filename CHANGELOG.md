# Changelog

## v2.1.0 (2026-09-17)

The dashboard was rewritten as a **shop-floor board**: 11 tiles instead of 15
panels, plain words instead of schema vocabulary, and a built-in guide so
nobody has to ask what a number means.

### Changed (dashboard `production-dashboard-sqlite.json`)
- Board is now **11 tiles, no rows**: *Made today*, *Made in the last 7 days*,
  *Still to make*, *Goal finished*, *Quality: pieces that passed*,
  *Sent back to be fixed*, *Is this page current?*, *Each step, in order*,
  *Every job, one line each*, *How many we made each day*, *How to read this
  board* (markdown guide). Title is now **Production Board** (UID kept, so a
  re-import updates existing installs).
- Removed from the board: *Units Added*, *Corrections*, *Total Produced (net)*,
  *Current Stock by Part*, *Quality by Stage*, *Cumulative Output vs Target*,
  *Rework Trend*, *Recent Production Log*, *Part Mapping & Sync Status*,
  *Daily Output by Stage*, *Net Output by Stage*, *Project Overview*. All of it
  stays in `production.db` for engineers (README documents how to query it).
- Jargon gone: tiles no longer say *net*, *delta*, *correction*, *stale*,
  *snapshot*, *KPI* or any table/column name; the day chart is a single "Made"
  bar series instead of one series per stage.
- Statuses are translated where they are read: `in_progress` → *Running*,
  `completed` → *Finished*, `stale/never synced` → *Sync stopped - numbers are
  old* (red background tile), NULL current stage → *not decided yet*.
- Unknown is no longer shown as 0: *Quality: pieces that passed* prints
  *no checks logged yet* when nothing has been recorded, *Goal finished* prints
  *goal not set yet* when `target_quantity` is 0 - instead of a misleading,
  alarming `0` / `0%`.
- A `0` is never coloured red (a quiet morning is not a breakdown); only real
  trouble is.
- Time range defaults to 30 days (was 30 days for trend panels only), refresh
  1m, only two variables left: the datasource picker and **Which project?**
  (defaults to *All*, single-select).
- Still portable: `${DS_SQLITE}` variable, RFC3339 time columns,
  duplication-safe `SUM(target)` progress.

### Added
- `tools/build_dashboard.py` - the JSON is generated from one readable source;
  `--check` fails if the committed file is stale (a test enforces this).
- `tools/preview_dashboard.py` - renders the same JSON (same SQL, thresholds,
  mappings, layout) in a browser without Grafana, read-only, self-refreshing.
- `tests/test_dashboard.py` rewritten for the new board: 45 dashboard tests
  including readability rules (no jargon, every tile explains itself, no
  overlapping/undersized tiles, help panel present) and an "empty database
  still renders the whole board" case. Total suite: 78 tests.

## v2.0.0 (2026-09-09)

### Fixed (sync `partdb_sync.py`)
- New projects are created **with** `target_quantity`/`status`: eliminates the
  `NOT NULL constraint failed: production_projects.target_quantity` crash that
  failed ~89% of logged v1 runs and rolled back good deltas with them.
- Comment parser strips **all** HTML tags + entities (was: only `<br>`/`&nbsp;`),
  ending `</span> <span style="…">` pollution in project/stage names.
- Tags are line-scoped; empty `PROD_PROJECT` no longer swallows `PROD_STAGE`.
- `current_stage_id` is set once per run to the most advanced stage (was: last
  part processed → flapping on multi-part projects).
- Per-part savepoints inside one explicit transaction: one bad part can't wipe
  out the run; fatal errors still roll back all data changes.
- Two-phase sync: stages declared by later parts are visible to earlier parts'
  fallback in the same run.
- Fractional stock handled (sub-unit drift absorbed, snapshot exact); no more
  `int()` truncation surprises.
- Part-DB opened **read-only**; production DB gets WAL + busy timeout, lock file
  against overlapping runs, `--dry-run`, `--log-file`, `--default-target`.

### Added
- `db_maintenance.py`: `migrate` (idempotent schema repair + uniqueness guards),
  `check` (read-only health report), `clean-html` (rename + merge duplicates,
  history preserved, dry-run first).
- `production_schema.sql`: canonical schema (parity-tested against the sync).
- `tests/`: 61 pytest tests (parser, sync end-to-end, every dashboard query for
  single + All project incl. time filtering, maintenance, schema parity).
- `grafana/provisioning/`: datasource + dashboard provisioning samples.
- `README.md` runbook (setup, tagging, QC logging, repair, troubleshooting).

### Fixed (dashboard `production-dashboard-sqlite.json`)
- File is valid JSON again (stray trailing `\` removed).
- Progress vs Target uses `SUM(target)` (was `MAX`), duplication-safe.
- Time columns are RFC3339 (`…T00:00:00Z`); trend panels follow the time picker.
- Stage series are project-prefixed (no collisions on “All”).
- Portable `${DS_SQLITE}` datasource variable (no hardcoded UID).
- New panels: Units Added, Corrections, Current Stock by Part, Cumulative Output
  vs Target, Recent Production Log, Project Overview, Part Mapping & Sync Status;
  every panel has a description, units and sane thresholds; refresh 30s.
