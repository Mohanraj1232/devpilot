"""Tests for the GitHub REST client (mocked transport via respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ai_hub.errors import AuthError, FailureReason
from ai_hub.github.client import GitHubClient, GitHubError

API = "https://api.github.test"


def _client(sleeps: list[float] | None = None) -> GitHubClient:
    log = sleeps if sleeps is not None else []
    return GitHubClient("tok", "octo/repo", api_url=API, sleep=log.append, max_retries=2)


def test_requires_token() -> None:
    with pytest.raises(AuthError) as exc:
        GitHubClient("", "octo/repo")
    assert exc.value.reason == FailureReason.TOKEN_MISSING


def test_rejects_malformed_repo_name() -> None:
    with pytest.raises(GitHubError):
        GitHubClient("tok", "not-a-repo")


@respx.mock
def test_sends_auth_header() -> None:
    route = respx.get(f"{API}/repos/octo/repo").mock(
        return_value=httpx.Response(200, json={"full_name": "octo/repo"})
    )
    _client().get_repo()
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"


class TestIssues:
    @respx.mock
    def test_get_issue(self) -> None:
        respx.get(f"{API}/repos/octo/repo/issues/7").mock(
            return_value=httpx.Response(200, json={"number": 7, "title": "t"})
        )
        assert _client().get_issue(7) == {"number": 7, "title": "t"}

    @respx.mock
    @pytest.mark.parametrize("status", [404, 410])
    def test_deleted_issue_is_none(self, status: int) -> None:
        respx.get(f"{API}/repos/octo/repo/issues/7").mock(return_value=httpx.Response(status))
        assert _client().get_issue(7) is None

    @respx.mock
    def test_comment_and_labels(self) -> None:
        comment = respx.post(f"{API}/repos/octo/repo/issues/7/comments").mock(
            return_value=httpx.Response(201, json={})
        )
        labels = respx.post(f"{API}/repos/octo/repo/issues/7/labels").mock(
            return_value=httpx.Response(200, json=[])
        )
        c = _client()
        c.add_issue_comment(7, "hello")
        c.add_labels(7, ["devpilot:in-progress"])
        assert comment.calls[0].request.content == b'{"body":"hello"}'
        assert b"devpilot:in-progress" in labels.calls[0].request.content

    @respx.mock
    def test_removing_missing_label_is_ok(self) -> None:
        respx.delete(f"{API}/repos/octo/repo/issues/7/labels/nope").mock(
            return_value=httpx.Response(404)
        )
        _client().remove_label(7, "nope")


class TestErrors:
    @respx.mock
    def test_401_is_auth_failure(self) -> None:
        respx.get(f"{API}/repos/octo/repo").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        with pytest.raises(GitHubError) as exc:
            _client().get_repo()
        assert exc.value.reason == FailureReason.AUTH_FAILURE
        assert exc.value.status_code == 401

    @respx.mock
    def test_403_is_permission_denied(self) -> None:
        respx.get(f"{API}/repos/octo/repo").mock(
            return_value=httpx.Response(403, json={"message": "Resource not accessible"})
        )
        with pytest.raises(GitHubError) as exc:
            _client().get_repo()
        assert exc.value.reason == FailureReason.PERMISSION_DENIED

    @respx.mock
    def test_404_repo_is_inaccessible(self) -> None:
        respx.get(f"{API}/repos/octo/repo").mock(return_value=httpx.Response(404))
        with pytest.raises(GitHubError) as exc:
            _client().get_repo()
        assert exc.value.reason == FailureReason.REPO_INACCESSIBLE

    @respx.mock
    def test_error_message_is_redacted(self) -> None:
        leaked = "ghp_" + "z" * 36
        respx.get(f"{API}/repos/octo/repo").mock(
            return_value=httpx.Response(401, json={"message": f"bad token {leaked}"})
        )
        with pytest.raises(GitHubError) as exc:
            _client().get_repo()
        assert leaked not in exc.value.message

    @respx.mock
    def test_422_exposes_status_code(self) -> None:
        respx.post(f"{API}/repos/octo/repo/pulls").mock(
            return_value=httpx.Response(422, json={"message": "A pull request already exists"})
        )
        with pytest.raises(GitHubError) as exc:
            _client().create_pull_request(title="t", body="b", head="h", base="main")
        assert exc.value.status_code == 422


class TestRetries:
    @respx.mock
    def test_retries_server_errors_then_succeeds(self) -> None:
        route = respx.get(f"{API}/repos/octo/repo").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(503),
                httpx.Response(200, json={"ok": 1}),
            ]
        )
        sleeps: list[float] = []
        assert _client(sleeps).get_repo() == {"ok": 1}
        assert route.call_count == 3
        assert len(sleeps) == 2

    @respx.mock
    def test_gives_up_after_max_retries(self) -> None:
        route = respx.get(f"{API}/repos/octo/repo").mock(return_value=httpx.Response(500))
        with pytest.raises(GitHubError):
            _client().get_repo()
        assert route.call_count == 3  # first try + 2 retries

    @respx.mock
    def test_transport_errors_are_retried(self) -> None:
        route = respx.get(f"{API}/repos/octo/repo").mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, json={"ok": 1})]
        )
        assert _client().get_repo() == {"ok": 1}
        assert route.call_count == 2

    @respx.mock
    def test_primary_rate_limit_waits_then_retries(self) -> None:
        route = respx.get(f"{API}/repos/octo/repo").mock(
            side_effect=[
                httpx.Response(
                    403, headers={"x-ratelimit-remaining": "0", "retry-after": "5"}, json={}
                ),
                httpx.Response(200, json={"ok": 1}),
            ]
        )
        sleeps: list[float] = []
        assert _client(sleeps).get_repo() == {"ok": 1}
        assert sleeps == [5.0]
        assert route.call_count == 2

    @respx.mock
    def test_rate_limit_longer_than_cap_fails_fast(self) -> None:
        respx.get(f"{API}/repos/octo/repo").mock(
            return_value=httpx.Response(429, headers={"retry-after": "3600"}, json={})
        )
        sleeps: list[float] = []
        with pytest.raises(GitHubError) as exc:
            _client(sleeps).get_repo()
        assert exc.value.reason == FailureReason.RATE_LIMITED
        assert sleeps == []


class TestPermissionsAndBranches:
    @respx.mock
    def test_collaborator_permission(self) -> None:
        respx.get(f"{API}/repos/octo/repo/collaborators/bot/permission").mock(
            return_value=httpx.Response(200, json={"permission": "write"})
        )
        assert _client().get_collaborator_permission("bot") == "write"

    @respx.mock
    def test_non_collaborator_is_none(self) -> None:
        respx.get(f"{API}/repos/octo/repo/collaborators/bot/permission").mock(
            return_value=httpx.Response(404)
        )
        assert _client().get_collaborator_permission("bot") == "none"

    @respx.mock
    def test_branch_sha(self) -> None:
        respx.get(f"{API}/repos/octo/repo/git/ref/heads/feature/x").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "abc"}})
        )
        assert _client().get_branch_sha("feature/x") == "abc"

    @respx.mock
    def test_missing_branch_is_none(self) -> None:
        respx.get(f"{API}/repos/octo/repo/git/ref/heads/nope").mock(
            return_value=httpx.Response(404)
        )
        assert _client().get_branch_sha("nope") is None


class TestPullRequests:
    @staticmethod
    def _pr(number: int, ref: str, repo: str = "octo/repo") -> dict[str, object]:
        return {"number": number, "head": {"ref": ref, "repo": {"full_name": repo}}}

    @respx.mock
    def test_list_filters_by_prefix_and_ignores_forks(self) -> None:
        respx.get(f"{API}/repos/octo/repo/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    self._pr(1, "devpilot/issue-7-fix"),
                    self._pr(2, "feature/other"),
                    self._pr(3, "devpilot/issue-7-fork", repo="evil/repo"),
                    {"number": 4, "head": {"ref": "devpilot/issue-7-gone", "repo": None}},
                ],
            )
        )
        found = _client().list_open_pulls(head_prefix="devpilot/issue-7")
        assert [p["number"] for p in found] == [1]

    @respx.mock
    def test_find_open_pull_for_exact_branch(self) -> None:
        respx.get(f"{API}/repos/octo/repo/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[self._pr(1, "devpilot/issue-7-fix-2"), self._pr(2, "devpilot/issue-7-fix")],
            )
        )
        pull = _client().find_open_pull_for_branch("devpilot/issue-7-fix")
        assert pull is not None
        assert pull["number"] == 2

    @respx.mock
    def test_pagination(self) -> None:
        page1 = [self._pr(i, f"b{i}") for i in range(100)]
        route = respx.get(f"{API}/repos/octo/repo/pulls").mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=[self._pr(500, "last")]),
            ]
        )
        assert len(_client().list_open_pulls()) == 101
        assert route.call_count == 2

    @respx.mock
    def test_create_and_update(self) -> None:
        respx.post(f"{API}/repos/octo/repo/pulls").mock(
            return_value=httpx.Response(201, json={"number": 9, "html_url": "u"})
        )
        patch = respx.patch(f"{API}/repos/octo/repo/pulls/9").mock(
            return_value=httpx.Response(200, json={"number": 9})
        )
        c = _client()
        assert c.create_pull_request(title="t", body="b", head="h", base="main")["number"] == 9
        c.update_pull_request(9, title="t2", body="b2")
        assert b"t2" in patch.calls[0].request.content
