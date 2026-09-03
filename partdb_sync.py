#!/usr/bin/env python3

import os
import sqlite3
import sys
import re
from datetime import date


# ============================================================
# CONFIG
# ============================================================

DEBUG = False

# Part-DB source database flavours
SOURCE_SQLITE = "sqlite"
SOURCE_MYSQL = "mysql"

# Tables that must exist in the Part-DB source database
REQUIRED_SOURCE_TABLES = ("parts", "part_lots")


# ============================================================
# SOURCE DATABASE HELPERS
# ============================================================

class SourceSchemaError(Exception):
    """Raised when the Part-DB source cannot be read or is the
    wrong database (e.g. empty file / missing parts table)."""


def load_dotenv_lite(path=".env"):
    """
    Very small .env loader (only for the MySQL connection settings).
    Existing environment variables are not overwritten.
    """
    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def env_first(*names, default=None):
    """Return the first set environment variable among names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def open_source_db(source_arg, mysql_mode):
    """
    Open the Part-DB source database.

    SQLite mode : source_arg is the path of a .db file
    MySQL  mode : source_arg is the Part-DB database name
                  (can be empty -> taken from .env / environment)

    Returns (connection, cursor, flavour). The cursor returns
    plain tuples like the SQLite cursor, so both modes share the
    same reading code.
    """
    if mysql_mode:
        return open_source_mysql(source_arg)

    if not os.path.isfile(source_arg):
        raise SourceSchemaError(
            f"Part-DB source file not found: '{source_arg}'\n"
            f"Run from the directory that contains your real "
            f"partdb.db, or use --mysql to read Part-DB directly."
        )

    connection = sqlite3.connect(
        source_arg,
        timeout=30.0
    )
    cursor = connection.cursor()
    return connection, cursor, SOURCE_SQLITE


def open_source_mysql(database):
    """Connect to the Part-DB MySQL/MariaDB database directly."""
    try:
        import pymysql  # noqa: PLC0415
    except ImportError:
        raise SourceSchemaError(
            "MySQL support needs the 'pymysql' package.\n"
            "  sudo apt install python3-pymysql   # Debian/Ubuntu\n"
            "  pip3 install pymysql               # any pip\n"
            "Then re-run the sync with --mysql."
        )

    load_dotenv_lite()

    host = env_first(
        "PARTDB_MYSQL_HOST", "PARTDB_DB_HOST", default="localhost"
    )
    port = int(env_first(
        "PARTDB_MYSQL_PORT", "PARTDB_DB_PORT", default="3306"
    ))
    name = (
        database
        or env_first(
            "PARTDB_MYSQL_DATABASE",
            "PARTDB_DB_NAME",
            "MYSQL_DATABASE",
            default="partdb",
        )
    )
    user = env_first(
        "PARTDB_MYSQL_USER",
        "PARTDB_DB_USER",
        "MYSQL_USER",
        default="partdb",
    )
    password = env_first(
        "PARTDB_MYSQL_PASSWORD",
        "PARTDB_DB_PASSWORD",
        "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        default="",
    )

    print(f"[MYSQL] host={host} port={port} db={name} user={user}")

    try:
        connection = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=name,
            charset="utf8mb4",
        )
    except Exception as error:
        raise SourceSchemaError(
            f"Could not connect to Part-DB MySQL database "
            f"'{name}'@{host}:{port} as '{user}':\n{error}\n\n"
            f"Check the PARTDB_MYSQL_* environment variables "
            f"(or .env in this folder). Hint for the Part-DB "
            f"Docker stack: check 'docker compose exec db env' "
            f"for the MYSQL_* credentials."
        )

    return connection, connection.cursor(), SOURCE_MYSQL


def list_source_tables(cursor, flavour):
    """Return the table names present in the source database."""
    if flavour == SOURCE_MYSQL:
        cursor.execute("SHOW TABLES")
    else:
        cursor.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
            """
        )
    return [str(row[0]) for row in cursor.fetchall()]


