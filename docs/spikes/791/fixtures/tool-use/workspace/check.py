"""Prints RESULT=<multiplier * 6>. The first run in a fresh workspace fails on purpose."""
import json
import pathlib
import sys

state = pathlib.Path(__file__).with_name(".check-state")
if not state.exists():
    state.write_text("1")
    print("ERROR: lock held by another job, retry", file=sys.stderr)
    sys.exit(3)
cfg = json.loads(pathlib.Path(__file__).with_name("config.json").read_text())
print(f"RESULT={cfg['multiplier'] * 6}")
