"""Tests for the GitHub API client: query correctness and loud failures."""

import datetime

import pytest
import requests

from generator.github_api import WINDOW_DAYS, GitHubAPI, StatsFetchError

STATS_KEYS = {"commits", "stars", "prs", "issues", "repos"}


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text
        self.headers = {"X-RateLimit-Remaining": "5000", "X-RateLimit-Reset": "0"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Error")


@pytest.fixture
def router(monkeypatch):
    """Patch requests.request with a recording router keyed on url/query."""
    calls = []

    def handler(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        for match, response in handler.routes:
            if match(url, kwargs):
                return response() if callable(response) else response
        raise AssertionError(f"unrouted request: {method} {url}")

    handler.routes = []
    handler.calls = calls
    handler.add = lambda match, response: handler.routes.append((match, response))
    monkeypatch.setattr("generator.github_api.requests.request", handler)
    return handler


def graphql_route(handler, base_payload, commits_payload):
    """Route the base stats query and the aliased commits query separately."""
    handler.add(
        lambda url, kw: "graphql" in url
        and "contributionsCollection" in kw["json"]["query"],
        FakeResponse(commits_payload),
    )
    handler.add(lambda url, kw: "graphql" in url, FakeResponse(base_payload))


BASE_OK = {
    "data": {
        "user": {
            "createdAt": "2024-02-20T13:45:53Z",
            "pullRequests": {"totalCount": 24},
            "issues": {"totalCount": 34},
            "repositories": {
                "totalCount": 29,
                "nodes": [
                    {"stargazerCount": 7, "isFork": False},
                    {"stargazerCount": 99, "isFork": True},
                    {"stargazerCount": 3, "isFork": False},
                ],
            },
        }
    }
}

COMMITS_OK = {
    "data": {
        "user": {
            "w0": {"totalCommitContributions": 40},
            "w1": {"totalCommitContributions": 60},
            "w2": {"totalCommitContributions": 25},
        }
    }
}


# --- Regression guards for the invalid-enum root cause -------------------


def test_graphql_query_uses_no_privacy_argument(router):
    """privacy: ALL is not a RepositoryPrivacy value; omitting it means 'all'."""
    graphql_route(router, BASE_OK, COMMITS_OK)
    GitHubAPI("octocat", token="t").fetch_stats()

    queries = [c["kwargs"]["json"]["query"] for c in router.calls if "graphql" in c["url"]]
    assert queries, "no GraphQL request was made"
    for query in queries:
        assert "privacy:" not in query
        assert "ALL" not in query


def test_graphql_query_drops_unused_repositories_contributed_to(router):
    """The field was fetched but never read, and carried an invalid argument."""
    graphql_route(router, BASE_OK, COMMITS_OK)
    GitHubAPI("octocat", token="t").fetch_stats()

    base = next(
        c["kwargs"]["json"]["query"]
        for c in router.calls
        if "graphql" in c["url"] and "contributionsCollection" not in c["kwargs"]["json"]["query"]
    )
    assert "repositoriesContributedTo" not in base


def test_graphql_success_returns_expected_stats(router):
    graphql_route(router, BASE_OK, COMMITS_OK)
    stats = GitHubAPI("octocat", token="t").fetch_stats()

    assert set(stats) == STATS_KEYS
    assert stats["commits"] == 125  # 40 + 60 + 25, summed across yearly windows
    assert stats["stars"] == 10  # 7 + 3; the 99-star fork is excluded
    assert stats["prs"] == 24
    assert stats["issues"] == 34
    assert stats["repos"] == 29


def test_restricted_contributions_count_not_requested(router):
    """It counts every private contribution type, not just commits."""
    graphql_route(router, BASE_OK, COMMITS_OK)
    GitHubAPI("octocat", token="t").fetch_stats()

    for call in router.calls:
        if "graphql" in call["url"]:
            assert "restrictedContributionsCount" not in call["kwargs"]["json"]["query"]


# --- Loud failures instead of silent degradation ------------------------


def test_graphql_errors_raise_with_message(router):
    router.add(
        lambda url, kw: "graphql" in url,
        FakeResponse({"errors": [{"message": "Expected type RepositoryPrivacy, found ALL."}]}),
    )
    with pytest.raises(StatsFetchError) as excinfo:
        GitHubAPI("octocat", token="t").fetch_stats()

    assert "RepositoryPrivacy" in str(excinfo.value)


def test_graphql_errors_do_not_fall_back_to_rest(router):
    """A token that fails GraphQL is a bug, not a reason to use public data."""
    router.add(lambda url, kw: "graphql" in url, FakeResponse({"errors": [{"message": "boom"}]}))
    with pytest.raises(StatsFetchError):
        GitHubAPI("octocat", token="t").fetch_stats()

    assert all("graphql" in c["url"] for c in router.calls), "REST fallback was used"


def test_graphql_http_error_raises(router):
    router.add(lambda url, kw: "graphql" in url, FakeResponse({}, status_code=401))
    with pytest.raises(StatsFetchError):
        GitHubAPI("octocat", token="t").fetch_stats()


def test_graphql_missing_user_raises(router):
    router.add(lambda url, kw: "graphql" in url, FakeResponse({"data": {"user": None}}))
    with pytest.raises(StatsFetchError) as excinfo:
        GitHubAPI("ghost", token="t").fetch_stats()

    assert "ghost" in str(excinfo.value)


def test_graphql_non_json_raises(router):
    router.add(lambda url, kw: "graphql" in url, FakeResponse(ValueError("not json")))
    with pytest.raises(StatsFetchError):
        GitHubAPI("octocat", token="t").fetch_stats()


def test_rest_failure_raises_instead_of_returning_zeros(router):
    router.add(lambda url, kw: "/users/octocat" in url, FakeResponse({}, status_code=500))
    with pytest.raises(StatsFetchError):
        GitHubAPI("octocat", token="").fetch_stats()


# --- Contribution windows ----------------------------------------------


def test_windows_cover_lifetime_contiguously():
    now = datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc)
    windows = GitHubAPI._contribution_windows("2024-02-20T13:45:53Z", now)

    assert len(windows) == 3
    assert windows[0][0].year == 2024
    assert windows[-1][1] == now
    for frm, to in windows:
        assert (to - frm).days <= WINDOW_DAYS
    for (_, prev_to), (next_from, _) in zip(windows, windows[1:]):
        assert prev_to == next_from, "windows must not overlap or leave gaps"


def test_windows_for_account_younger_than_one_year():
    now = datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc)
    windows = GitHubAPI._contribution_windows("2026-06-01T00:00:00Z", now)

    assert len(windows) == 1