def describe_empty_source(source_arg):
    """Build a helpful message for the most common mistake:
    pointing at an empty Part-DB file."""
    return (
        f"Part-DB source '{source_arg}' has NO tables — it is an "
        f"empty SQLite database.\n"
        f"  - Point at the REAL Part-DB database file (the one that "
        f"contains the 'parts' table), not an empty/new .db file.\n"
        f"  - Check for other copies:  ls -la partdb.db* *.db\n"
        f"  - Or read your Part-DB MySQL database directly:  "
        f"python3 partdb_sync.py --mysql <dbname> production.db"
    )


def validate_source_schema(cursor, flavour, source_arg):
    """Ensure the source really is a Part-DB database."""
    tables = list_source_tables(cursor, flavour)

    missing = [
        table for table in REQUIRED_SOURCE_TABLES
        if table not in tables
    ]

    if not missing:
        return tables

    if not tables:
        raise SourceSchemaError(describe_empty_source(source_arg))

    raise SourceSchemaError(
        f"Part-DB source '{source_arg}' is missing tables: "
        f"{', '.join(missing)}.\n"
        f"Tables actually found:\n"
        f"  {', '.join(tables) or '(none)'}\n\n"
        f"Common causes:\n"
        f"  1. You passed production.db by mistake — Part-DB and "
        f"     production data are two DIFFERENT files.\n"
        f"  2. partdb.db is a new/empty file — replace it with the "
        f"     real Part-DB database.\n"
        f"  3. Your Part-DB runs in Docker/MySQL — then use:  "
        f"python3 partdb_sync.py --mysql <dbname> production.db"
    )


# ============================================================
# TEXT CLEANING (Part-DB comments / legacy names)
# ============================================================

HTML_TAG_RE = re.compile(
    r"<[^>]*>"
)


def clean_text(text):
    """
    Strip everything that Part-DB embeds in its comment field:
    HTML tags (spans, style attributes), entities and line breaks.
    Also converts escaped underscores and normalizes whitespace.

    'Drone Soccer Balls with RC</span> <span style="...">' -> 'Drone Soccer Balls with RC'
    """
    if text is None:
        return ""

    text = str(text)

    # Part-DB HTML line breaks
    text = re.sub(
        r"<br\s*/?>",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # HTML entities
    text = re.sub(
        r"&nbsp;",
        " ",
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r"&amp;",
        "&",
        text,
        flags=re.IGNORECASE
    )

    # Remove every remaining HTML tag
    text = HTML_TAG_RE.sub(" ", text)

    # Handle escaped underscores
    text = text.replace(r"\_", "_")
    text = text.replace(r"\\_", "_")

    # Normalize whitespace
    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


# ============================================================
# PARSE PART-DB COMMENT
# ============================================================

def parse_comment(comment):

    if not comment:
        return None, None

    text = clean_text(comment)

    # --------------------------------------------------------
    # PROJECT
    # --------------------------------------------------------

    project_match = re.search(
        r"PROD_PROJECT\s*=\s*(.*?)(?=\s+PROD_STAGE\s*=|$)",
        text,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # STAGE
    # --------------------------------------------------------

    stage_match = re.search(
        r"PROD_STAGE\s*=\s*(.*?)\s*$",
        text,
        flags=re.IGNORECASE
    )

    project_name = None
    stage_name = None

    if project_match:
        project_name = project_match.group(1).strip()

    if stage_match:
        stage_name = stage_match.group(1).strip()

    return project_name, stage_name


# ============================================================
# LEGACY NAME CLEANUP
# ============================================================

def table_exists(cur, table_name):

    cur.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,)
    )

    return cur.fetchone() is not None


