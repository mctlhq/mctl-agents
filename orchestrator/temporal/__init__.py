"""Temporal control plane for the dev-workflow pipeline (plan phase 4).

DevLoopWorkflow (workflows/dev_loop.py) is the durable orchestrator; the
actual Claude Agent SDK runs still happen in Argo (see activities/argo.py's
submit_and_wait), which submits the same ClusterWorkflowTemplates the cron
pipeline already uses. This package only adds a durable, resumable "what
stage is this issue at" layer on top — it does not reimplement any agent.
"""
