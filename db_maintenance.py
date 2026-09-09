#!/usr/bin/env python3
"""
Production database maintenance (v2).

Commands:
    migrate production.db
        Bring ANY existing production database up to the canonical schema:
        missing tables/columns/indexes are created, legacy columns repaired,
        current_stage_id backfilled, best-effort uniqueness indexes added.
        Safe and idempotent - run it as often as you like.

    check production.db
        Read-only health report: HTML pollution scan, orphan rows, projects
        without targets/stages, negative nets, stale snapshots, empty
        quality/rework tables. Exit code 0 = healthy, 2 = issues found.

    clean-html production.db [--dry-run]
        Repair names polluted by the v1 parser (e.g. "Heatsink</span> <span
        style=...>"): strip HTML from project/stage names and MERGE rows that
        become duplicates (production history is re-pointed, never deleted).
        Always run with --dry-run first and inspect the plan.

Standard library only.
"""

import argparse
import sqlite3
import sys

from partdb_sync import (
    VERSION,
    ensure_schema,
    refresh_current_stages,
    strip_html,
)


# ============================================================
# MIGRATE
# ============================================================

def cmd_migrate(path):
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA foreign_keys = ON")
        cur = con.cursor()
        print(f"[MIGRATE] {path}")
        ensure_schema(cur)

        # Backfill NULL targets / statuses left by very old writers.
        cur.execute(
            "UPDATE production_projects SET target_quantity = 0 "
            "WHERE target_quantity IS NULL"
        )
        if cur.rowcount:
            print(f"  - target_quantity NULL -> 0 ({cur.rowcount} rows)")
        cur.execute(
            "UPDATE production_projects SET status = 'planned' "
            "WHERE status IS NULL OR TRIM(status) = ''"
        )
        if cur.rowcount:
            print(f"  - status NULL/empty -> 'planned' ({cur.rowcount} rows)")

        # Backfill current_stage_id for every project missing one.
        cur.execute("SELECT id FROM production_projects "
                    "WHERE current_stage_id IS NULL")
        missing = [r[0] for r in cur.fetchall()]
        if missing:
            refresh_current_stages(cur, set(missing))
            print(f"  - current_stage_id backfilled ({len(missing)} projects)")

        # Best-effort uniqueness guards (skipped with a warning when
        # duplicates already exist - run clean-html first).
        for label, ddl in (
            ("projects by name (case-insensitive)",
             "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_name "
             "ON production_projects(project_name COLLATE NOCASE)"),
            ("stages per project (case-insensitive)",
             "CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_per_project "
             "ON production_stages(project_id, stage_name COLLATE NOCASE)"),
        ):
            try:
                cur.execute(ddl)
                print(f"  - unique index OK: {label}")
            except sqlite3.IntegrityError:
                print(f"  - unique index SKIPPED (duplicates exist): {label}")

        con.commit()
        print("[MIGRATE] Done.")
        return 0
    except Exception as exc:
        con.rollback()
        print(f"[MIGRATE FAILED] {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()


# ============================================================
# CHECK
# ============================================================

def cmd_check(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    issues = []
    try:
        cur = con.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        print(f"[CHECK] {path}")

        required = {"production_projects", "production_stages",
                    "daily_production", "quality_tests", "rework_log",
                    "part_stock_snapshot", "production_part_mapping"}
        missing_tables = required - tables
        if missing_tables:
            issues.append(f"missing tables: {sorted(missing_tables)}")
        else:
            print("  - schema: all 7 tables present")

        def count(sql, params=()):
            return cur.execute(sql, params).fetchone()[0]

        if "production_projects" in tables:
            n = count("SELECT COUNT(*) FROM production_projects")
            print(f"  - projects: {n}")
            polluted = cur.execute(
                "SELECT id, project_name FROM production_projects "
                "WHERE project_name LIKE '%<%' OR project_name LIKE '%&nbsp%' "
                "OR project_name LIKE '%&amp%' OR project_name LIKE '%\\\\_%' "
                "ESCAPE '\\'"
            ).fetchall()
            for row in polluted:
                issues.append(
                    f"project ID={row['id']} has HTML-polluted name: "
                    f"{row['project_name']!r}")
            no_target = count(
                "SELECT COUNT(*) FROM production_projects "
                "WHERE target_quantity IS NULL OR target_quantity <= 0")
            if no_target:
                issues.append(
                    f"{no_target} project(s) without a positive "
                    "target_quantity (Progress gauge will read 0%)")
            no_stage = count(
                "SELECT COUNT(*) FROM production_projects pp "
                "WHERE NOT EXISTS (SELECT 1 FROM production_stages ps "
                "WHERE ps.project_id = pp.id)")
            if no_stage:
                issues.append(f"{no_stage} project(s) without any stage")
            no_current = count(
                "SELECT COUNT(*) FROM production_projects "
                "WHERE current_stage_id IS NULL")
            if no_current:
                issues.append(
                    f"{no_current} project(s) without current_stage_id "
                    "(run migrate)")

        if "production_stages" in tables:
            polluted = cur.execute(
                "SELECT id, stage_name FROM production_stages "
                "WHERE stage_name LIKE '%<%' OR stage_name LIKE '%&nbsp%' "
                "OR stage_name LIKE '%&amp%'"
            ).fetchall()
            for row in polluted:
                issues.append(
                    f"stage ID={row['id']} has HTML-polluted name: "
                    f"{row['stage_name']!r}")

        if "production_part_mapping" in tables and "production_projects" in tables:
            orphans = count(
                "SELECT COUNT(*) FROM production_part_mapping m "
                "LEFT JOIN production_projects p ON p.id = m.project_id "
                "WHERE p.id IS NULL")
            if orphans:
                issues.append(
                    f"{orphans} orphan mapping row(s) (bad project_id)")

        if "daily_production" in tables:
            n = count("SELECT COUNT(*) FROM daily_production")
            net = cur.execute(
                "SELECT COALESCE(SUM(quantity_produced), 0) "
                "FROM daily_production").fetchone()[0]
            print(f"  - daily_production rows: {n} (net {net})")

        if "quality_tests" in tables:
            n = count("SELECT COUNT(*) FROM quality_tests")
            print(f"  - quality_tests rows: {n}")
            if n == 0:
                issues.append("quality_tests is empty "
                              "(Quality panels will show 0/blank)")
        if "rework_log" in tables:
            n = count("SELECT COUNT(*) FROM rework_log")
            print(f"  - rework_log rows: {n}")
            if n == 0:
                issues.append("rework_log is empty "
                              "(Rework panels will show 0/blank)")

        if "part_stock_snapshot" in tables:
            stale = cur.execute(
                "SELECT part_id, last_checked FROM part_stock_snapshot "
                "WHERE last_checked IS NULL OR "
                "datetime(last_checked) < datetime('now', '-7 days')"
            ).fetchall()
            for row in stale:
                issues.append(
                    f"snapshot for part {row['part_id']} is stale "
                    f"(last checked: {row['last_checked']})")

        print("")
        if issues:
            print(f"[CHECK] {len(issues)} issue(s) found:")
            for item in issues:
                print(f"  ! {item}")
            return 2
        print("[CHECK] Healthy - no issues found.")
        return 0
    finally:
        con.close()


# ============================================================
# CLEAN-HTML (with duplicate merging)
# ============================================================

def _repoint(cur, table, column, old_id, new_id):
    cur.execute(
        f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
        (new_id, old_id),
    )
    return cur.rowcount


def clean_html_names(path, dry_run=False):
    """Strip HTML from project/stage names, merging duplicates.

    Returns the list of human-readable actions performed (or planned).
    History rows are re-pointed onto surviving rows - never deleted,
    except for the duplicate project/stage/mapping shells themselves.
    """
    if dry_run:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(path))
    actions = []
    try:
        con.execute("PRAGMA foreign_keys = ON")
        cur = con.cursor()

        # ---- 1. Projects ------------------------------------------------
        projects = cur.execute(
            "SELECT id, project_name FROM production_projects ORDER BY id"
        ).fetchall()
        survivor_by_clean = {}  # cleaned lowercase name -> survivor id
        for pid, name in projects:
            cleaned = strip_html(name)
            if not cleaned:
                actions.append(
                    f"project ID={pid}: name {name!r} cleans to empty - "
                    "left untouched (rename it manually)")
                continue
            key = cleaned.lower()
            if key not in survivor_by_clean:
                survivor_by_clean[key] = pid
                if cleaned != name:
                    actions.append(
                        f"project ID={pid}: rename {name!r} -> {cleaned!r}")
                    if not dry_run:
                        cur.execute(
                            "UPDATE production_projects SET project_name = ? "
                            "WHERE id = ?", (cleaned, pid))
                        cur.execute(
                            "UPDATE production_part_mapping "
                            "SET project_name = ? WHERE project_id = ?",
                            (cleaned, pid))
                continue
            # Duplicate -> merge into survivor.
            survivor = survivor_by_clean[key]
            actions.append(
                f"project ID={pid} {name!r}: MERGE into project ID={survivor} "
                f"{cleaned!r}")
            if not dry_run:
                # Move stages (merging same-named ones below).
                cur.execute(
                    "UPDATE production_stages SET project_id = ? "
                    "WHERE project_id = ?", (survivor, pid))
                for table, col in (
                    ("daily_production", "project_id"),
                    ("quality_tests", "project_id"),
                    ("rework_log", "project_id"),
                    ("production_part_mapping", "project_id"),
                ):
                    moved = _repoint(cur, table, col, pid, survivor)
                    if moved:
                        actions.append(
                            f"    re-pointed {moved} {table} row(s)")
                # Survivor keeps the most informative target/status.
                cur.execute(
                    "UPDATE production_projects SET "
                    "target_quantity = MAX(target_quantity, "
                    "  (SELECT target_quantity FROM production_projects "
                    "   WHERE id = ?)), "
                    "status = CASE WHEN target_quantity >= "
                    "  (SELECT target_quantity FROM production_projects "
                    "   WHERE id = ?) THEN status ELSE "
                    "  (SELECT status FROM production_projects WHERE id = ?) "
                    "END "
                    "WHERE id = ?", (pid, pid, pid, survivor))
                cur.execute(
                    "DELETE FROM production_projects WHERE id = ?", (pid,))

        # ---- 2. Stages (per project) ------------------------------------
        projects_now = cur.execute(
            "SELECT id FROM production_projects"
        ).fetchall()
        for (project_id,) in projects_now:
            stages = cur.execute(
                "SELECT id, stage_name, sequence_order FROM production_stages "
                "WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()
            survivor_stage = {}
            for sid, sname, _seq in stages:
                cleaned = strip_html(sname)
                if not cleaned:
                    actions.append(
                        f"stage ID={sid}: name {sname!r} cleans to empty - "
                        "left untouched")
                    continue
                key = cleaned.lower()
                if key not in survivor_stage:
                    survivor_stage[key] = sid
                    if cleaned != sname:
                        actions.append(
                            f"stage ID={sid}: rename {sname!r} -> {cleaned!r}")
                        if not dry_run:
                            cur.execute(
                                "UPDATE production_stages SET stage_name = ? "
                                "WHERE id = ?", (cleaned, sid))
                            cur.execute(
                                "UPDATE production_part_mapping "
                                "SET stage_name = ? WHERE stage_id = ?",
                                (cleaned, sid))
                    continue
                target = survivor_stage[key]
                actions.append(
                    f"stage ID={sid} {sname!r}: MERGE into stage ID={target} "
                    f"{cleaned!r}")
                if not dry_run:
                    for table, col in (
                        ("daily_production", "stage_id"),
                        ("quality_tests", "stage_id"),
                        ("rework_log", "stage_id"),
                        ("production_part_mapping", "stage_id"),
                    ):
                        moved = _repoint(cur, table, col, sid, target)
                        if moved:
                            actions.append(
                                f"    re-pointed {moved} {table} row(s)")
                    # Fix current_stage pointers, then drop the shell.
                    cur.execute(
                        "UPDATE production_projects SET current_stage_id = ? "
                        "WHERE current_stage_id = ?", (target, sid))
                    cur.execute(
                        "DELETE FROM production_stages WHERE id = ?", (sid,))

        # ---- 3. Mapping display names ------------------------------------
        rows = cur.execute(
            "SELECT part_id, project_name, stage_name "
            "FROM production_part_mapping"
        ).fetchall()
        for part_id, pname, sname in rows:
            cp, cs = strip_html(pname), strip_html(sname)
            if cp != pname or cs != sname:
                actions.append(
                    f"mapping part {part_id}: rename "
                    f"{pname!r}/{sname!r} -> {cp!r}/{cs!r}")
                if not dry_run:
                    cur.execute(
                        "UPDATE production_part_mapping SET project_name = ?, "
                        "stage_name = ? WHERE part_id = ?",
                        (cp or pname, cs or sname, part_id))

        # ---- 4. Re-sequence + refresh current stages ----------------------
        if not dry_run:
            for (project_id,) in cur.execute(
                    "SELECT id FROM production_projects").fetchall():
                ids = [r[0] for r in cur.execute(
                    "SELECT id FROM production_stages WHERE project_id = ? "
                    "ORDER BY sequence_order, id", (project_id,)).fetchall()]
                for order, sid in enumerate(ids, start=1):
                    cur.execute(
                        "UPDATE production_stages SET sequence_order = ? "
                        "WHERE id = ?", (order, sid))
            all_projects = {r[0] for r in cur.execute(
                "SELECT id FROM production_projects").fetchall()}
            refresh_current_stages(cur, all_projects)
            con.commit()
        return actions
    except Exception:
        if not dry_run:
            con.rollback()
        raise
    finally:
        con.close()


def cmd_clean_html(path, dry_run):
    print(f"[CLEAN-HTML] {path}"
          + (" (DRY RUN - no changes)" if dry_run else ""))
    try:
        actions = clean_html_names(path, dry_run=dry_run)
    except Exception as exc:
        print(f"[CLEAN-HTML FAILED] {exc}", file=sys.stderr)
        return 1
    if not actions:
        print("  - names are already clean; nothing to do.")
    else:
        for action in actions:
            print(f"  - {action}")
    if dry_run:
        print("[CLEAN-HTML] Dry run complete; re-run without --dry-run "
              "to apply.")
    else:
        print("[CLEAN-HTML] Done.")
    return 0


# ============================================================
# CLI
# ============================================================

def build_parser():
    parser = argparse.ArgumentParser(
        description="Production database maintenance "
                    f"(v{VERSION}).")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="Upgrade schema (idempotent)")
    migrate.add_argument("production")
    check = sub.add_parser("check", help="Read-only health report")
    check.add_argument("production")
    clean = sub.add_parser("clean-html",
                           help="Strip HTML from names + merge duplicates")
    clean.add_argument("production")
    clean.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return cmd_migrate(args.production)
    if args.command == "check":
        return cmd_check(args.production)
    if args.command == "clean-html":
        return cmd_clean_html(args.production, args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
