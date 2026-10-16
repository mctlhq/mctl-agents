"""Spike: OpenSpec-compatible artifact contract for DevLoop proposals (#655).

Compatibility target: OpenSpec v1.14.2, commit f80d860
(f80d860fc510ba4f51180c8ddde35b91b32de657, @fission-ai/openspec 1.14.2).

This package is a SPIKE. It is pure Python (PyYAML only), needs no Node.js and
no OpenSpec CLI, and is NOT imported by any runner, workflow or activity. It
must never import approval, authorization or Shepherd code. See
docs/spikes/655-openspec-contract.md for the decision record.
"""

COMPAT_TARGET = "OpenSpec v1.14.2 (f80d860)"