def normalize_project_and_stage_names(cur):
    """
    Older syncs stored names with HTML junk from Part-DB comments
    ('Project</span> <span style="...">'). Rename those rows to the
    clean names so the auto-sync matches them instead of creating
    duplicate projects/stages.
    """

    renamed_projects = 0
    renamed_stages = 0

    cur.execute(
        "SELECT id, project_name FROM production_projects"
    )

    for project_id, name in cur.fetchall():

        clean = clean_text(name)

        if clean and clean != name:

            cur.execute(
                "UPDATE production_projects "
                "SET project_name = ? "
                "WHERE id = ?",
                (clean, project_id)
            )

            renamed_projects += 1

    cur.execute(
        "SELECT id, stage_name FROM production_stages"
    )

    for stage_id, name in cur.fetchall():

        clean = clean_text(name)

        if clean and clean != name:

            cur.execute(
                "UPDATE production_stages "
                "SET stage_name = ? "
                "WHERE id = ?",
                (clean, stage_id)
            )

            renamed_stages += 1

    if renamed_projects or renamed_stages:
        print(
            f"[SCHEMA] Cleaned HTML from names: "
            f"{renamed_projects} project(s), "
            f"{renamed_stages} stage(s)"
        )


def deduplicate_projects_and_stages(cur):
    """
    After cleaning, two rows may have identical names (one old
    polluted row, one new clean row). Merge duplicates: keep the
    lowest id, move stages/deltas/mappings to it, delete the rest.
    """

    # Tables that reference project_id / stage_id
    project_tables = [
        "production_stages",
        "daily_production",
        "production_part_mapping",
        "quality_tests",
        "rework_log",
    ]
    stage_tables = [
        "daily_production",
        "quality_tests",
        "rework_log",
    ]

    # ------------------------------------------------ projects
    cur.execute(
        "SELECT id, project_name FROM production_projects ORDER BY id"
    )

    keep_project = {}

    for project_id, name in cur.fetchall():

        key = clean_text(name).lower()

        if key not in keep_project:

            keep_project[key] = project_id
            continue

        target = keep_project[key]

        for table in project_tables:
            if table_exists(cur, table):
                cur.execute(
                    f"UPDATE {table} SET project_id = ? "
                    f"WHERE project_id = ?",
                    (target, project_id)
                )

        cur.execute(
            "DELETE FROM production_projects WHERE id = ?",
            (project_id,)
        )

    # ------------------------------------------------ stages
    cur.execute(
        "SELECT id, project_id, stage_name "
        "FROM production_stages ORDER BY project_id, id"
    )

    keep_stage = {}

    for stage_id, project_id, name in cur.fetchall():

        key = (int(project_id), clean_text(name).lower())

        if key not in keep_stage:

            keep_stage[key] = stage_id
            continue

        target = keep_stage[key]

        for table in stage_tables:
            if table_exists(cur, table):
                cur.execute(
                    f"UPDATE {table} SET stage_id = ? "
                    f"WHERE stage_id = ?",
                    (target, stage_id)
                )

        cur.execute(
            "UPDATE production_projects "
            "SET current_stage_id = ? "
            "WHERE current_stage_id = ?",
            (target, stage_id)
        )

        cur.execute(
            "DELETE FROM production_stages WHERE id = ?",
            (stage_id,)
        )


# ============================================================
# ENSURE GROUP TABLE
# ============================================================

