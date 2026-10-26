"""Client side of the SecurityFinding remediation lifecycle (mctl-api 4.124.0).

mctl-api decides every state change and audits every write; this package only
reads, picks a verification strategy, and writes through three mutation
routes. See `docs/adr/024-security-finding-remediation-runner.md`.
"""
