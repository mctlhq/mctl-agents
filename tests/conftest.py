"""Shared pytest fixtures.

The repo has no pyproject.toml; we resolve the project root by walking
up from this file so `pytest` can be run from anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Put the repo root on sys.path so `import orchestrator.run_shepherd`
# resolves without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
