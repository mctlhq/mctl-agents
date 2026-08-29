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
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, submit_and_wait
from orchestrator.temporal.activities.proposals import ProposalListingError, find_proposal_slug
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
        from orchestrator.run_shepherd import ProposalRef, PRSnapshot
        from orchestrator.temporal.activities.orphans import detect_orphans

        # Slug carries the issue-<N>- prefix the investigator always writes;
        # the expected workflow id is derived from that issue number (the id
        # scheme in start.py:workflow_id_for), NOT from the full slug — the
        # old slug-based reconstruction matched nothing (mctl-agents#151).
        fake_ref = ProposalRef(
            service="mctl-web",
            slug="issue-10-test-slug",
            proposal_dir=tmp_path / "mctl-web" / "proposals" / "issue-10-test-slug",
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

        result_active = await env.run(detect_orphans, str(tmp_path), ["dev-loop-mctlhq-mctl-web-10"])
        assert len(result_active.orphans) == 0

        result_orphan = await env.run(detect_orphans, str(tmp_path), [])
        assert len(result_orphan.orphans) == 1
        assert result_orphan.orphans[0].slug == "issue-10-test-slug"


class TestVisibilityActivities:
    async def test_list_active_dev_loop_ids_collects_running_ids(self, env):
        from orchestrator.temporal.activities.visibility import (
            ACTIVE_DEV_LOOPS_QUERY,
            VisibilityActivities,
        )

        class _Exec:
            def __init__(self, wid: str) -> None:
                self.id = wid

        class _FakeClient:
            def __init__(self) -> None:
                self.query = None

            def list_workflows(self, query: str):
                self.query = query

                async def gen():
                    yield _Exec("dev-loop-mctlhq-mctl-web-10")
                    yield _Exec("dev-loop-mctlhq-mctl-api-7")

                return gen()

        client = _FakeClient()
        acts = VisibilityActivities(client)
        ids = await env.run(acts.list_active_dev_loop_ids)
        assert ids == ["dev-loop-mctlhq-mctl-web-10", "dev-loop-mctlhq-mctl-api-7"]
        assert client.query == ACTIVE_DEV_LOOPS_QUERY
        # Running-only is load-bearing: closed DevLoops are exactly the
        # orphan case detect_orphans exists to catch.
        assert "ExecutionStatus = 'Running'" in ACTIVE_DEV_LOOPS_QUERY




class TestFindProposalSlug:
    def _entries(self, *names: str) -> list[dict[str, str]]:
        return [{"name": n, "type": "dir"} for n in names]

    async def test_matches_exact_issue_prefix(self, env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == (
                "/repos/mctlhq/mctl-gitops/contents/"
                "platform-gitops/agents-state/mctl-portal/proposals"
            )
            assert request.url.params["ref"] == "main"
            assert request.headers["authorization"] == "Bearer gh-test-token"
            return httpx.Response(
                200,
                json=self._entries(
                    "issue-9-other-thing",
                    "issue-79-remove-unauthenticated-access-from-githu",
                    "issue-80-enforce-tenant-ownership-in-custom-domai",
                ),
            )

        _mock_async_client(monkeypatch, handler)
        slug = await env.run(find_proposal_slug, "mctl-portal", "80")
        assert slug == "issue-80-enforce-tenant-ownership-in-custom-domai"

    async def test_prefix_dash_prevents_issue_number_prefix_collision(self, env, monkeypatch):
        """issue-9 must not match issue-98's directory (and vice versa)."""
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._entries("issue-98-fail-closed"))

        _mock_async_client(monkeypatch, handler)
        assert await env.run(find_proposal_slug, "mctl-agent", "9") is None

    async def test_missing_proposals_dir_is_none_not_error(self, env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        _mock_async_client(monkeypatch, handler)
        assert await env.run(find_proposal_slug, "mctl-docs", "5") is None

    async def test_server_error_raises_for_retry(self, env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ProposalListingError):
            await env.run(find_proposal_slug, "mctl-portal", "80")

    async def test_duplicate_dirs_for_one_issue_refuse_to_guess(self, env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=self._entries("issue-80-old-title", "issue-80-new-title")
            )

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ApplicationError) as excinfo:
            await env.run(find_proposal_slug, "mctl-portal", "80")
        assert excinfo.value.non_retryable

    async def test_files_matching_prefix_are_ignored(self, env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"name": "issue-80-stray-file.md", "type": "file"},
                    {"name": "issue-80-enforce-tenant", "type": "dir"},
                ],
            )

        _mock_async_client(monkeypatch, handler)
        assert await env.run(find_proposal_slug, "mctl-portal", "80") == "issue-80-enforce-tenant"

    async def test_empty_token_raises_instead_of_unauthenticated_404(self, env, monkeypatch):
        """An unauthenticated lookup against the private gitops repo would
        404 and masquerade as a missing proposal — the activity must raise
        (retryably) instead of ever sending that request."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(str(request.url))
            return httpx.Response(404)

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ProposalListingError, match="no GitHub token available"):
            await env.run(find_proposal_slug, "mctl-portal", "80")
        assert requests_made == []

    async def test_leading_zero_issue_number_is_normalized(self, env, monkeypatch):
        """A manually started workflow can carry /issues/007 — the dir on
        disk is issue-7-*, so the prefix must use the canonical number."""
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._entries("issue-7-some-title"))

        _mock_async_client(monkeypatch, handler)
        assert await env.run(find_proposal_slug, "mctl-docs", "007") == "issue-7-some-title"

    async def test_listing_at_contents_api_cap_raises(self, env, monkeypatch):
        """A 1000-entry listing may be silently truncated by the contents
        API — a missing match proves nothing, so the activity must refuse."""
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            names = [f"issue-{i}-old" for i in range(1000)]
            return httpx.Response(200, json=self._entries(*names))

        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ApplicationError, match="listing cap") as excinfo:
            await env.run(find_proposal_slug, "mctl-portal", "80")
        assert excinfo.value.non_retryable


class TestGetPRState:
    STATUS_PATH = (
        "/repos/mctlhq/mctl-gitops/contents/platform-gitops/agents-state/"
        "mctl-web/proposals/issue-10-test/.status.yaml"
    )

    @staticmethod
    def _status_payload(body: str) -> dict:
        import base64 as _b64

        return {"content": _b64.b64encode(body.encode()).decode()}

    def _handler(self, monkeypatch, *, status_yaml, pr_json=None, pr_status=200):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer gh-test-token"
            if request.url.path == self.STATUS_PATH:
                if status_yaml is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json=self._status_payload(status_yaml))
            if request.url.path == "/repos/mctlhq/mctl-web/pulls/99":
                return httpx.Response(pr_status, json=pr_json or {})
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)

    async def test_open_pr(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="status: implemented\npr: https://github.com/mctlhq/mctl-web/pull/99\n",
            pr_json={"state": "open", "merged": False, "html_url": "https://github.com/mctlhq/mctl-web/pull/99"},
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is True
        assert (state.repo, state.number, state.state, state.merged) == ("mctlhq/mctl-web", 99, "OPEN", False)

    async def test_merged_pr_carries_merge_commit(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="pr: https://github.com/mctlhq/mctl-web/pull/99\n",
            pr_json={"state": "closed", "merged": True, "merge_commit_sha": "cafe1234"},
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.state == "MERGED"
        assert state.merged is True
        assert state.merge_commit == "cafe1234"

    async def test_closed_unmerged_pr(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="pr: https://github.com/mctlhq/mctl-web/pull/99\n",
            pr_json={"state": "closed", "merged": False},
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.state == "CLOSED"
        assert state.merged is False
        assert state.merge_commit is None

    async def test_missing_status_file_is_not_found(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(monkeypatch, status_yaml=None)
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is False

    async def test_status_without_pr_field_is_not_found(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(monkeypatch, status_yaml="status: implemented\n")
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is False

    async def test_vanished_pr_is_not_found_but_keeps_reference(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="pr: https://github.com/mctlhq/mctl-web/pull/99\n",
            pr_status=404,
            pr_json={"message": "Not Found"},
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is False
        assert (state.repo, state.number) == ("mctlhq/mctl-web", 99)

    async def test_server_error_raises_for_retry(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state
        from orchestrator.temporal.activities.proposals import ProposalListingError

        self._handler(
            monkeypatch,
            status_yaml="pr: https://github.com/mctlhq/mctl-web/pull/99\n",
            pr_status=502,
            pr_json={"message": "bad gateway"},
        )
        with pytest.raises(ProposalListingError):
            await env.run(get_pr_state, "mctl-web", "issue-10-test")

    async def test_no_token_raises_instead_of_unauthenticated_404(self, env, monkeypatch):
        from orchestrator.temporal.activities.pr_state import get_pr_state
        from orchestrator.temporal.activities.proposals import ProposalListingError

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        with pytest.raises(ProposalListingError, match="no GitHub token"):
            await env.run(get_pr_state, "mctl-web", "issue-10-test")

    async def test_api_style_pr_url_is_accepted(self, env, monkeypatch):
        """run_shepherd._parse_pr_url supports the API URL form too — a
        repaired .status.yaml carrying it must not read as 'no PR'."""
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="pr: https://api.github.com/repos/mctlhq/mctl-web/pulls/99\n",
            pr_json={"state": "open", "merged": False},
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is True
        assert (state.repo, state.number, state.state) == ("mctlhq/mctl-web", 99, "OPEN")

    async def test_pr_link_outside_proposal_repo_is_refused(self, env, monkeypatch):
        """A stale/hand-edited link to another repo's PR must not complete
        merge detection with an unrelated PR's state."""
        from orchestrator.temporal.activities.pr_state import get_pr_state

        self._handler(
            monkeypatch,
            status_yaml="pr: https://github.com/mctlhq/mctl-api/pull/99\n",
        )
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is False
        assert state.repo == "mctlhq/mctl-api"
        assert state.number == 99

    async def test_repo_comparison_is_case_insensitive(self, env, monkeypatch):
        """GitHub URLs are case-insensitive — a mixed-case recorded link to
        the proposal's own repo must still be tracked."""
        from orchestrator.temporal.activities.pr_state import get_pr_state

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == self.STATUS_PATH:
                return httpx.Response(
                    200,
                    json=self._status_payload(
                        "pr: https://github.com/MCTLHQ/MCTL-Web/pull/99\n"
                    ),
                )
            if request.url.path == "/repos/MCTLHQ/MCTL-Web/pulls/99":
                return httpx.Response(200, json={"state": "open", "merged": False})
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)
        state = await env.run(get_pr_state, "mctl-web", "issue-10-test")
        assert state.found is True
        assert state.state == "OPEN"

    async def test_directory_contents_payload_raises_listing_error(self, env, monkeypatch):
        """The contents API returns a JSON list for a directory — that must
        surface as a retryable ProposalListingError, not an AttributeError."""
        from orchestrator.temporal.activities.pr_state import get_pr_state
        from orchestrator.temporal.activities.proposals import ProposalListingError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"name": "x", "type": "file"}])

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ProposalListingError, match="payload type"):
            await env.run(get_pr_state, "mctl-web", "issue-10-test")

    async def test_malformed_pulls_json_raises_listing_error(self, env, monkeypatch):
        """A 200 with a non-JSON pulls body (broken proxy) must stay a
        retryable read error, not an unhandled ValueError."""
        from orchestrator.temporal.activities.pr_state import get_pr_state
        from orchestrator.temporal.activities.proposals import ProposalListingError

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == self.STATUS_PATH:
                return httpx.Response(
                    200,
                    json=self._status_payload(
                        "pr: https://github.com/mctlhq/mctl-web/pull/99\n"
                    ),
                )
            return httpx.Response(200, text="<html>gateway</html>")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)
        with pytest.raises(ProposalListingError, match="non-JSON"):
            await env.run(get_pr_state, "mctl-web", "issue-10-test")


