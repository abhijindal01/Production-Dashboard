#!/usr/bin/env python3
"""
PART-DB -> PRODUCTION SYNC (v2)

Reads production-tagged parts from a Part-DB SQLite database, compares live
stock levels against a local snapshot, and logs the resulting deltas
(additions AND corrections/subtractions) into the production database that
backs the Grafana "Production Workflow" dashboard.

Part-DB comment format (put this in the part's comment field)::

    PROD_PROJECT=<project name> PROD_STAGE=<stage name>

The stage part is optional. If it is missing, the delta is booked against
the project's current stage.

Usage::

    python3 partdb_sync.py [--dry-run] [--verbose] [--log-file PATH]
                           [--lock-file PATH] [--no-wal] [--default-target N]
                           <partdb.db> <production.db>

Exit codes:
    0 - success (all parts processed, changes committed)
    1 - fatal error (nothing was committed)
    2 - completed with per-part errors (good parts committed, bad parts
        skipped - check the log)
    3 - another sync is already running (lock file held)

This script uses the Python standard library only so it can run on minimal
hosts via cron without dependency management.
"""

import argparse
import html
import logging
import math
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

try:
    import fcntl  # POSIX file locking (Linux/macOS)
except ImportError:  # pragma: no cover - Windows hosts
    fcntl = None


VERSION = "2.0.0"

LOG = logging.getLogger("partdb_sync")

# Maximum length stored for names (protects against runaway HTML comments).
MAX_NAME_LEN = 200

