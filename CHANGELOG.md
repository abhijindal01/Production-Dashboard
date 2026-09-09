# Changelog

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
