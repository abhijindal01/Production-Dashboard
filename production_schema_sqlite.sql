CREATE TABLE production_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    part_id INTEGER,
    start_date TEXT NOT NULL,
    target_quantity INTEGER NOT NULL,
    status TEXT DEFAULT 'planned',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE production_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    stage_name TEXT NOT NULL,
    sequence_order INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES production_projects(id)
);

CREATE TABLE daily_production (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    production_date TEXT NOT NULL,
    quantity_produced INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES production_projects(id),
    FOREIGN KEY (stage_id) REFERENCES production_stages(id)
);

CREATE TABLE quality_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    test_date TEXT NOT NULL,
    quantity_tested INTEGER NOT NULL DEFAULT 0,
    quantity_passed INTEGER NOT NULL DEFAULT 0,
    quantity_failed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES production_projects(id),
    FOREIGN KEY (stage_id) REFERENCES production_stages(id)
);

CREATE TABLE rework_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    rework_date TEXT NOT NULL,
    quantity_reworked INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    FOREIGN KEY (project_id) REFERENCES production_projects(id),
    FOREIGN KEY (stage_id) REFERENCES production_stages(id)
);

INSERT INTO production_projects (project_name, start_date, target_quantity, status)
VALUES ('Sample Project A', DATE('now'), 1000, 'in_progress');

INSERT INTO production_stages (project_id, stage_name, sequence_order) VALUES
  (1, 'Cutting', 1),
  (1, 'Assembly', 2),
  (1, 'Painting', 3),
  (1, 'Quality Check', 4),
  (1, 'Packaging', 5);

INSERT INTO daily_production (project_id, stage_id, production_date, quantity_produced)
VALUES (1, 1, DATE('now'), 120);

INSERT INTO quality_tests (project_id, stage_id, test_date, quantity_tested, quantity_passed, quantity_failed)
VALUES (1, 4, DATE('now'), 120, 115, 5);
