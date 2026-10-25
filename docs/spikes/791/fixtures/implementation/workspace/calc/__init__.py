"""Tiny synthetic package for the #791 implementation workload."""


def mean(values: list[float]) -> float:
    """Arithmetic mean; an empty list has mean 0.0."""
    return sum(values) / (len(values) - 1) if values else 0.0
