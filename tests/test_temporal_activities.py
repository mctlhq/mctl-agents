"""Tests for orchestrator/temporal/activities/*.

Runs each @activity.defn function through temporalio.testing.ActivityEnvironment
(so activity.heartbeat()/activity.logger calls have a real, if fake, activity
context) with httpx wired to a MockTransport instead of a real network call —
mctl-api's actual request/response shapes are asserted against directly
(field names, status codes), not just "was called".
"""
from __future__ import annotations

import json

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, submit_and_wait
from orchestrator.temporal.activities.registry import resolve_agent_release
from orchestrator.temporal.activities.state import ExecutionRecord, record_execution

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def mctl_token(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")


@pytest.fixture
def env():
    return ActivityEnvironment()


def _mock_async_client(monkeypatch, handler):
    """Force every httpx.AsyncClient(...) constructed by the activity code to
    route through a MockTransport, while preserving the base_url/timeout
    kwargs the activity passes (asserted on indirectly via request.url)."""
    real_client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class TestResolveAgentRelease:
    async def test_resolves_version_and_image(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-token"
            if request.url.path == "/api/v1/agents/issue-investigator/resolve":
                assert request.url.params["environment"] == "production"
                return httpx.Response(200, json={"agent": "issue-investigator", "version": "1.2.0"})
            if request.url.path == "/api/v1/agents/issue-investigator/versions":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"version": "1.1.0", "image_repository": "ghcr.io/mctlhq/mctl-agents"},
                            {
                                "version": "1.2.0",
                                "image_repository": "ghcr.io/mctlhq/mctl-agents",
                                "image_digest": "sha256:abc123",
                            },
                        ]
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "issue-investigator", "production")
        assert result is not None
        assert result.version == "1.2.0"
        assert result.image_ref == "ghcr.io/mctlhq/mctl-agents@sha256:abc123"

    async def test_falls_back_to_tag_without_digest(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/resolve"):
                return httpx.Response(200, json={"version": "1.2.0"})
            return httpx.Response(
                200, json={"items": [{"version": "1.2.0", "image_repository": "ghcr.io/mctlhq/mctl-agents"}]}
            )

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "implementer", "production")
        assert result.image_ref == "ghcr.io/mctlhq/mctl-agents:1.2.0"

    async def test_does_not_double_a_repo_that_already_carries_a_tag(self, env, monkeypatch):
        """Regression for incident 2026-08-06: a row published before
        mctl-api validated image_repository could still have the tag baked
        in (e.g. "ghcr.io/mctlhq/mctl-agents:1.22.0"). Blindly appending
        ":{version}" doubled it into an invalid image reference
        ("...:1.22.0:1.22.0") that the CWFT pod could never pull. resolve
        must trust an already-tagged repo instead of appending again."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/resolve"):
                return httpx.Response(200, json={"version": "1.22.0"})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "version": "1.22.0",
                            "image_repository": "ghcr.io/mctlhq/mctl-agents:1.22.0",
                        }
                    ]
                },
            )

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "issue-investigator", "production")
        assert result.image_ref == "ghcr.io/mctlhq/mctl-agents:1.22.0"

    async def test_does_not_double_a_repo_that_already_carries_a_digest(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/resolve"):
                return httpx.Response(200, json={"version": "1.22.0"})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "version": "1.22.0",
                            "image_repository": "ghcr.io/mctlhq/mctl-agents@sha256:abc123",
                        }
                    ]
                },
            )

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "issue-investigator", "production")
        assert result.image_ref == "ghcr.io/mctlhq/mctl-agents@sha256:abc123"

    async def test_does_not_double_a_repo_carrying_its_own_digest_plus_a_separate_image_digest_field(
        self, env, monkeypatch
    ):
        """The digest branch needs the same already-tagged/digested guard as
        the tag branch: a legacy repo with its own baked-in "@sha256:..."
        that *also* has a populated image_digest field must not produce
        "...@sha256:x@sha256:y". Caught in review of the tag-only fix."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/resolve"):
                return httpx.Response(200, json={"version": "1.22.0"})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "version": "1.22.0",
                            "image_repository": "ghcr.io/mctlhq/mctl-agents@sha256:abc123",
                            "image_digest": "sha256:xyz789",
                        }
                    ]
                },
            )

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "issue-investigator", "production")
        assert result.image_ref == "ghcr.io/mctlhq/mctl-agents@sha256:abc123"

    async def test_returns_none_when_never_released(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "no release"})

        _mock_async_client(monkeypatch, handler)
        result = await env.run(resolve_agent_release, "mentor", "production")
        assert result is None

    async def test_raises_without_mctl_token(self, env, monkeypatch):
        monkeypatch.delenv("MCTL_TOKEN", raising=False)
        with pytest.raises(Exception, match="MCTL_TOKEN"):
            await env.run(resolve_agent_release, "issue-investigator", "production")


