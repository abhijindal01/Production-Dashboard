"""Unit tests for Part-DB comment parsing (partdb_sync.parse_comment)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partdb_sync import parse_comment, strip_html  # noqa: E402


def test_none_and_empty():
    assert parse_comment(None) == (None, None)
    assert parse_comment("") == (None, None)
    assert parse_comment("   ") == (None, None)


def test_no_tags():
    assert parse_comment("just a regular comment") == (None, None)
    assert parse_comment("PROD_STAGE=Lonely") == (None, None)


def test_plain():
    assert parse_comment("PROD_PROJECT=Heatsink PROD_STAGE=Anodised") == (
        "Heatsink", "Anodised")


def test_case_insensitive_keys_and_spacing():
    assert parse_comment("prod_project = Heatsink   prod_stage =  Anodised ") == (
        "Heatsink", "Anodised")


def test_stage_optional():
    assert parse_comment("PROD_PROJECT=Solo") == ("Solo", None)
    assert parse_comment("  PROD_PROJECT=Solo  ") == ("Solo", None)


def test_br_and_nbsp_separators():
    assert parse_comment(
        "PROD_PROJECT=Heatsink<br/>PROD_STAGE=Anodised") == (
        "Heatsink", "Anodised")
    assert parse_comment(
        "PROD_PROJECT=Heatsink<BR>PROD_STAGE=Anodised") == (
        "Heatsink", "Anodised")
    assert parse_comment(
        "PROD_PROJECT=Heatsink&nbsp;PROD_STAGE=Anodised") == (
        "Heatsink", "Anodised")


def test_escaped_underscores():
    assert parse_comment(
        r"PROD\_PROJECT=Heatsink PROD\_STAGE=Anodised") == (
        "Heatsink", "Anodised")


def test_full_html_rich_text_is_stripped():
    # Realistic Part-DB rich-text editor output (the v1 parser leaked this
    # into project/stage names, see sync.log).
    raw = ('<p>PROD_PROJECT=Drone Soccer Balls with RC</p>'
           '<p>PROD_STAGE=Ready to Delivery</p>')
    assert parse_comment(raw) == (
        "Drone Soccer Balls with RC", "Ready to Delivery")

    soup = ('PROD_PROJECT=Drone Soccer Balls with RC</span> '
            '<span style="background-color:rgb(33,37,41);'
            'color:rgb(222,226,230);"> PROD_STAGE=Ready to Delivery</span>')
    assert parse_comment(soup) == (
        "Drone Soccer Balls with RC", "Ready to Delivery")


def test_html_entities_unescaped():
    assert parse_comment(
        "PROD_PROJECT=Fish &amp; Chips PROD_STAGE=QC &gt; Final") == (
        "Fish & Chips", "QC > Final")


def test_multiline_comment():
    assert parse_comment(
        "Some notes here\nPROD_PROJECT=Heatsink\nPROD_STAGE=Anodised\nmore") == (
        "Heatsink", "Anodised")


def test_empty_project_rejected():
    assert parse_comment("PROD_PROJECT= PROD_STAGE=X") == (None, None)
    assert parse_comment("PROD_PROJECT=<br>") == (None, None)


def test_empty_stage_becomes_none():
    assert parse_comment("PROD_PROJECT=P PROD_STAGE=   ") == ("P", None)


def test_project_name_containing_stage_like_text():
    # Only a real `PROD_STAGE=` assignment splits the name.
    assert parse_comment(
        "PROD_PROJECT=My PROD_STAGE project PROD_STAGE=Real") == (
        "My PROD_STAGE project", "Real")


def test_names_are_truncated_not_exploding():
    long_name = "P" * 500
    project, stage = parse_comment(
        f"PROD_PROJECT={long_name} PROD_STAGE=S")
    assert project == "P" * 200
    assert stage == "S"


def test_strip_html_helper():
    assert strip_html(None) == ""
    assert strip_html("<b>A</b>&nbsp;B<br>C") == "A B\nC"
    assert strip_html("a&nbsp;&nbsp;b") == "a b"