def test_commits_query_requests_one_alias_per_window(router):
    graphql_route(router, BASE_OK, COMMITS_OK)
    GitHubAPI("octocat", token="t").fetch_stats()

    query = next(
        c["kwargs"]["json"]["query"]
        for c in router.calls
        if "graphql" in c["url"] and "contributionsCollection" in c["kwargs"]["json"]["query"]
    )
    # An unbounded contributionsCollection would silently mean "last year only".
    assert "from:" in query and "to:" in query
    assert "w0:" in query


# --- REST fallback commit counting -------------------------------------


def test_rest_counts_commits_via_search_not_events(router):
    """PushEvent payloads no longer carry a commits array, so it can only be 0."""
    router.add(lambda url, kw: url.endswith("/users/octocat"), FakeResponse({"public_repos": 6}))
    router.add(lambda url, kw: "/repos" in url, FakeResponse([]))
    router.add(
        lambda url, kw: "/search/commits" in url, FakeResponse({"total_count": 107})
    )
    router.add(lambda url, kw: "/search/issues" in url, FakeResponse({"total_count": 14}))

    stats = GitHubAPI("octocat", token="").fetch_stats()

    assert stats["commits"] == 107
    assert not any("/events/" in c["url"] for c in router.calls), (
        "the dead Events API estimate is still being used"
    )
    assert set(stats) == STATS_KEYS


def test_no_token_uses_rest_and_token_uses_graphql(router):
    graphql_route(router, BASE_OK, COMMITS_OK)
    GitHubAPI("octocat", token="t").fetch_stats()
    assert all("graphql" in c["url"] for c in router.calls)