class TestSubmitAndWait:
    async def test_submits_and_polls_to_success(self, env, monkeypatch):
        calls = {"status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/api/v1/operations/mctl-agents-investigate/execute":
                body = json.loads(request.content)
                assert body == {"issue_url": "https://github.com/mctlhq/mctl-telegram/issues/1"}
                return httpx.Response(
                    202,
                    json={
                        "operation": "mctl-agents-investigate",
                        "workflow": {"workflowName": "mctl-agents-investigate-ab12cd34"},
                    },
                )
            if request.url.path == "/api/v1/workflows/mctl-agents-investigate-ab12cd34":
                calls["status"] += 1
                # First poll: not yet terminal. Second poll: Succeeded.
                phase = "Running" if calls["status"] == 1 else "Succeeded"
                return httpx.Response(
                    200,
                    json={
                        "workflow": "mctl-agents-investigate-ab12cd34",
                        "live": {"status": {"phase": phase, "startedAt": "2026-08-05T00:00:00Z"}},
                    },
                )
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        # Skip the real sleep between polls so the test is instant.
        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr("orchestrator.temporal.activities.argo.asyncio.sleep", no_sleep)
        _mock_async_client(monkeypatch, handler)

        result = await env.run(
            submit_and_wait,
            SubmitAndWaitInput(
                operation="mctl-agents-investigate",
                params={"issue_url": "https://github.com/mctlhq/mctl-telegram/issues/1"},
            ),
        )
        assert result.workflow_name == "mctl-agents-investigate-ab12cd34"
        assert result.phase == "Succeeded"
        assert result.succeeded is True
        assert calls["status"] == 2

    async def test_failed_phase_is_not_succeeded(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(202, json={"workflow": {"workflowName": "wf-1"}})
            return httpx.Response(200, json={"live": {"status": {"phase": "Failed"}}})

        _mock_async_client(monkeypatch, handler)
        result = await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-implement", params={}))
        assert result.phase == "Failed"
        assert result.succeeded is False

    async def test_raises_on_submit_http_error(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "agent registry is admin-only"})

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(httpx.HTTPStatusError):
            await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))

    async def test_heartbeats_workflow_name_right_after_submit(self, env, monkeypatch):
        """The first heartbeat must carry workflow_name — a retried attempt
        depends on reading this back via heartbeat_details to know not to
        resubmit (see the next two tests)."""
        heartbeats = []
        env.on_heartbeat = lambda *details: heartbeats.append(details)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(202, json={"workflow": {"workflowName": "wf-1"}})
            return httpx.Response(200, json={"live": {"status": {"phase": "Succeeded"}}})

        _mock_async_client(monkeypatch, handler)
        await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))
        assert heartbeats[0] == ("wf-1",)

    async def test_unparseable_submit_response_heartbeats_sentinel_and_raises(self, env, monkeypatch):
        """mctl-api returning 2xx (Argo run genuinely created) with a body
        that doesn't parse into a workflow name must not silently swallow
        the fact that a run exists — and a retry after this must never
        resubmit, since that would duplicate the real SDK run."""
        heartbeats = []
        env.on_heartbeat = lambda *details: heartbeats.append(details)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(202, json={"unexpected": "shape"})

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(RuntimeError, match="could not parse the workflow"):
            await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))
        assert heartbeats == [("<submitted, workflow name unparseable>",)]

    async def test_retry_after_unparseable_response_refuses_to_resubmit(self, env, monkeypatch):
        import dataclasses

        env.info = dataclasses.replace(
            env.info, heartbeat_details=["<submitted, workflow name unparseable>"]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must never make an HTTP call once the unparseable-response sentinel is seen")

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(RuntimeError, match="refusing to resubmit"):
            await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))

    async def test_resumes_from_heartbeat_details_without_resubmitting(self, env, monkeypatch):
        """Simulates a retried attempt after a worker crash: heartbeat_details
        already has the workflow_name from a prior attempt's submission. The
        activity must go straight to polling, never POSTing a second run —
        that's the whole point of allowing SDK_STEP_RETRY_POLICY > 1."""
        import dataclasses

        env.info = dataclasses.replace(env.info, heartbeat_details=["mctl-agents-investigate-ab12cd34"])

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                raise AssertionError("must not re-submit when heartbeat_details already has a workflow_name")
            assert request.url.path == "/api/v1/workflows/mctl-agents-investigate-ab12cd34"
            return httpx.Response(200, json={"live": {"status": {"phase": "Succeeded"}}})

        _mock_async_client(monkeypatch, handler)
        result = await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))
        assert result.workflow_name == "mctl-agents-investigate-ab12cd34"
        assert result.succeeded is True

    async def test_rides_out_transient_poll_errors(self, env, monkeypatch):
        """A handful of consecutive poll failures (mctl-api 5xx blip) must
        not kill the activity — only a persistent run of them should."""
        calls = {"status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(202, json={"workflow": {"workflowName": "wf-1"}})
            calls["status"] += 1
            if calls["status"] <= 3:
                return httpx.Response(502, json={"error": "bad gateway"})
            return httpx.Response(200, json={"live": {"status": {"phase": "Succeeded"}}})

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr("orchestrator.temporal.activities.argo.asyncio.sleep", no_sleep)
        _mock_async_client(monkeypatch, handler)

        result = await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))
        assert result.phase == "Succeeded"
        assert calls["status"] == 4  # 3 failures + 1 success

    async def test_gives_up_after_too_many_consecutive_poll_errors(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(202, json={"workflow": {"workflowName": "wf-1"}})
            return httpx.Response(502, json={"error": "bad gateway"})

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr("orchestrator.temporal.activities.argo.asyncio.sleep", no_sleep)
        _mock_async_client(monkeypatch, handler)

        with pytest.raises(httpx.HTTPStatusError):
            await env.run(submit_and_wait, SubmitAndWaitInput(operation="mctl-agents-investigate", params={}))


class TestRecordExecution:
    async def test_posts_execution_record(self, env, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/agents/executions"
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"ok": True})

        _mock_async_client(monkeypatch, handler)
        await env.run(
            record_execution,
            ExecutionRecord(
                temporal_workflow_id="dev-loop-mctlhq-mctl-telegram-1",
                agent="issue-investigator",
                environment="production",
                version="1.2.0",
                image_ref="ghcr.io/mctlhq/mctl-agents@sha256:abc123",
                target_repo="mctl-telegram",
                argo_workflow_name="mctl-agents-investigate-ab12cd34",
                phase="Succeeded",
            ),
        )
        assert seen["body"]["agent"] == "issue-investigator"
        assert seen["body"]["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-telegram-1"
        assert seen["body"]["phase"] == "Succeeded"
        assert seen["body"]["target_repo"] == "mctl-telegram"

    async def test_raises_on_error_response(self, env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid"})

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(httpx.HTTPStatusError):
            await env.run(
                record_execution,
                ExecutionRecord(
                    temporal_workflow_id="x",
                    agent="issue-investigator",
                    environment="production",
                    version="",
                    image_ref="",
                    target_repo="",
                    argo_workflow_name="wf",
                    phase="Succeeded",
                ),
            )


class TestDiscoverAndProject:
    async def test_discover_and_project_empty_dir(self, env, tmp_path):
        from orchestrator.temporal.activities.discovery import discover_and_project
        result = await env.run(discover_and_project, str(tmp_path))
        assert result.total_inspected == 0
        assert result.projections == []


class TestDetectOrphans:
    async def test_detect_orphans_empty_dir(self, env, tmp_path):
        from orchestrator.temporal.activities.orphans import detect_orphans
        result = await env.run(detect_orphans, str(tmp_path), [])
        assert result.total_actionable == 0
        assert result.orphans == []

    async def test_detect_orphans_filters_active_workflow(self, env, tmp_path, monkeypatch):
        from orchestrator.temporal.activities.orphans import detect_orphans
        from orchestrator.run_shepherd import ProposalRef, PRSnapshot

        fake_ref = ProposalRef(
            service="mctl-web",
            slug="test-slug",
            proposal_dir=tmp_path / "mctl-web" / "proposals" / "test-slug",
            status="accepted",
            pr_url="https://github.com/mctlhq/mctl-web/pull/10",
        )
        fake_pr = PRSnapshot(
            number=10,
            repo="mctlhq/mctl-web",
            state="OPEN",
            merged=False,
            closed_unmerged=False,
            merge_commit=None,
            close_comment_or_default="",
            head_sha="abc12345",
            head_pushed_at="2026-08-06T00:00:00Z",
            merge_state_status="CLEAN",
            checks_green=True,
            is_draft=False,
        )

        monkeypatch.setattr("orchestrator.temporal.activities.orphans._discover_refs", lambda *a, **kw: [fake_ref])
        monkeypatch.setattr("orchestrator.temporal.activities.orphans.find_pr_for_proposal", lambda *a, **kw: fake_pr)

        result_active = await env.run(detect_orphans, str(tmp_path), ["dev-loop-mctlhq-mctl-web-test-slug"])
        assert len(result_active.orphans) == 0

        result_orphan = await env.run(detect_orphans, str(tmp_path), [])
        assert len(result_orphan.orphans) == 1
        assert result_orphan.orphans[0].slug == "test-slug"

