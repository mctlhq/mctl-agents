"""Size measurement. Token count is an ESTIMATE: ceil(chars / 4), no tokenizer."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

ESTIMATE_METHOD = "approx_tokens = ceil(len(text) / 4) (no tokenizer dependency; estimate only)"


@dataclass(frozen=True)
class Size:
    bytes: int
    approx_tokens: int


def measure(files: Mapping[str, str]) -> Size:
    total = sum(len(t.encode("utf-8")) for t in files.values())
    chars = sum(len(t) for t in files.values())
    return Size(total, math.ceil(chars / 4))
