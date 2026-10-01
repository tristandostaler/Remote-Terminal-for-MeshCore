#!/usr/bin/env python3
"""Regenerate the tinyllm bot's converted reference notes.

The ``tinyllm`` bot ships notes in ``app/bots/library/docs/``. Some are other
documentation converted for the bot's keyword search: headings are kept (each
one starts a searchable section), while tables of contents, links, bold
markers and horizontal rules are dropped.

* ``remoteterm-*.md`` -- this repository's own READMEs. Always regenerated;
  rerun after a README change worth knowing offline (the READMEs are not in
  the Docker image, so the bot cannot read them at runtime).
* ``meshcore-cli.md`` and ``meshcore-faq.md`` -- MeshCore's CLI command
  reference and FAQ. Only regenerated when given a MeshCore checkout:

    uv run python scripts/build/update_tinyllm_docs.py
    git clone --depth 1 https://github.com/meshcore-dev/MeshCore /tmp/MeshCore
    uv run python scripts/build/update_tinyllm_docs.py /tmp/MeshCore

MeshCore is MIT-licensed; its copyright and permission notice travel in each
generated file's header comment (comments are not indexed by the search).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "app" / "bots" / "library" / "docs"
# This repository's own docs: output file -> (source file, top heading).
LOCAL_SOURCES = {
    "remoteterm-readme.md": ("README.md", "RemoteTerm setup and features"),
    "remoteterm-advanced.md": (
        "README_ADVANCED.md",
        "RemoteTerm advanced setup and troubleshooting",
    ),
    "remoteterm-home-assistant.md": ("README_HA.md", "RemoteTerm Home Assistant integration"),
}
SOURCES = {
    # output file: (source file, title for the top heading)
    "meshcore-cli.md": (
        "docs/cli_commands.md",
        "MeshCore CLI commands (repeater, room server, sensor)",
    ),
    "meshcore-faq.md": ("docs/faq.md", "MeshCore FAQ"),
}

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Real HTML tags only: command placeholders such as <name> or <value> look
# like tags too, and are exactly the part of a command worth keeping.
_HTML = re.compile(
    r"</?(?:a|b|br|code|details|div|em|i|img|kbd|p|span|strong|sub|summary|sup|"
    r"table|tbody|td|th|thead|tr|u)\b[^>]*>",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# "3.9.4. Q: What ..." -> "Q: What ..."
_NUMBERING = re.compile(r"^\d+(?:\.\d+)*\.?\s+")


def _clean_heading(text: str) -> str:
    text = _LINK.sub(r"\1", text).replace("**", "").strip()
    return _NUMBERING.sub("", text)


def convert(markdown: str, title: str) -> str:
    """Firmware doc -> notes: same headings, prose only."""
    out: list[str] = []
    in_navigation = False
    in_code = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            # Kept as text, indented: a code line such as "#Europe F" (a
            # region name) would otherwise read as a markdown heading.
            out.append(f"    {line.rstrip()}" if line.strip() else "")
            continue
        match = _HEADING.match(line)
        if match:
            level, text = len(match.group(1)), _clean_heading(match.group(2))
            in_navigation = text.lower() == "navigation"
            if not in_navigation:
                out.append(f"# {title}" if level == 1 else f"{match.group(1)} {text}")
            continue
        stripped = line.strip()
        if (
            in_navigation
            # A table of contents: list items that are nothing but a link.
            or re.fullmatch(r"[-*]\s+\[[^\]]*\]\([^)]*\)", stripped)
            or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped)
        ):
            continue
        line = _LINK.sub(r"\1", line)
        line = _HTML.sub("", line).replace("**", "")
        out.append(line.rstrip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        print(__doc__)
        return 2
    for name, (source, title) in LOCAL_SOURCES.items():
        body = convert((REPO_ROOT / source).read_text(), title)
        header = (
            "<!--\n"
            f"Converted from RemoteTerm's {source} by scripts/build/update_tinyllm_docs.py.\n"
            "Notes for the tinyllm bot, overwritten in the tinyllm-docs folder on every "
            "restart: put your own notes in another .md file there.\n"
            "-->\n\n"
        )
        (OUT_DIR / name).write_text(header + body)
        print(f"wrote {OUT_DIR / name} ({len(body)} chars)")
    if len(sys.argv) == 1:
        return 0
    repo = Path(sys.argv[1])
    license_text = (repo / "license.txt").read_text().strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h (%cs)"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    for name, (source, title) in SOURCES.items():
        body = convert((repo / source).read_text(), title)
        header = (
            "<!--\n"
            f"Converted from MeshCore's {source} (github.com/meshcore-dev/MeshCore, "
            f"{commit or 'unknown revision'}) by scripts/build/update_tinyllm_docs.py.\n"
            "Starter notes for the tinyllm bot, overwritten in the tinyllm-docs folder on every "
            "restart: put your own notes in another .md file there.\n\n"
            f"{license_text}\n"
            "-->\n\n"
        )
        (OUT_DIR / name).write_text(header + body)
        print(f"wrote {OUT_DIR / name} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