class TestResolveDeployTarget:
    """#215: repo → (team, app), the mapping that exists only as dispatch args."""

    PATH = "/repos/mctlhq/mctl-agents/contents/.github/workflows/release-please.yml"

    @staticmethod
    def _contents(body: str) -> dict:
        import base64 as _b64

        return {"content": _b64.b64encode(body.encode()).decode()}

    def _handler(self, monkeypatch, *, body: str | None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == self.PATH:
                if body is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json=self._contents(body))
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)

    async def test_values_path_wins_over_component_name(self, env, monkeypatch):
        """The real mctl-agents case, and the whole reason this is not trivial.

        component_name is `mctl-agents`, but the deployed ArgoCD app is
        `admins-mctl-agents-worker`; asking mctl-api for admins/mctl-agents
        returns "application not found".
        """
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(
            monkeypatch,
            body=(
                "            -f team_name=admins \\\n"
                "            -f component_name=mctl-agents \\\n"
                '            -f values_glob="platform-gitops/argo-workflows/cluster-templates/cwft-*.yaml" \\\n'
                '            -f values_path="platform-gitops/services/admins/mctl-agents-worker/values.yaml"\n'
            ),
        )
        target = await env.run(resolve_deploy_target, "mctlhq/mctl-agents")
        assert target is not None
        assert (target.team, target.app) == ("admins", "mctl-agents-worker")

    async def test_falls_back_to_component_when_values_path_is_not_a_service(
        self, env, monkeypatch
    ):
        """mctl-api bumps a bootstrap template, which names no service dir."""
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(
            monkeypatch,
            body=(
                "            -f team_name=admins \\\n"
                "            -f component_name=mctl-api \\\n"
                '            -f values_path="platform-gitops/bootstrap/templates/mctl-platform/mctl-api.yaml"\n'
            ),
        )
        target = await env.run(resolve_deploy_target, "mctlhq/mctl-agents")
        assert target is not None
        assert (target.team, target.app) == ("admins", "mctl-api")

    async def test_no_dispatch_args_means_no_target(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(monkeypatch, body="name: release-please\non: push\n")
        assert await env.run(resolve_deploy_target, "mctlhq/mctl-agents") is None

    async def test_missing_workflow_file_means_no_target(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(monkeypatch, body=None)
        assert await env.run(resolve_deploy_target, "mctlhq/mctl-agents") is None


class TestGetReleaseAfter:
    PATH = "/repos/mctlhq/mctl-web/releases"

    def _handler(self, monkeypatch, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == self.PATH:
                return httpx.Response(200, json=payload)
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)

    async def test_picks_the_newest_release_after_the_merge(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import get_release_after

        self._handler(
            monkeypatch,
            [
                {"tag_name": "1.0.0", "published_at": "2026-08-29T10:00:00Z"},
                {"tag_name": "1.2.0", "published_at": "2026-08-30T12:00:00Z"},
                {"tag_name": "1.1.0", "published_at": "2026-08-30T09:00:00Z"},
            ],
        )
        release = await env.run(get_release_after, "mctlhq/mctl-web", "2026-08-30T08:00:00Z")
        assert release is not None and release.tag == "1.2.0"

    async def test_a_release_older_than_the_merge_is_not_ours(self, env, monkeypatch):
        """Otherwise every merge would 'observe' the previous release."""
        from orchestrator.temporal.activities.deploy_state import get_release_after

        self._handler(
            monkeypatch, [{"tag_name": "1.0.0", "published_at": "2026-08-29T10:00:00Z"}]
        )
        assert await env.run(get_release_after, "mctlhq/mctl-web", "2026-08-30T08:00:00Z") is None

    async def test_drafts_are_ignored(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import get_release_after

        self._handler(
            monkeypatch,
            [{"tag_name": "1.3.0", "published_at": "2026-08-30T12:00:00Z", "draft": True}],
        )
        assert await env.run(get_release_after, "mctlhq/mctl-web", "2026-08-30T08:00:00Z") is None


class TestGetDeployStatus:
    def _handler(self, monkeypatch, payload, status=200):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/status/admins/mctl-web":
                return httpx.Response(status, json=payload)
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("MCTL_TOKEN", "mctl-test-token")
        _mock_async_client(monkeypatch, handler)

    async def test_reads_tag_health_and_sync(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import get_deploy_status

        self._handler(
            monkeypatch,
            {
                "argocd": {"health": "Healthy", "syncStatus": "Synced"},
                "service": {"imageTag": "7.4.0"},
            },
        )
        status = await env.run(get_deploy_status, "admins", "mctl-web")
        assert (status.found, status.image_tag, status.health, status.sync_status) == (
            True,
            "7.4.0",
            "Healthy",
            "Synced",
        )

    async def test_platform_app_without_a_service_record_has_no_tag(self, env, monkeypatch):
        """mctl-api answers argocd-only for its own platform application."""
        from orchestrator.temporal.activities.deploy_state import get_deploy_status

        self._handler(
            monkeypatch,
            {"argocd": {"health": "Healthy", "syncStatus": "Synced"}, "service": None},
        )
        status = await env.run(get_deploy_status, "admins", "mctl-web")
        assert status.found is True and status.image_tag is None

    async def test_missing_application_is_not_found(self, env, monkeypatch):
        """mctl-api answers 200 with argocd:null and a note, not a 404."""
        from orchestrator.temporal.activities.deploy_state import get_deploy_status

        self._handler(
            monkeypatch, {"argocd": None, "note": "ArgoCD application not found", "service": None}
        )
        status = await env.run(get_deploy_status, "admins", "mctl-web")
        assert status.found is False


class TestDeployTargetPathSafety:
    """agy P1 on #235: the target ends up in an mctl-api URL path.

    release-please.yml is editable by any merged PR, so a crafted
    team/component would otherwise normalise into an authenticated GET
    against an arbitrary internal endpoint — whose body this module quotes
    back in its exception messages.
    """

    PATH = "/repos/mctlhq/evil/contents/.github/workflows/release-please.yml"

    def _handler(self, monkeypatch, body: str):
        import base64 as _b64

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == self.PATH:
                return httpx.Response(
                    200, json={"content": _b64.b64encode(body.encode()).decode()}
                )
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
        _mock_async_client(monkeypatch, handler)

    async def test_traversal_in_dispatch_args_is_refused(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(
            monkeypatch,
            "            -f team_name=.. \\\n            -f component_name=admin/secrets\n",
        )
        assert await env.run(resolve_deploy_target, "mctlhq/evil") is None

    async def test_traversal_in_values_path_is_refused(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(
            monkeypatch,
            '            -f values_path="platform-gitops/services/../admin/values.yaml"\n',
        )
        assert await env.run(resolve_deploy_target, "mctlhq/evil") is None

    async def test_ordinary_names_still_resolve(self, env, monkeypatch):
        from orchestrator.temporal.activities.deploy_state import resolve_deploy_target

        self._handler(
            monkeypatch,
            "            -f team_name=admins \\\n            -f component_name=mctl-api\n",
        )
        target = await env.run(resolve_deploy_target, "mctlhq/evil")
        assert target is not None and (target.team, target.app) == ("admins", "mctl-api")

class TestListServiceIncidents:
    """#216: scoped incident query — service + window, nothing stronger."""

    def _handler(self, monkeypatch, payload, status=200, capture=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/incidents":
                if capture is not None:
                    capture.update(dict(request.url.params))
                return httpx.Response(status, json=payload)
            raise AssertionError(f"unexpected request: {request.url}")

        monkeypatch.setenv("MCTL_TOKEN", "mctl-test-token")
        _mock_async_client(monkeypatch, handler)

    async def test_scopes_the_query_to_service_and_window(self, env, monkeypatch):
        from orchestrator.temporal.activities.incidents import list_service_incidents

        params: dict = {}
        self._handler(monkeypatch, {"items": [], "count": 0}, capture=params)
        await env.run(list_service_incidents, "mctl-web", "2026-08-30T00:00:00Z")
        assert params["service"] == "mctl-web"
        assert params["since"] == "2026-08-30T00:00:00Z"

    async def test_maps_incident_fields(self, env, monkeypatch):
        from orchestrator.temporal.activities.incidents import list_service_incidents

        self._handler(
            monkeypatch,
            {
                "items": [
                    {
                        "id": "alert-7",
                        "title": "pods crashlooping",
                        "severity": "critical",
                        "status": "firing",
                        "started_at": "2026-08-30T00:05:00Z",
                    }
                ],
                "count": 1,
            },
        )
        result = await env.run(list_service_incidents, "mctl-web", "2026-08-30T00:00:00Z")
        assert len(result.incidents) == 1
        assert result.incidents[0].id == "alert-7"
        assert result.incidents[0].severity == "critical"

    async def test_falls_back_to_fingerprint_when_there_is_no_id(self, env, monkeypatch):
        from orchestrator.temporal.activities.incidents import list_service_incidents

        self._handler(
            monkeypatch, {"items": [{"fingerprint": "fp-1", "title": "x"}], "count": 1}
        )
        result = await env.run(list_service_incidents, "mctl-web", "2026-08-30T00:00:00Z")
        assert [i.id for i in result.incidents] == ["fp-1"]

    async def test_unexpected_items_shape_is_an_error_not_a_clean_window(
        self, env, monkeypatch
    ):
        """Reporting "no incidents" for a changed payload would be a lie."""
        from orchestrator.temporal.activities.incidents import list_service_incidents

        self._handler(monkeypatch, {"items": {"unexpected": True}})
        with pytest.raises(Exception, match="incidents items"):
            await env.run(list_service_incidents, "mctl-web", "2026-08-30T00:00:00Z")
