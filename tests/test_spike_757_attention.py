"""Collector: runs docs/spikes/757/test_attention.py in the default pytest collection."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "docs" / "spikes" / "757" / "test_attention.py"
_spec = importlib.util.spec_from_file_location("spike_757_test_attention", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

globals().update({k: v for k, v in vars(_mod).items() if k.startswith("test_")})
