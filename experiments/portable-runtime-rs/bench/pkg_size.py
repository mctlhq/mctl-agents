"""Prints `<distribution count> <size kB> <interpreter kB>` for the closure of
`mcp`'s requirements in the running environment (stdlib only)."""

import importlib.metadata as md
import os
import re
import sys


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def tree_kb(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total // 1024


installed, seen, stack, size = set(), set(), ["mcp"], 0
while stack:
    name = norm(stack.pop())
    if name in seen:
        continue
    seen.add(name)
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        continue  # conditional dependency not installed on this platform
    installed.add(name)
    for f in dist.files or []:
        try:
            size += os.lstat(dist.locate_file(f)).st_size
        except OSError:
            pass
    for req in dist.requires or []:
        if "extra ==" in req:
            continue
        stack.append(re.split(r"[ ;<>=!~\[(]", req, maxsplit=1)[0])
print(len(installed), size // 1024, tree_kb(sys.base_prefix))
