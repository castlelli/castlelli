"""Tests for the GitLab API client: attribution correctness and loud failures."""

import logging

import pytest
import requests

from generator.gitlab_api import GitLabAPI, GitLabStatsError

STATS_KEYS = {"commits", "stars", "prs", "issues", "repos"}

HOST = "https://gitlab.example.org"
MY_EMAILS = ["vcastelli@usp.br", "castellivinicius07@gmail.com"]


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = {"RateLimit-Remaining": "1000", "RateLimit-Reset": "0"}
        if headers:
            self.headers.update(headers)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def router(monkeypatch):
    """Patch requests.request with a recording router keyed on url/params."""
    calls = []

    def handler(method, url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append({"method": method, "url": url, "params": params})
        for match, response in handler.routes:
            if match(url, params):
                return response(url, params) if callable(response) else response
        raise AssertionError(f"unrouted request: {method} {url} {params}")

    handler.routes = []
    handler.calls = calls
    handler.add = lambda match, response: handler.routes.append((match, response))
    monkeypatch.setattr("generator.gitlab_api.requests.request", handler)
    return handler


def api(**kwargs):
    kwargs.setdefault("host", HOST)
    kwargs.setdefault("username", "vcastelli")
    kwargs.setdefault("emails", MY_EMAILS)
    kwargs.setdefault("token", "t")
    return GitLabAPI(**kwargs)


def commit(name, email):
    return {"author_name": name, "author_email": email}


PROJECTS = [
    {"id": 1, "path_with_namespace": "grp/alpha", "star_count": 3, "empty_repo": False},
    {"id": 2, "path_with_namespace": "grp/beta", "star_count": 4, "empty_repo": False},
]

# Deliberately spans three author NAMES over two emails, plus a mixed-case
# address, because that is what the real instance looks like.
COMMITS = {
    1: [
        commit("castelli", "castellivinicius07@gmail.com"),
        commit("Castelli", "vcastelli@usp.br"),
        commit("Someone Else", "other@example.com"),
    ],
    2: [commit("vcastelli", "VCastelli@USP.BR")],
}


def standard_routes(router, projects=PROJECTS, commits=COMMITS, mr_total="8", issue_total="2"):
    """Wire up a full happy-path instance."""
    router.add(lambda u, p: u.endswith("/users"), FakeResponse([{"id": 34961}]))

    def commits_response(url, params):
        pid = int(url.rstrip("/").split("/projects/")[1].split("/")[0])
        page = int(params.get("page", 1))
        batch = commits.get(pid, []) if page == 1 else []
        return FakeResponse(batch)

    router.add(lambda u, p: "/repository/commits" in u, commits_response)
    router.add(
        lambda u, p: u.endswith("/merge_requests"),
        FakeResponse([], headers={"X-Total": mr_total}),
    )
    router.add(
        lambda u, p: u.endswith("/issues"),
        FakeResponse([], headers={"X-Total": issue_total}),
    )
    router.add(
        lambda u, p: u.endswith("/projects"),
        lambda u, p: FakeResponse(projects if int(p.get("page", 1)) == 1 else []),
    )


# --- happy path ---------------------------------------------------------


def test_fetch_stats_returns_the_github_key_set(router):
    standard_routes(router)
    stats = api().fetch_stats()
    assert set(stats) == STATS_KEYS


def test_fetch_stats_values(router):
    standard_routes(router)
    stats = api().fetch_stats()

    assert stats["commits"] == 3   # 2 in alpha + 1 in beta; other author ignored
    assert stats["stars"] == 7     # 3 + 4 summed over projects
    assert stats["prs"] == 8       # from X-Total
    assert stats["issues"] == 2    # from X-Total
    assert stats["repos"] == 2


def test_commits_attributed_by_email_not_name(router):
    """Three author names over two emails must all count."""
    standard_routes(router)
    assert api().fetch_stats()["commits"] == 3


def test_email_matching_is_case_insensitive(router):
    standard_routes(router)
    # beta's single commit uses VCastelli@USP.BR
    assert api().fetch_stats()["commits"] == 3


def test_commits_walk_all_branches(router):
    """all=true is what makes squash-merged branch work countable."""
    standard_routes(router)
    api().fetch_stats()

    commit_calls = [c for c in router.calls if "/repository/commits" in c["url"]]
    assert commit_calls, "no commits request was made"
    for call in commit_calls:
        assert call["params"].get("all") == "true"


def test_membership_is_used_by_default(router):
    standard_routes(router)
    api().fetch_stats()

    project_calls = [c for c in router.calls if c["url"].endswith("/projects")]
    assert project_calls[0]["params"].get("membership") == "true"
    assert "owned" not in project_calls[0]["params"]


def test_owned_only_when_membership_disabled(router):
    standard_routes(router)
    api(include_membership=False).fetch_stats()

    project_calls = [c for c in router.calls if c["url"].endswith("/projects")]
    assert project_calls[0]["params"].get("owned") == "true"
    assert "membership" not in project_calls[0]["params"]


def test_empty_repo_is_skipped(router):
    projects = [
        {"id": 1, "path_with_namespace": "grp/alpha", "star_count": 1, "empty_repo": True},
    ]
    standard_routes(router, projects=projects, commits={})
    stats = api().fetch_stats()

    assert stats["commits"] == 0
    assert not any("/repository/commits" in c["url"] for c in router.calls)


def test_projects_are_paginated(router):
    """A full page must trigger a request for the next one."""
    page1 = [
        {"id": i, "path_with_namespace": f"grp/p{i}", "star_count": 1, "empty_repo": True}
        for i in range(100)
    ]
    page2 = [
        {"id": 999, "path_with_namespace": "grp/last", "star_count": 5, "empty_repo": True}
    ]
    router.add(lambda u, p: u.endswith("/users"), FakeResponse([{"id": 1}]))
    router.add(lambda u, p: u.endswith("/merge_requests"), FakeResponse([], headers={"X-Total": "0"}))
    router.add(lambda u, p: u.endswith("/issues"), FakeResponse([], headers={"X-Total": "0"}))
    router.add(
        lambda u, p: u.endswith("/projects"),
        lambda u, p: FakeResponse({1: page1, 2: page2}.get(int(p.get("page", 1)), [])),
    )

    stats = api().fetch_stats()
    assert stats["repos"] == 101
    assert stats["stars"] == 105


# --- no silent failures -------------------------------------------------


def test_missing_x_total_raises_instead_of_estimating(router):
    """Some GitLab endpoints omit X-Total; guessing a total is not acceptable."""
    standard_routes(router)
    router.routes = [
        (m, r) for m, r in router.routes if not m("https://x/merge_requests", {})
    ]
    router.add(lambda u, p: u.endswith("/merge_requests"), FakeResponse([]))

    with pytest.raises(GitLabStatsError, match="X-Total"):
        api().fetch_stats()


def test_no_token_raises(router):
    with pytest.raises(GitLabStatsError, match="GITLAB_TOKEN"):
        api(token="").fetch_stats()
    assert router.calls == [], "no request should be attempted without a token"


def test_empty_emails_raises(router):
    """Without an email list every commit would be unattributable, i.e. 0."""
    with pytest.raises(GitLabStatsError, match="emails"):
        api(emails=[]).fetch_stats()
    assert router.calls == []


def test_unknown_user_raises(router):
    router.add(lambda u, p: u.endswith("/users"), FakeResponse([]))
    with pytest.raises(GitLabStatsError, match="vcastelli"):
        api().fetch_stats()


def test_http_error_raises(router):
    router.add(lambda u, p: u.endswith("/users"), FakeResponse([], status_code=500))
    with pytest.raises(GitLabStatsError, match="HTTP 500"):
        api().fetch_stats()


def test_invalid_json_raises(router):
    router.add(lambda u, p: u.endswith("/users"), FakeResponse(ValueError("nope")))
    with pytest.raises(GitLabStatsError, match="invalid JSON"):
        api().fetch_stats()


def test_transport_error_raises(router):
    def boom(method, url, **kwargs):
        raise requests.exceptions.ConnectTimeout("unreachable")

    router.routes = []
    import generator.gitlab_api as mod

    mod.requests.request = boom
    with pytest.raises(GitLabStatsError, match="failed"):
        api().fetch_stats()


def test_unlisted_author_alias_is_warned_about(router, caplog):
    """A third git identity must not vanish quietly."""
    commits = {1: [commit("vcastelli", "third-address@example.com")], 2: []}
    standard_routes(router, commits=commits)

    with caplog.at_level(logging.WARNING):
        stats = api().fetch_stats()

    assert stats["commits"] == 0
    assert "third-address@example.com" in caplog.text
    assert "NOT counted" in caplog.text


def test_commit_page_cap_warns(router, caplog):
    standard_routes(router)
    with caplog.at_level(logging.WARNING):
        api(max_commit_pages=1).fetch_stats()

    assert "cap" in caplog.text.lower()


def test_token_value_is_never_logged(router, caplog):
    standard_routes(router)
    secret = "glpat-SUPERSECRETVALUE"
    with caplog.at_level(logging.DEBUG):
        api(token=secret).fetch_stats()

    assert secret not in caplog.text


# --- transient network resilience ---------------------------------------


def test_transient_timeout_is_retried_once(monkeypatch, caplog):
    """A single read timeout must not cost the whole GitLab fetch."""
    standard = {"calls": 0}

    def flaky(method, url, **kwargs):
        standard["calls"] += 1
        if standard["calls"] == 1:
            raise requests.exceptions.ReadTimeout("blip")
        return FakeResponse([{"id": 7}])

    monkeypatch.setattr("generator.gitlab_api.requests.request", flaky)
    with caplog.at_level(logging.WARNING):
        assert GitLabAPI(HOST, "vcastelli", MY_EMAILS, token="t")._resolve_user_id() == 7

    assert standard["calls"] == 2
    assert "retrying once" in caplog.text


def test_second_failure_propagates(monkeypatch):
    """Retry is one attempt, not an infinite loop."""
    calls = {"n": 0}

    def always_down(method, url, **kwargs):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("still down")

    monkeypatch.setattr("generator.gitlab_api.requests.request", always_down)
    with pytest.raises(GitLabStatsError, match="failed"):
        GitLabAPI(HOST, "vcastelli", MY_EMAILS, token="t").fetch_stats()

    assert calls["n"] == 2
