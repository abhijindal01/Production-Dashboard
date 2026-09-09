"""The sync's embedded DDL (SCHEMA_STATEMENTS) and production_schema.sql
must describe the same tables/indexes - otherwise fresh installs (SQL file)
and healed databases (sync script) would diverge."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partdb_sync import SCHEMA_STATEMENTS  # noqa: E402
from tests.conftest import REPO_ROOT  # noqa: E402


def normalize(statement):
    text = statement.strip().rstrip(";").strip()
    text = re.sub(r"\s+", " ", text).lower()
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text


def test_schema_parity():
    sql_file = (REPO_ROOT / "production_schema.sql").read_text()
    # Drop comment/full-line directives; keep CREATE statements only.
    cleaned_lines = [ln for ln in sql_file.splitlines()
                     if ln.strip() and not ln.strip().startswith("--")
                     and not ln.strip().upper().startswith("PRAGMA")]
    file_statements = {normalize(s) for s in
                       "\n".join(cleaned_lines).split(";")
                       if s.strip() and s.strip().upper().startswith("CREATE")}
    embedded = {normalize(s) for s in SCHEMA_STATEMENTS}
    assert embedded == file_statements
    assert len(embedded) == 12  # 7 tables + 5 indexes
