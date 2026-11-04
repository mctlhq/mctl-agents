#!/usr/bin/env python3
"""Fail unless `release-hold.json` says, in so many words, that no hold is on.

A merged release PR is what cuts a tag, publishes the image and dispatches the
deploy (`.github/workflows/release-please.yml`). Holding that PR back by hand
(Draft, a watcher) leaves a window between its creation and the hold, and any
automation that merges green PRs closes the window for good. This check sits at
the point of effect instead: the `release-hold` job runs it before the
`release-please` job may start, so a held release is not cut however its PR got
merged.

Fail-closed. Only a file that parses to an object whose `hold` is the JSON
boolean `false` passes. A missing file, unreadable file, malformed JSON, a
non-object, a missing key, `"false"` as a string, `0`, `null` and `true` all
stop the release: "could not tell" is never "no hold".

    python3 tools/check_release_hold.py [--file release-hold.json]

Exit 0: no hold. Exit 1: hold on, or the file could not be read as an answer.
Standard library only, so the gate needs no `uv sync`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_FILE = "release-hold.json"


def evaluate(path: Path) -> tuple[bool, str]:
    """(released, message). `released` is True only for an explicit `false`."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read {path}: {exc}; treating as a hold"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return False, f"{path} is not valid JSON ({exc}); treating as a hold"
    if not isinstance(data, dict) or "hold" not in data:
        return False, f"{path} has no `hold` key; treating as a hold"
    hold = data["hold"]
    if hold is False:
        return True, f"{path}: hold is false, release may proceed"
    reason = data.get("reason")
    shown = reason if isinstance(reason, str) and reason else "no reason recorded"
    if hold is True:
        return False, f"release hold is ON: {shown}"
    return False, f"{path}: `hold` must be the boolean false to release, got {hold!r}; treating as a hold"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=DEFAULT_FILE)
    released, message = evaluate(Path(parser.parse_args(argv).file))
    print(message if released else f"::error::{message}")
    return 0 if released else 1


if __name__ == "__main__":
    sys.exit(main())