def ensure_group_table(cur):

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS production_part_groups
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            part_name TEXT NOT NULL,
            total_quantity INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (project_id, stage_id, part_name)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_part_groups_name
        ON production_part_groups(part_name)
        """
    )


# ============================================================
# UPSERT PART GROUP
# ============================================================

def upsert_part_group(
    cur,
    project_id,
    stage_id,
    part_name,
    total_quantity
):

    cur.execute(
        """
        INSERT INTO production_part_groups
        (
            project_id,
            stage_id,
            part_name,
            total_quantity,
            last_updated
        )
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)

        ON CONFLICT(project_id, stage_id, part_name)
        DO UPDATE SET
            total_quantity = excluded.total_quantity,
            last_updated = CURRENT_TIMESTAMP
        """,
        (
            int(project_id),
            int(stage_id),
            str(part_name),
            int(total_quantity)
        )
    )


# ============================================================
# GET OR CREATE PROJECT
# ============================================================

def get_or_create_project(cur, project_name, part_id):

    cur.execute(
        """
        SELECT id
        FROM production_projects
        WHERE project_name = ?
        LIMIT 1
        """,
        (project_name,)
    )

    row = cur.fetchone()

    if row:
        return row[0]

    cur.execute(
        """
        INSERT INTO production_projects
        (
            project_name,
            start_date,
            target_quantity
        )
        VALUES (?, ?, 0)
        """,
        (
            project_name,
            date.today().isoformat()
        )
    )

    return cur.lastrowid


# ============================================================
# GET OR CREATE STAGE
# ============================================================

def get_or_create_stage(
    cur,
    project_id,
    stage_name
):

    cur.execute(
        """
        SELECT id
        FROM production_stages
        WHERE project_id = ?
          AND stage_name = ?
        LIMIT 1
        """,
        (
            project_id,
            stage_name
        )
    )

    row = cur.fetchone()

    if row:
        return row[0]

    cur.execute(
        """
        SELECT COALESCE(
            MAX(sequence_order),
            0
        ) + 1
        FROM production_stages
        WHERE project_id = ?
        """,
        (project_id,)
    )

    sequence_order = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO production_stages
        (
            project_id,
            stage_name,
            sequence_order
        )
        VALUES (?, ?, ?)
        """,
        (
            project_id,
            stage_name,
            sequence_order
        )
    )

    return cur.lastrowid


# ============================================================
# GET CURRENT STAGE
# ============================================================

def get_current_stage(cur, project_id):

    cur.execute(
        """
        SELECT current_stage_id
        FROM production_projects
        WHERE id = ?
        """,
        (project_id,)
    )

    row = cur.fetchone()

    if row and row[0] is not None:
        return row[0]

    cur.execute(
        """
        SELECT id
        FROM production_stages
        WHERE project_id = ?
        ORDER BY sequence_order DESC
        LIMIT 1
        """,
        (project_id,)
    )

    row = cur.fetchone()

    if row:
        return row[0]

    return None


# ============================================================
# GET PREVIOUS SNAPSHOT
# ============================================================

def get_previous_quantity(cur, part_id):

    cur.execute(
        """
        SELECT last_known_quantity
        FROM part_stock_snapshot
        WHERE part_id = ?
        """,
        (part_id,)
    )

    row = cur.fetchone()

    if row is None:
        return None

    return int(row[0])


# ============================================================
# UPDATE SNAPSHOT
# ============================================================

def update_snapshot(
    cur,
    part_id,
    quantity
):

    cur.execute(
        """
        INSERT INTO part_stock_snapshot
        (
            part_id,
            last_known_quantity,
            last_checked
        )
        VALUES (?, ?, CURRENT_TIMESTAMP)

        ON CONFLICT(part_id)
        DO UPDATE SET
            last_known_quantity =
                excluded.last_known_quantity,
            last_checked =
                CURRENT_TIMESTAMP
        """,
        (
            int(part_id),
            int(quantity)
        )
    )


# ============================================================
# UPDATE PART MAPPING
# ============================================================

def update_part_mapping(
    cur,
    part_id,
    project_id,
    stage_id,
    project_name,
    stage_name
):

    # --------------------------------------------------------
    # IMPORTANT
    #
    # production_part_mapping contains:
    #
    # part_id
    # project_id
    # stage_id
    # project_name
    # stage_name
    #
    # We update the existing mapping if it exists.
    # Otherwise insert it.
    # --------------------------------------------------------

    cur.execute(
        """
        SELECT part_id
        FROM production_part_mapping
        WHERE part_id = ?
        LIMIT 1
        """,
        (int(part_id),)
    )

    row = cur.fetchone()

    if row:

        cur.execute(
            """
            UPDATE production_part_mapping
            SET
                project_id = ?,
                stage_id = ?,
                project_name = ?,
                stage_name = ?
            WHERE part_id = ?
            """,
            (
                project_id,
                stage_id,
                project_name,
                stage_name,
                int(part_id)
            )
        )

    else:

        cur.execute(
            """
            INSERT INTO production_part_mapping
            (
                part_id,
                project_id,
                stage_id,
                project_name,
                stage_name
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(part_id),
                project_id,
                stage_id,
                project_name,
                stage_name
            )
        )


# ============================================================
# INSERT PRODUCTION DELTA
# ============================================================

def insert_production_delta(
    cur,
    project_id,
    stage_id,
    delta
):

    cur.execute(
        """
        INSERT INTO daily_production
        (
            project_id,
            stage_id,
            production_date,
            quantity_produced
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            project_id,
            stage_id,
            date.today().isoformat(),
            int(delta)
        )
    )