# Matches any HTML/XML tag, e.g. <br/>, <span style="...">, </span>, <p>.
_TAG_RE = re.compile(r"<[^<>]*>")
_BR_RE = re.compile(r"<br\s*/?>", flags=re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_HSPACE_RE = re.compile(r"[ \t\r\f\v]+")

# PROD_PROJECT=<name>  (single line; terminated by a PROD_STAGE=
# assignment or end of line, so trailing notes are never swallowed)
_PROJECT_RE = re.compile(
    r"PROD_PROJECT\s*=\s*(.*?)(?=\s*PROD_STAGE\s*=|$)",
    flags=re.IGNORECASE | re.MULTILINE,
)
# PROD_STAGE=<name> (rest of the line)
_STAGE_RE = re.compile(
    r"PROD_STAGE\s*=\s*(.*?)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


# ============================================================
# CANONICAL PRODUCTION SCHEMA (kept in sync with
# production_schema.sql - see tests/test_schema_parity.py)
# ============================================================

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS production_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name TEXT NOT NULL,
        part_id INTEGER,
        start_date TEXT NOT NULL,
        target_quantity INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'planned',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        current_stage_id INTEGER REFERENCES production_stages(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_stages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL
            REFERENCES production_projects(id),
        stage_name TEXT NOT NULL,
        sequence_order INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_production (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL
            REFERENCES production_projects(id),
        stage_id INTEGER NOT NULL
            REFERENCES production_stages(id),
        production_date TEXT NOT NULL,
        quantity_produced INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL
            REFERENCES production_projects(id),
        stage_id INTEGER NOT NULL
            REFERENCES production_stages(id),
        test_date TEXT NOT NULL,
        quantity_tested INTEGER NOT NULL DEFAULT 0,
        quantity_passed INTEGER NOT NULL DEFAULT 0,
        quantity_failed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rework_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL
            REFERENCES production_projects(id),
        stage_id INTEGER NOT NULL
            REFERENCES production_stages(id),
        rework_date TEXT NOT NULL,
        quantity_reworked INTEGER NOT NULL DEFAULT 0,
        reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS part_stock_snapshot (
        part_id INTEGER PRIMARY KEY,
        last_known_quantity REAL NOT NULL DEFAULT 0,
        last_checked TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_part_mapping (
        part_id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL
            REFERENCES production_projects(id),
        stage_id INTEGER NOT NULL
            REFERENCES production_stages(id),
        project_name TEXT NOT NULL,
        stage_name TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_daily_production_project_stage_date
    ON daily_production(project_id, stage_id, production_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_production_stages_project_order
    ON production_stages(project_id, sequence_order)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_part_stock_snapshot_checked
    ON part_stock_snapshot(last_checked)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quality_tests_project_stage_date
    ON quality_tests(project_id, stage_id, test_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rework_log_project_stage_date
    ON rework_log(project_id, stage_id, rework_date)
    """,
]


# ============================================================
# COMMENT PARSING
# ============================================================

def strip_html(text):
    """Remove HTML tags/entities from Part-DB rich-text comments.

    Line breaks are preserved (<br> becomes a newline) so tags only ever
    consume up to the end of their own line.
    """
    if text is None:
        return ""
    cleaned = _BR_RE.sub("\n", str(text))
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    # Part-DB escapes underscores in markdown-ish comments: PROD\_PROJECT
    cleaned = cleaned.replace("\\_", "_").replace("\\\\_", "_")
    cleaned = cleaned.replace("\xa0", " ")  # non-breaking space
    lines = [_HSPACE_RE.sub(" ", line).strip()
             for line in cleaned.split("\n")]
    return "\n".join(line for line in lines if line)


def _clean_name(value):
    """Normalise a parsed project/stage name; '' if unusable."""
    if value is None:
        return ""
    name = _WS_RE.sub(" ", strip_html(value)).strip()
    # A name that is only PROD_* keys with empty values is unusable.
    if not name:
        return ""
    return name[:MAX_NAME_LEN].strip()


def parse_comment(comment):
    """Parse a Part-DB comment into (project_name, stage_name|None).

    Returns (None, None) when the comment carries no PROD_PROJECT tag.
    HTML markup, entities and irregular whitespace are stripped so the
    dashboard never shows garbage such as '</span> <span style=...>'.
    """
    if comment is None:
        return None, None

    text = strip_html(comment)
    if not text:
        return None, None

    project_match = _PROJECT_RE.search(text)
    if not project_match:
        return None, None

    project_name = _clean_name(project_match.group(1))
    if not project_name:
        return None, None

    stage_name = None
    stage_match = _STAGE_RE.search(text)
    if stage_match:
        candidate = _clean_name(stage_match.group(1))
        if candidate:
            stage_name = candidate

    return project_name, stage_name


# ============================================================
# SMALL HELPERS
# ============================================================

def round_half_away(value):
    """Round to int, halves away from zero (deterministic for stock)."""
    if value >= 0:
        return int(math.floor(float(value) + 0.5))
    return int(math.ceil(float(value) - 0.5))


def table_columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1]: row for row in cur.fetchall()}


def ensure_schema(cur):
    """Create missing tables/indexes and repair legacy columns.

    Safe to run on every sync: everything is IF NOT EXISTS / guarded.
    """
    for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)

    # --- Repair very old databases ----------------------------------
    cols = table_columns(cur, "production_projects")
    if "target_quantity" not in cols:
        cur.execute(
            "ALTER TABLE production_projects "
            "ADD COLUMN target_quantity INTEGER NOT NULL DEFAULT 0"
        )
    if "status" not in cols:
        cur.execute(
            "ALTER TABLE production_projects "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'planned'"
        )
    if "current_stage_id" not in cols:
        cur.execute(
            "ALTER TABLE production_projects "
            "ADD COLUMN current_stage_id INTEGER "
            "REFERENCES production_stages(id)"
        )
    # NOTE: very old DBs may declare part_stock_snapshot.last_known_quantity
    # with INTEGER affinity. SQLite only converts REAL->INTEGER when it is
    # lossless, so fractional stock levels still survive; no rebuild needed.


# ============================================================
# GET OR CREATE PROJECT / STAGE
# ============================================================

def get_or_create_project(cur, project_name, part_id, default_target=0):
    """Return the project id, creating the row when needed.

    Lookup order: exact match, then case-insensitive match (reuses the
    existing row instead of creating 'Heatsink' vs 'HEATSINK' duplicates),
    then insert WITH target_quantity/status so the NOT NULL constraint
    can never fail (the v1 bug that broke ~89% of sync runs).
    """
    cur.execute(
        "SELECT id, project_name FROM production_projects "
        "WHERE project_name = ? LIMIT 1",
        (project_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "SELECT id, project_name FROM production_projects "
        "WHERE project_name = ? COLLATE NOCASE LIMIT 1",
        (project_name,),
    )
    row = cur.fetchone()
    if row:
        LOG.info(
            "[PROJECT REUSE] '%s' matched existing project '%s' (ID=%s)",
            project_name, row[1], row[0],
        )
        return row[0]

    cur.execute(
        """
        INSERT INTO production_projects
            (project_name, part_id, start_date, target_quantity, status)
        VALUES (?, ?, ?, ?, 'in_progress')
        """,
        (project_name, int(part_id), date.today().isoformat(),
         int(default_target)),
    )
    LOG.info(
        "[PROJECT CREATE] '%s' (ID=%s, target=%s)",
        project_name, cur.lastrowid, int(default_target),
    )
    return cur.lastrowid


def get_or_create_stage(cur, project_id, stage_name):
    """Return the stage id within a project, creating when needed."""
    cur.execute(
        "SELECT id, stage_name FROM production_stages "
        "WHERE project_id = ? AND stage_name = ? LIMIT 1",
        (project_id, stage_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "SELECT id, stage_name FROM production_stages "
        "WHERE project_id = ? AND stage_name = ? COLLATE NOCASE LIMIT 1",
        (project_id, stage_name),
    )
    row = cur.fetchone()
    if row:
        LOG.info(
            "[STAGE REUSE] '%s' matched existing stage '%s' (ID=%s)",
            stage_name, row[1], row[0],
        )
        return row[0]

    cur.execute(
        "SELECT COALESCE(MAX(sequence_order), 0) + 1 "
        "FROM production_stages WHERE project_id = ?",
        (project_id,),
    )
    sequence_order = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO production_stages "
        "(project_id, stage_name, sequence_order) VALUES (?, ?, ?)",
        (project_id, stage_name, sequence_order),
    )
    LOG.info(
        "[STAGE CREATE] '%s' (ID=%s, project=%s, order=%s)",
        stage_name, cur.lastrowid, project_id, sequence_order,
    )
    return cur.lastrowid


def get_fallback_stage(cur, project_id):
    """Stage to use when a comment carries no PROD_STAGE tag.

    Prefers the project's recorded current stage, else the most advanced
    (highest sequence_order) stage, else None.
    """
    cur.execute(
        "SELECT current_stage_id FROM production_projects WHERE id = ?",
        (project_id,),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return row[0]
    cur.execute(
        "SELECT id FROM production_stages WHERE project_id = ? "
        "ORDER BY sequence_order DESC, id DESC LIMIT 1",
        (project_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_stage_name(cur, stage_id):
    cur.execute(
        "SELECT stage_name FROM production_stages WHERE id = ?",
        (stage_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def refresh_current_stages(cur, project_ids):
    """Point each touched project's current_stage_id at its most advanced
    stage. Done once at the end so multi-part projects don't flap between
    stages depending on processing order (v1 bug)."""
    for project_id in sorted(project_ids):
        cur.execute(
            "SELECT id FROM production_stages WHERE project_id = ? "
            "ORDER BY sequence_order DESC, id DESC LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE production_projects SET current_stage_id = ? "
                "WHERE id = ?",
                (row[0], project_id),
            )


# ============================================================
# SNAPSHOT / MAPPING / DELTA WRITES
# ============================================================

def get_previous_quantity(cur, part_id):
    cur.execute(
        "SELECT last_known_quantity FROM part_stock_snapshot WHERE part_id = ?",
        (int(part_id),),
    )
    row = cur.fetchone()
    return None if row is None else float(row[0])


def update_snapshot(cur, part_id, quantity):
    cur.execute(
        """
        INSERT INTO part_stock_snapshot
            (part_id, last_known_quantity, last_checked)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(part_id) DO UPDATE SET
            last_known_quantity = excluded.last_known_quantity,
            last_checked = CURRENT_TIMESTAMP
        """,
        (int(part_id), float(quantity)),
    )


def update_part_mapping(cur, part_id, project_id, stage_id,
                        project_name, stage_name):
    """Upsert the part->project/stage mapping. Never stores NULL names."""
    if not project_name or not stage_name:
        raise ValueError(
            f"refusing to store mapping with empty names "
            f"(part={part_id}, project={project_name!r}, "
            f"stage={stage_name!r})"
        )
    cur.execute(
        """
        INSERT INTO production_part_mapping
            (part_id, project_id, stage_id, project_name, stage_name)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(part_id) DO UPDATE SET
            project_id = excluded.project_id,
            stage_id = excluded.stage_id,
            project_name = excluded.project_name,
            stage_name = excluded.stage_name
        """,
        (int(part_id), project_id, stage_id, project_name, stage_name),
    )


def insert_production_delta(cur, project_id, stage_id, delta):
    cur.execute(
        """
        INSERT INTO daily_production
            (project_id, stage_id, production_date, quantity_produced)
        VALUES (?, ?, ?, ?)
        """,
        (project_id, stage_id, date.today().isoformat(), int(delta)),
    )


# ============================================================
# PART-DB READS
# ============================================================

def read_tracked_parts(partdb_cur):
    """Return [(part_id, project_name, stage_name|None)] for PROD_* parts."""
    tables = {
        r[0] for r in
        partdb_cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "parts" not in tables:
        raise RuntimeError(
            "Part-DB database has no 'parts' table - wrong file?"
        )
    if "part_lots" not in tables:
        raise RuntimeError(
            "Part-DB database has no 'part_lots' table - wrong file?"
        )

    partdb_cur.execute(
        "SELECT id, comment FROM parts "
        "WHERE comment IS NOT NULL AND TRIM(comment) != ''"
    )
    all_parts = partdb_cur.fetchall()
    LOG.info("Parts with comments found: %d", len(all_parts))

    tracked = []
    for part_id, comment in all_parts:
        project_name, stage_name = parse_comment(comment)
        if project_name:
            tracked.append((int(part_id), project_name, stage_name))
            LOG.info(
                "[TRACKED PART] ID=%s | Project='%s' | Stage='%s'",
                part_id, project_name, stage_name,
            )
    LOG.info("Production tracked parts found: %d", len(tracked))
    return tracked


def read_current_stock(partdb_cur):
    """Return {part_id: stock_float} summed across all lots."""
    cols = {r[1] for r in
            partdb_cur.execute("PRAGMA table_info(part_lots)").fetchall()}
    if "id_part" not in cols or "amount" not in cols:
        raise RuntimeError(
            "part_lots table must have id_part/amount columns; "
            f"found: {sorted(cols)}"
        )
    partdb_cur.execute(
        "SELECT id_part, COALESCE(SUM(amount), 0) "
        "FROM part_lots GROUP BY id_part"
    )
    return {int(pid): float(qty or 0.0)
            for pid, qty in partdb_cur.fetchall()}


# ============================================================
# PER-PART PROCESSING
# ============================================================

class PartStats:
    def __init__(self):
        self.mappings = 0
        self.additions = 0
        self.added_units = 0
        self.subtractions = 0
        self.subtracted_units = 0
        self.unchanged = 0
        self.baselines = 0
        self.skipped_no_stage = 0
        self.errors = []


def ensure_topology(proddb_cur, tracked_parts, default_target, stats):
    """Phase 1: make sure every named project/stage exists BEFORE any
    deltas are computed, so parts processed early in the loop can already
    fall back to stages declared by parts processed later. Returns the set
    of part ids that failed (skipped in phase 2)."""
    failed = set()
    for part_id, project_name, stage_name in tracked_parts:
        proddb_cur.execute("SAVEPOINT topo_sync")
        try:
            project_id = get_or_create_project(
                proddb_cur, project_name, part_id, default_target)
            if stage_name:
                get_or_create_stage(proddb_cur, project_id, stage_name)
            proddb_cur.execute("RELEASE topo_sync")
        except Exception as exc:
            proddb_cur.execute("ROLLBACK TO topo_sync")
            proddb_cur.execute("RELEASE topo_sync")
            stats.errors.append((part_id, f"topology: {exc}"))
            LOG.error("[PART ERROR] Part=%s failed: %s (other parts continue)",
                      part_id, exc)
            failed.add(part_id)
    return failed


def process_part(proddb_cur, part_id, project_name, stage_name,
                 current_stock, default_target, stats, touched_projects):
    """Sync one part inside a SAVEPOINT so a bad part cannot wipe out the
    good parts processed in the same run (v1 rolled back everything)."""
    proddb_cur.execute("SAVEPOINT part_sync")
    try:
        project_id = get_or_create_project(
            proddb_cur, project_name, part_id, default_target
        )
        touched_projects.add(project_id)

        if stage_name:
            stage_id = get_or_create_stage(
                proddb_cur, project_id, stage_name)
            resolved_stage = stage_name
        else:
            stage_id = get_fallback_stage(proddb_cur, project_id)
            resolved_stage = (
                get_stage_name(proddb_cur, stage_id)
                if stage_id is not None else None
            )

        new_quantity = float(current_stock.get(part_id, 0.0))
        previous_quantity = get_previous_quantity(proddb_cur, part_id)

        # --- First sight of this part: baseline, no delta logged ------
        if previous_quantity is None:
            if stage_id is not None and resolved_stage:
                update_part_mapping(
                    proddb_cur, part_id, project_id, stage_id,
                    project_name, resolved_stage,
                )
                stats.mappings += 1
                LOG.info(
                    "[MAPPING] Part=%s | Project=%s (ID=%s) | "
                    "Stage=%s (ID=%s)",
                    part_id, project_name, project_id,
                    resolved_stage, stage_id,
                )
            update_snapshot(proddb_cur, part_id, new_quantity)
            stats.baselines += 1
            LOG.info("[BASELINE] Part=%s | Stock=%s",
                     part_id, _fmt_qty(new_quantity))
            proddb_cur.execute("RELEASE part_sync")
            return

        delta_float = new_quantity - previous_quantity
        delta = round_half_away(delta_float)

        # --- No (material) change --------------------------------------
        if delta == 0:
            if stage_id is not None and resolved_stage:
                update_part_mapping(
                    proddb_cur, part_id, project_id, stage_id,
                    project_name, resolved_stage,
                )
                stats.mappings += 1
            update_snapshot(proddb_cur, part_id, new_quantity)
            stats.unchanged += 1
            if delta_float != 0:
                LOG.info(
                    "[NO CHANGE] Part=%s | Stock=%s (sub-unit drift %+.2f "
                    "absorbed, snapshot updated)",
                    part_id, _fmt_qty(new_quantity), delta_float,
                )
            else:
                LOG.info("[NO CHANGE] Part=%s | Stock=%s",
                         part_id, _fmt_qty(new_quantity))
            proddb_cur.execute("RELEASE part_sync")
            return

        # --- Real delta but nowhere to book it --------------------------
        if stage_id is None or not resolved_stage:
            stats.skipped_no_stage += 1
            LOG.warning(
                "[WARNING] Part=%s has no stage. Delta=%+d NOT logged. "
                "Add PROD_STAGE to its comment (or a stage to project "
                "'%s'); the delta will be retried on the next sync.",
                part_id, delta, project_name,
            )
            # NOTE: snapshot deliberately NOT updated so the delta is
            # retried next run.
            proddb_cur.execute("RELEASE part_sync")
            return

        # --- Book the delta ---------------------------------------------
        update_part_mapping(
            proddb_cur, part_id, project_id, stage_id,
            project_name, resolved_stage,
        )
        stats.mappings += 1
        insert_production_delta(proddb_cur, project_id, stage_id, delta)
        update_snapshot(proddb_cur, part_id, new_quantity)

        if delta > 0:
            stats.additions += 1
            stats.added_units += delta
            LOG.info(
                "[ADDITION] Part=%s | %s -> %s | +%d | Stage=%s",
                part_id, _fmt_qty(previous_quantity),
                _fmt_qty(new_quantity), delta, resolved_stage,
            )
        else:
            stats.subtractions += 1
            stats.subtracted_units += -delta
            LOG.info(
                "[SUBTRACTION] Part=%s | %s -> %s | %d | Stage=%s",
                part_id, _fmt_qty(previous_quantity),
                _fmt_qty(new_quantity), delta, resolved_stage,
            )
        proddb_cur.execute("RELEASE part_sync")
    except Exception as exc:  # per-part isolation: log, roll back part only
        proddb_cur.execute("ROLLBACK TO part_sync")
        proddb_cur.execute("RELEASE part_sync")
        stats.errors.append((part_id, str(exc)))
        LOG.error("[PART ERROR] Part=%s failed: %s (other parts continue)",
                  part_id, exc)


def _fmt_qty(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}"


# ============================================================
# CONNECTIONS / LOCKING / LOGGING
# ============================================================

def connect_partdb(path):
    if not Path(path).exists():
        raise RuntimeError(f"Part-DB file not found: {path}")
    # Read-only: the sync must never write to Part-DB.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def connect_production(path, use_wal=True):
    exists = Path(path).exists()
    con = sqlite3.connect(str(path), timeout=30.0)
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA foreign_keys = ON")
    if use_wal:
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.OperationalError as exc:
            LOG.warning("Could not enable WAL mode (%s); continuing.", exc)
    if not exists:
        LOG.info("Production database did not exist; created: %s", path)
    return con


class SyncLock:
    """Inter-process lock so overlapping cron runs can't corrupt a sync."""

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        self.handle = None

    def acquire(self):
        if fcntl is None:  # pragma: no cover
            LOG.warning("fcntl unavailable; running without a lock file.")
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.lock_path, "w")
        try:
            fcntl.flock(self.handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self.handle.close()
            self.handle = None
            return False
        self.handle.write(str(__import__("os").getpid()))
        self.handle.flush()
        return True

    def release(self):
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def setup_logging(verbose, log_file):
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(logging.DEBUG if verbose else logging.INFO)
    stdout.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(stdout)
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s"))
        LOG.addHandler(fh)
    LOG.propagate = False


# ============================================================
# MAIN
# ============================================================

def build_parser():
    parser = argparse.ArgumentParser(
        description="Sync Part-DB stock levels into the production database "
                    f"(v{VERSION}).",
    )
    parser.add_argument("partdb", help="Path to Part-DB SQLite file")
    parser.add_argument("production", help="Path to production SQLite file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute everything but roll back at the end")
    parser.add_argument("--verbose", action="store_true",
                        help="Debug-level logging")
    parser.add_argument("--log-file", default=None,
                        help="Also append timestamped logs to this file")
    parser.add_argument("--lock-file", default=None,
                        help="Lock file path (default: <production>.lock)")
    parser.add_argument("--no-wal", action="store_true",
                        help="Do not enable WAL mode on the production DB")
    parser.add_argument("--default-target", type=int, default=0,
                        help="target_quantity for newly created projects "
                             "(default: 0; set real targets in the DB)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {VERSION}")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)

    lock_path = args.lock_file or (str(args.production) + ".lock")
    lock = SyncLock(lock_path)
    if not lock.acquire():
        print(f"[LOCKED] Another sync is running ({lock_path}). Exiting.",
              file=sys.stderr)
        return 3

    LOG.info("")
    LOG.info("=" * 70)
    LOG.info("PART-DB -> PRODUCTION SYNC v%s", VERSION)
    LOG.info("=" * 70)
    if args.dry_run:
        LOG.info("[DRY RUN] No changes will be committed.")
    LOG.info("")

    partdb = None
    proddb = None
    try:
        partdb = connect_partdb(args.partdb)
        proddb = connect_production(args.production,
                                    use_wal=not args.no_wal)
        partdb_cur = partdb.cursor()
        proddb_cur = proddb.cursor()

        ensure_schema(proddb_cur)

        tracked_parts = read_tracked_parts(partdb_cur)
        LOG.info("")
        current_stock = read_current_stock(partdb_cur)

        stats = PartStats()
        touched_projects = set()

        # One explicit outer transaction for the whole data sync.
        # (Required: in autocommit mode RELEASE of the outermost
        # SAVEPOINT would commit, which would defeat both --dry-run
        # and the fatal-error rollback below. Schema DDL above stays
        # auto-committed on purpose so healing persists.)
        proddb_cur.execute("BEGIN")

        failed_topology = ensure_topology(
            proddb_cur, tracked_parts, args.default_target, stats)

        for part_id, project_name, stage_name in tracked_parts:
            if part_id in failed_topology:
                continue
            process_part(
                proddb_cur, part_id, project_name, stage_name,
                current_stock, args.default_target, stats,
                touched_projects,
            )

        refresh_current_stages(proddb_cur, touched_projects)

        if args.dry_run:
            proddb.rollback()
            LOG.info("")
            LOG.info("[DRY RUN] Rolled back; no changes saved.")
        else:
            proddb.commit()

        LOG.info("")
        LOG.info("=" * 70)
        LOG.info("SYNC COMPLETE")
        LOG.info("=" * 70)
        LOG.info("Tracked parts:       %d", len(tracked_parts))
        LOG.info("Mappings:            %d", stats.mappings)
        LOG.info("Additions:           %d (+%d units)",
                 stats.additions, stats.added_units)
        LOG.info("Subtractions:        %d (-%d units)",
                 stats.subtractions, stats.subtracted_units)
        LOG.info("No changes:          %d", stats.unchanged)
        LOG.info("New baselines:       %d", stats.baselines)
        LOG.info("Skipped (no stage):  %d", stats.skipped_no_stage)
        LOG.info("Part errors:         %d", len(stats.errors))
        for part_id, err in stats.errors:
            LOG.info("  - Part %s: %s", part_id, err)
        LOG.info("=" * 70)

        if stats.errors or stats.skipped_no_stage:
            LOG.warning("Sync finished with warnings (see above).")
            return 2 if stats.errors else 0
        return 0

    except Exception as error:
        if proddb is not None:
            proddb.rollback()
        LOG.error("")
        LOG.error("=" * 70)
        LOG.error("[FATAL ERROR]")
        LOG.error("=" * 70)
        LOG.error("%s", error, exc_info=LOG.isEnabledFor(logging.DEBUG))
        LOG.error("=" * 70)
        LOG.error("No production data was committed "
                  "(schema healing, if any, persists).")
        return 1
    finally:
        if partdb is not None:
            partdb.close()
        if proddb is not None:
            proddb.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
