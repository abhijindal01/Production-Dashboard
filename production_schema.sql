-- ============================================================================
-- PRODUCTION DATABASE - CANONICAL SCHEMA (v2)
-- ============================================================================
-- This is the single source of truth for the production database layout.
-- It is mirrored by SCHEMA_STATEMENTS in partdb_sync.py (kept identical -
-- enforced by tests/test_schema_parity.py) so the sync script can heal any
-- database on its own, and this file exists for DBAs and fresh installs.
--
-- Usage for a brand-new database:
--     sqlite3 production.db < production_schema.sql
--
-- Usage for an EXISTING database (safe, idempotent):
--     python3 db_maintenance.py migrate production.db
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS production_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    part_id INTEGER,
    start_date TEXT NOT NULL,
    target_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    current_stage_id INTEGER REFERENCES production_stages(id)
);

CREATE TABLE IF NOT EXISTS production_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES production_projects(id),
    stage_name TEXT NOT NULL,
    sequence_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_production (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES production_projects(id),
    stage_id INTEGER NOT NULL REFERENCES production_stages(id),
    production_date TEXT NOT NULL,
    quantity_produced INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quality_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES production_projects(id),
    stage_id INTEGER NOT NULL REFERENCES production_stages(id),
    test_date TEXT NOT NULL,
    quantity_tested INTEGER NOT NULL DEFAULT 0,
    quantity_passed INTEGER NOT NULL DEFAULT 0,
    quantity_failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rework_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES production_projects(id),
    stage_id INTEGER NOT NULL REFERENCES production_stages(id),
    rework_date TEXT NOT NULL,
    quantity_reworked INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS part_stock_snapshot (
    part_id INTEGER PRIMARY KEY,
    last_known_quantity REAL NOT NULL DEFAULT 0,
    last_checked TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS production_part_mapping (
    part_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES production_projects(id),
    stage_id INTEGER NOT NULL REFERENCES production_stages(id),
    project_name TEXT NOT NULL,
    stage_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_production_project_stage_date
ON daily_production(project_id, stage_id, production_date);

CREATE INDEX IF NOT EXISTS idx_production_stages_project_order
ON production_stages(project_id, sequence_order);

CREATE INDEX IF NOT EXISTS idx_part_stock_snapshot_checked
ON part_stock_snapshot(last_checked);

CREATE INDEX IF NOT EXISTS idx_quality_tests_project_stage_date
ON quality_tests(project_id, stage_id, test_date);

CREATE INDEX IF NOT EXISTS idx_rework_log_project_stage_date
ON rework_log(project_id, stage_id, rework_date);

-- Backfill current_stage_id for projects missing one (safe to re-run).
UPDATE production_projects
SET current_stage_id = (
    SELECT id FROM production_stages
    WHERE production_stages.project_id = production_projects.id
    ORDER BY sequence_order DESC, id DESC
    LIMIT 1
)
WHERE current_stage_id IS NULL
  AND EXISTS (
      SELECT 1 FROM production_stages
      WHERE production_stages.project_id = production_projects.id
  );
