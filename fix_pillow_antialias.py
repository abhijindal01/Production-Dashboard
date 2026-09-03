#!/usr/bin/env python3
"""
Fix: module 'PIL.Image' has no attribute 'ANTIALIAS'

Cause
-----
Pillow 10.0 removed the Image.ANTIALIAS constant. Any Python program
that still calls resize()/thumbnail() with Image.ANTIALIAS crashes
with this AttributeError (for example when you press "Print label" /
"Preview" in a label printing tool).

Fix
---
Replace every occurrence of:
    Image.ANTIALIAS      -> Image.Resampling.LANCZOS   (Pillow >= 9.1)
    PIL.Image.ANTIALIAS  -> PIL.Image.Resampling.LANCZOS
with a fallback to Image.LANCZOS on older Pillow versions.

The patcher makes a .bak copy of every file before changing it and
skips common non-source directories (.git, site-packages-less caches,
venvs, node_modules, ...). Run it from the folder that contains the
label printing / Part-DB helper python code.

Usage
-----
    # dry run: only list files/lines that would change
    python3 fix_pillow_antialias.py --check [DIR_OR_FILE ...]

    # apply the fix (default: search current directory)
    python3 fix_pillow_antialias.py [DIR_OR_FILE ...]

Examples
--------
    python3 fix_pillow_antialias.py --check /opt/labelapp
    python3 fix_pillow_antialias.py /opt/labelapp
    python3 fix_pillow_antialias.py /opt/labelapp/label_render.py

Quick alternatives (temporary)
------------------------------
    # 1. pin old Pillow inside the environment running the app
    pip install "Pillow<10"

    # 2. check the installed version
    python3 -c "from PIL import Image; print(Image.__version__)"
"""

import os
import re
import shutil
import sys
from argparse import ArgumentParser

# Matches both "Image.ANTIALIAS" and "PIL.Image.ANTIALIAS"
TOKEN_RE = re.compile(r"\bPIL\.Image\.ANTIALIAS\b|\bImage\.ANTIALIAS\b")

# Directories that are never worth scanning
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}

SKIP_EXT = {".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe"}


def replacement_tokens():
    """
    Choose the most compatible replacement for the local Pillow version.
    Pillow >= 9.1 has Image.Resampling; older versions need Image.LANCZOS.
    """
    image = 'Image'
    pil_image = 'PIL.Image'
    try:
        from PIL import Image  # noqa: PLC0415

        if hasattr(Image, "Resampling"):
            return f"{image}.Resampling.LANCZOS", f"{pil_image}.Resampling.LANCZOS"
    except Exception:
        pass
    return f"{image}.LANCZOS", f"{pil_image}.LANCZOS"


def scan_files(paths):
    """Yield absolute paths of .py files under the given dirs/files."""
    for path in paths:
        if os.path.isfile(path):
            if path.endswith(".py"):
                yield os.path.abspath(path)
            continue

        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def fix_file(path, tokens, dry_run):
    """Fix one file. Returns (changed, matches)."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        original = handle.read()

    image_token, pil_image_token = tokens
    matches = list(TOKEN_RE.finditer(original))
    if not matches:
        return False, 0

    def repl(match):
        text = match.group(0)
        if text == "PIL.Image.ANTIALIAS":
            return pil_image_token
        return image_token

    fixed = TOKEN_RE.sub(repl, original)

    if fixed == original:
        return False, len(matches)

    if not dry_run:
        backup = path + ".bak"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(fixed)

    return True, len(matches)


def main():
    parser = ArgumentParser(description="Replace Pillow Image.ANTIALIAS with LANCZOS.")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Python files or directories to scan (default: current directory)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry run: only show what would change, do not modify anything",
    )
    args = parser.parse_args()

    tokens = replacement_tokens()
    print("Replacement:", tokens[0])

    changed = 0
    total = 0

    for file_path in scan_files(args.paths):
        try:
            is_changed, count = fix_file(file_path, tokens, args.check)
        except OSError as exc:
            print(f"[SKIP] {file_path}: {exc}")
            continue

        if count:
            total += count
            print(f"  {file_path}: {count} occurrence(s)")
        if is_changed:
            changed += 1

    print()
    if args.check:
        print(f"Would fix {changed} file(s), {total} occurrence(s).")
        print("Run without --check to apply the fix.")
    else:
        print(f"Fixed {changed} file(s), {total} occurrence(s).")
        if changed:
            print("Restart the label printing app/service after patching.")
    return 0 if total == 0 or not args.check else 0


if __name__ == "__main__":
    sys.exit(main())