# ============================================================
# MAIN
# ============================================================

def main():

    mysql_mode = "--mysql" in sys.argv
    args = [
        argument
        for argument in sys.argv[1:]
        if argument != "--mysql"
    ]

    if mysql_mode:
        # --mysql [partdb_database_name] production.db
        if len(args) not in (1, 2):
            print(
                "Usage:\n"
                "python3 partdb_sync.py --mysql "
                "<production.db>\n"
                "python3 partdb_sync.py --mysql "
                "<partdb_db_name> <production.db>\n\n"
                "Part-DB MySQL connection is read from PARTDB_MYSQL_* "
                "environment variables or .env:\n"
                "  PARTDB_MYSQL_HOST (default localhost)\n"
                "  PARTDB_MYSQL_PORT (default 3306)\n"
                "  PARTDB_MYSQL_DATABASE\n"
                "  PARTDB_MYSQL_USER / PARTDB_MYSQL_PASSWORD\n"
                "(PARTDB_DB_* and the Docker stack's MYSQL_* names "
                "are also accepted.)"
            )
            sys.exit(1)

        partdb_arg = args[0] if len(args) == 2 else ""
        production_path = args[-1]

    else:
        if len(args) != 2:
            print(
                "Usage:\n"
                "python3 partdb_sync.py "
                "<partdb.db> <production.db>\n"
                "python3 partdb_sync.py --mysql "
                "[partdb_db_name] <production.db>"
            )
            sys.exit(1)

        partdb_arg = args[0]
        production_path = args[1]

    print()
    print("=" * 70)
    print("PART-DB → PRODUCTION SYNC")
    print("=" * 70)
    print()

    partdb = None
    proddb = None

    try:

        # ----------------------------------------------------
        # DATABASE CONNECTIONS
        # ----------------------------------------------------

        partdb, partdb_cur, partdb_flavour = open_source_db(
            partdb_arg,
            mysql_mode
        )

        proddb = sqlite3.connect(
            production_path,
            timeout=30.0
        )

        proddb_cur = proddb.cursor()

        proddb_cur.execute(
            "PRAGMA foreign_keys = ON"
        )

        # ----------------------------------------------------
        # CHECK PART-DB SOURCE SCHEMA
        # ----------------------------------------------------

        tables = validate_source_schema(
            partdb_cur,
            partdb_flavour,
            partdb_arg or "(mysql database)"
        )

        print(
            f"Part-DB source tables: "
            f"{len(tables)} found"
        )

        print()

        # ----------------------------------------------------
        # ENSURE GROUPED PART TABLE EXISTS
        # ----------------------------------------------------

        ensure_group_table(proddb_cur)

        # ----------------------------------------------------
        # CLEAN LEGACY HTML NAMES + MERGE DUPLICATES
        # ----------------------------------------------------

        normalize_project_and_stage_names(proddb_cur)

        deduplicate_projects_and_stages(proddb_cur)

        # ----------------------------------------------------
        # READ PART-DB PARTS
        # ----------------------------------------------------

        partdb_cur.execute(
            """
            SELECT
                id,
                name,
                comment
            FROM parts
            WHERE comment IS NOT NULL
              AND TRIM(comment) != ''
            """
        )

        all_parts = partdb_cur.fetchall()

        print(
            f"Parts with comments found: "
            f"{len(all_parts)}"
        )

        print()

        # ----------------------------------------------------
        # FIND PRODUCTION PARTS
        # ----------------------------------------------------

        tracked_parts = []

        for part_id, part_name, comment in all_parts:

            project_name, stage_name = parse_comment(
                comment
            )

            if project_name:

                tracked_parts.append(
                    (
                        int(part_id),
                        part_name,
                        project_name,
                        stage_name,
                        comment
                    )
                )

                print(
                    f"[TRACKED PART] "
                    f"ID={part_id} | "
                    f"Name='{part_name}' | "
                    f"Project='{project_name}' | "
                    f"Stage='{stage_name}'"
                )

        print()

        print(
            f"Production tracked parts found: "
            f"{len(tracked_parts)}"
        )

        print()

        # ----------------------------------------------------
        # GET CURRENT STOCK FROM PART-DB
        # ----------------------------------------------------

        partdb_cur.execute(
            """
            SELECT
                id_part,
                COALESCE(SUM(amount), 0)
            FROM part_lots
            GROUP BY id_part
            """
        )

        current_stock = {}

        for part_id, quantity in partdb_cur.fetchall():

            current_stock[
                int(part_id)
            ] = int(quantity)

        # ----------------------------------------------------
        # COUNTERS
        # ----------------------------------------------------

        additions = 0
        subtractions = 0
        unchanged = 0
        baselines = 0
        skipped = 0
        mappings = 0

        # ----------------------------------------------------
        # PART-NAME GROUPS
        #
        # key: (project_id, stage_id, part_name)
        # value: current stock quantity
        #
        # Parts with the SAME name in the same project+stage
        # are merged into one group row.
        # ----------------------------------------------------

        part_groups = {}

        # ----------------------------------------------------
        # PROCESS PARTS
        # ----------------------------------------------------

        for (
            part_id,
            part_name,
            project_name,
            stage_name,
            comment
        ) in tracked_parts:

            # ------------------------------------------------
            # PROJECT
            # ------------------------------------------------

            project_id = get_or_create_project(
                proddb_cur,
                project_name,
                part_id
            )

            # ------------------------------------------------
            # STAGE
            # ------------------------------------------------

            if stage_name:

                stage_id = get_or_create_stage(
                    proddb_cur,
                    project_id,
                    stage_name
                )

                # Current stage
                proddb_cur.execute(
                    """
                    UPDATE production_projects
                    SET current_stage_id = ?
                    WHERE id = ?
                    """,
                    (
                        stage_id,
                        project_id
                    )
                )

            else:

                stage_id = get_current_stage(
                    proddb_cur,
                    project_id
                )

            # ------------------------------------------------
            # UPDATE PART MAPPING
            #
            # THIS WAS MISSING BEFORE.
            # ------------------------------------------------

            if stage_id is not None:

                update_part_mapping(
                    proddb_cur,
                    part_id,
                    project_id,
                    stage_id,
                    project_name,
                    stage_name
                )

                mappings += 1

                print(
                    f"[MAPPING] "
                    f"Part={part_id} | "
                    f"Project={project_name} "
                    f"(ID={project_id}) | "
                    f"Stage={stage_name} "
                    f"(ID={stage_id})"
                )

            # ------------------------------------------------
            # CURRENT STOCK
            # ------------------------------------------------

            new_quantity = int(
                current_stock.get(
                    part_id,
                    0
                )
            )

            # ------------------------------------------------
            # ACCUMULATE PART-NAME GROUP
            # ------------------------------------------------

            if stage_id is not None:

                group_name = str(part_name).strip()

                if not group_name:
                    group_name = f"Part {part_id}"

                group_key = (
                    int(project_id),
                    int(stage_id),
                    group_name
                )

                part_groups[group_key] = (
                    part_groups.get(group_key, 0) +
                    new_quantity
                )

            # ------------------------------------------------
            # PREVIOUS SNAPSHOT
            # ------------------------------------------------

            previous_quantity = get_previous_quantity(
                proddb_cur,
                part_id
            )

            # ------------------------------------------------
            # FIRST RUN
            # ------------------------------------------------

            if previous_quantity is None:

                update_snapshot(
                    proddb_cur,
                    part_id,
                    new_quantity
                )

                baselines += 1

                print(
                    f"[BASELINE] "
                    f"Part={part_id} | "
                    f"Stock={new_quantity}"
                )

                continue

            # ------------------------------------------------
            # CALCULATE DELTA
            # ------------------------------------------------

            delta = (
                new_quantity -
                previous_quantity
            )

            # ------------------------------------------------
            # NO CHANGE
            # ------------------------------------------------

            if delta == 0:

                unchanged += 1

                update_snapshot(
                    proddb_cur,
                    part_id,
                    new_quantity
                )

                print(
                    f"[NO CHANGE] "
                    f"Part={part_id} | "
                    f"Stock={new_quantity}"
                )

                continue

            # ------------------------------------------------
            # NO STAGE
            # ------------------------------------------------

            if stage_id is None:

                skipped += 1

                print(
                    f"[WARNING] "
                    f"Part={part_id} has no stage. "
                    f"Delta={delta:+} NOT logged."
                )

                # IMPORTANT:
                # Do NOT update snapshot.
                #
                # The delta will be retried on the
                # next sync after the stage is fixed.

                continue

            # ------------------------------------------------
            # INSERT DELTA FIRST
            # ------------------------------------------------

            insert_production_delta(
                proddb_cur,
                project_id,
                stage_id,
                delta
            )

            # ------------------------------------------------
            # ONLY AFTER INSERT SUCCESS:
            # UPDATE SNAPSHOT
            # ------------------------------------------------

            update_snapshot(
                proddb_cur,
                part_id,
                new_quantity
            )

            # ------------------------------------------------
            # LOG
            # ------------------------------------------------

            if delta > 0:

                additions += 1

                print(
                    f"[ADDITION] "
                    f"Part={part_id} | "
                    f"{previous_quantity} → "
                    f"{new_quantity} | "
                    f"+{delta} | "
                    f"Stage={stage_name}"
                )

            else:

                subtractions += 1

                print(
                    f"[SUBTRACTION] "
                    f"Part={part_id} | "
                    f"{previous_quantity} → "
                    f"{new_quantity} | "
                    f"{delta} | "
                    f"Stage={stage_name}"
                )

        # ----------------------------------------------------
        # WRITE PART-NAME GROUPS
        #
        # The group table is fully derived from the tracked
        # parts on every run, so the dashboard always shows
        # the CURRENT quantity for every group.
        # ----------------------------------------------------

        proddb_cur.execute(
            "DELETE FROM production_part_groups"
        )

        groups = 0

        for (
            project_id,
            stage_id,
            part_name
        ), total_quantity in sorted(
            part_groups.items()
        ):

            upsert_part_group(
                proddb_cur,
                project_id,
                stage_id,
                part_name,
                total_quantity
            )

            groups += 1

            print(
                f"[GROUP] "
                f"'{part_name}' | "
                f"Stage ID={stage_id} | "
                f"Quantity={total_quantity}"
            )

        # ----------------------------------------------------
        # COMMIT
        # ----------------------------------------------------

        proddb.commit()

        print()
        print("=" * 70)
        print("SYNC COMPLETE")
        print("=" * 70)

        print(
            f"Part groups:         {groups}"
        )

        print(
            f"Mappings:            {mappings}"
        )

        print(
            f"Additions:           {additions}"
        )

        print(
            f"Subtractions:        {subtractions}"
        )

        print(
            f"No changes:          {unchanged}"
        )

        print(
            f"New baselines:       {baselines}"
        )

        print(
            f"Skipped:             {skipped}"
        )

        print("=" * 70)

    except SourceSchemaError as error:

        print()
        print("=" * 70)
        print("[FATAL ERROR - SOURCE DATABASE]")
        print("=" * 70)
        print(str(error))
        print("=" * 70)

        sys.exit(1)

    except Exception as error:

        # ----------------------------------------------------
        # ROLLBACK EVERYTHING
        # ----------------------------------------------------

        if proddb:

            proddb.rollback()

        print()
        print("=" * 70)
        print("[FATAL ERROR]")
        print("=" * 70)
        print(error)
        print("=" * 70)

        sys.exit(1)

    finally:

        if partdb:
            partdb.close()

        if proddb:
            proddb.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
