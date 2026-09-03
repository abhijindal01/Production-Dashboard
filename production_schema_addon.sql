-- Production database addon for Part-DB stock sync
-- Safe for the CURRENT production.db that already contains:
--   production_projects.current_stage_id
--   part_stock_snapshot
--
-- This supports both positive and negative quantity changes.
-- Example: stock 100 -> 120 logs +20
--          stock 120 -> 90  logs -30

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS part_stock_snapshot (
    part_id INTEGER PRIMARY KEY,
    last_known_quantity REAL NOT NULL DEFAULT 0,
    last_checked TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_production_project_stage_date
ON daily_production(project_id, stage_id, production_date);

CREATE INDEX IF NOT EXISTS idx_production_stages_project_order
ON production_stages(project_id, sequence_order);

CREATE INDEX IF NOT EXISTS idx_part_stock_snapshot_checked
ON part_stock_snapshot(last_checked);

-- Grouped part quantities:
-- Multiple parts with the SAME name inside the same project + stage
-- are merged into one row so the dashboard can show them as a group.
CREATE TABLE IF NOT EXISTS production_part_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    part_name TEXT NOT NULL,
    total_quantity INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, stage_id, part_name),
    FOREIGN KEY (project_id) REFERENCES production_projects(id),
    FOREIGN KEY (stage_id) REFERENCES production_stages(id)
);

CREATE INDEX IF NOT EXISTS idx_part_groups_name
ON production_part_groups(part_name);

-- Initialize current_stage_id for projects that do not yet have one.
-- This is safe to run repeatedly.
UPDATE production_projects
SET current_stage_id = (
    SELECT id
    FROM production_stages
    WHERE production_stages.project_id = production_projects.id
    ORDER BY sequence_order ASC
    LIMIT 1
)
WHERE current_stage_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM production_stages
      WHERE production_stages.project_id = production_projects.id
  );
