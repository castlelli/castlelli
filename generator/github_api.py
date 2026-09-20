"""GitHub API client for fetching user stats and language data."""

import datetime
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# contributionsCollection covers at most one year per call, so an all-time
# total is built from contiguous windows of this length.
WINDOW_DAYS = 365


class StatsFetchError(Exception):
    """Raised when stats could not be fetched.

    Callers must not substitute zeros: committing a telemetry card full of
    zeros is worse than failing the run, because it looks like real data.
    """


class GitHubAPI:
    """Fetches GitHub stats via GraphQL (with token) or REST (fallback)."""

    GRAPHQL_URL = "https://api.github.com/graphql"
    REST_URL = "https://api.github.com"

    def __init__(self, username: str, token: str = None):
        self.username = username
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an HTTP request with rate-limit awareness and retry.

        Checks X-RateLimit-Remaining after each response.
        On 403 rate-limit, waits until reset and retries once.
        """
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", 15)

        resp = requests.request(method, url, **kwargs)

        # Check rate limit headers
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 10:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
            logger.warning(
                "GitHub API rate limit low: %s remaining (resets at %s)",
                remaining,
                time.strftime("%H:%M:%S", time.localtime(reset_ts)),
            )

        # Retry once on rate-limit 403
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
            wait = max(reset_ts - int(time.time()), 1)
            logger.warning("Rate limited. Waiting %ds for reset...", wait)
            time.sleep(wait)
            resp = requests.request(method, url, **kwargs)

        return resp

    def fetch_stats(self) -> dict:
        """Fetch user statistics. Uses GraphQL if token available, REST otherwise.

        Raises:
            StatsFetchError: if the stats could not be fetched. A token that
                is present but produces a GraphQL failure is a real bug, not a
                transient hiccup, so it is never papered over with public-only
                REST data or with zeros.
        """
        if self.token:
            return self._fetch_stats_graphql()
        try:
            return self._fetch_stats_rest()
        except (requests.exceptions.RequestException, ValueError, KeyError) as e:
            raise StatsFetchError(f"REST stats fetch failed: {e}") from e

    def _graphql(self, query: str) -> dict:
        """POST a GraphQL query for self.username and return its `data.user`.

        Raises StatsFetchError on any transport failure, on a response
        carrying `errors`, or on a missing user, so the caller fails loudly.
        """
        try:
            resp = self._request(
                "POST",
                self.GRAPHQL_URL,
                json={"query": query, "variables": {"username": self.username}},
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise StatsFetchError(f"GraphQL request failed: {e}") from e

        try:
            data = resp.json()
        except ValueError as e:
            raise StatsFetchError(f"GraphQL response was not valid JSON: {e}") from e

        if data.get("errors"):
            messages = "; ".join(
                str(err.get("message", err)) for err in data["errors"]
            )
            raise StatsFetchError(f"GraphQL query returned errors: {messages}")

        user = (data.get("data") or {}).get("user")
        if not user:
            raise StatsFetchError(
                f"GraphQL returned no user data for '{self.username}'."
            )
        return user

    def _fetch_stats_graphql(self) -> dict:
        """Fetch stats via GraphQL for accurate counts including private data.

        The privacy argument is deliberately omitted everywhere: its type is
        RepositoryPrivacy, whose only values are PUBLIC and PRIVATE. Omitting
        it means "no filter", which is what includes private repos.
        """
        query = """
                query($username: String!) {
                    user(login: $username) {
                        createdAt
                        pullRequests {
                            totalCount
                        }
                        issues {
                            totalCount
                        }
                        repositories(ownerAffiliations: OWNER, first: 100) {
                            totalCount
                            nodes {
                                stargazerCount
                                isFork
                            }
                        }
                    }
                }
                """
        user = self._graphql(query)
        repos = user["repositories"]

        # Only count stars for non-fork repositories. Private repos are included
        # because the query applies no privacy filter.
        total_stars = sum(n["stargazerCount"] for n in repos["nodes"] if not n.get("isFork"))

        return {
            "commits": self._fetch_commits_all_time(user["createdAt"]),
            "stars": total_stars,
            "prs": user["pullRequests"]["totalCount"],
            "issues": user["issues"]["totalCount"],
            "repos": repos["totalCount"],
        }

    @staticmethod
    def _contribution_windows(created_at: str, now: datetime.datetime = None) -> list:
        """Split account lifetime into contiguous <=1 year (from, to) windows."""
        start = datetime.datetime.strptime(
            created_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
        now = now or datetime.datetime.now(datetime.timezone.utc)

        windows = []
        cursor = start
        while cursor < now:
            end = min(cursor + datetime.timedelta(days=WINDOW_DAYS), now)
            windows.append((cursor, end))
            cursor = end
        return windows

    def _fetch_commits_all_time(self, created_at: str) -> int:
        """Sum commit contributions over every year since the account was made.

        contributionsCollection with no from/to covers only the last year, so
        one aliased window per year is requested in a single query.

        restrictedContributionsCount is deliberately not added: it counts every
        private contribution type (PRs, issues, reviews), not just commits, and
        a token that can see the private repos already counts those commits in
        totalCommitContributions.
        """
        windows = self._contribution_windows(created_at)
        if not windows:
            logger.warning("Account creation date %s is not in the past.", created_at)
            return 0

        blocks = "\n".join(
            f'                        w{i}: contributionsCollection('
            f'from: "{frm:%Y-%m-%dT%H:%M:%SZ}", to: "{to:%Y-%m-%dT%H:%M:%SZ}") {{\n'
            f"                            totalCommitContributions\n"
            f"                        }}"
            for i, (frm, to) in enumerate(windows)
        )
        query = (
            "\n                query($username: String!) {\n"
            "                    user(login: $username) {\n"
            f"{blocks}\n"
            "                    }\n"
            "                }\n                "
        )

        user = self._graphql(query)
        total = sum(
            user[f"w{i}"]["totalCommitContributions"] for i in range(len(windows))
        )
        logger.info(
            "All-time commits: %d across %d yearly window(s) since %s",
            total,
            len(windows),
            created_at,
        )
        return total

    def _fetch_stats_rest(self) -> dict:
        """Fallback: fetch stats via REST API (public data only)."""
        user_resp = self._request(
            "GET", f"{self.REST_URL}/users/{self.username}"
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()

        # Fetch repos to count stars
        total_stars = 0
        for repos in self._paginate_repos():
            total_stars += sum(r.get("stargazers_count", 0) for r in repos)

        # Count public commits via the Search API. The Events API no longer
        # returns PushEvent.payload.commits, so the old estimate that summed
        # those arrays could only ever produce 0.
        commit_count = self._search_count(f"author:{self.username}", kind="commits")

        # Fetch actual PR count via Search API
        pr_count = self._search_count(f"author:{self.username} type:pr")

        # Fetch actual issue count via Search API
        issue_count = self._search_count(f"author:{self.username} type:issue")

        return {
            "commits": commit_count,
            "stars": total_stars,
            "prs": pr_count,
            "issues": issue_count,
            "repos": user_data.get("public_repos", 0),
        }

    def _paginate_repos(self):
        """Yield pages of owned repos from the REST API."""
        page = 1
        # Cache authenticated username when possible
        if self.token and not hasattr(self, "_auth_user_fetched"):
            try:
                resp = self._request("GET", f"{self.REST_URL}/user")
                if resp.status_code == 200:
                    self._auth_user = resp.json().get("login")
                else:
                    self._auth_user = None
            except Exception:
                self._auth_user = None
            self._auth_user_fetched = True
        while True:
            # If we have a token and it belongs to the requested user, use
            # the authenticated endpoint which can return private repos.
            if getattr(self, "_auth_user", None) == self.username:
                url = f"{self.REST_URL}/user/repos"
                params = {"per_page": 100, "page": page, "affiliation": "owner", "visibility": "all"}
            else:
                url = f"{self.REST_URL}/users/{self.username}/repos"
                params = {"per_page": 100, "page": page, "type": "owner"}

            repos_resp = self._request("GET", url, params=params)
            repos_resp.raise_for_status()
            repos = repos_resp.json()
            if not repos:
                break
            yield repos
            if len(repos) < 100:
                break
            page += 1

    def _search_count(self, query: str, kind: str = "issues") -> int:
        """Use the GitHub Search API to get a total_count for a query."""
        try:
            resp = self._request(
                "GET",
                f"{self.REST_URL}/search/{kind}",
                params={"q": query, "per_page": 1},
            )
            if resp.status_code == 200:
                return resp.json().get("total_count", 0)
            logger.warning("Search API returned %d for query '%s'", resp.status_code, query)
        except requests.exceptions.RequestException as e:
            logger.warning("Search API failed for '%s': %s", query, e)
        return 0

    def fetch_languages(self) -> dict:
        """Fetch language byte counts aggregated across all owned non-fork repos."""
        languages = {}
        for repos in self._paginate_repos():
            for repo in repos:
                if repo.get("fork"):
                    continue
                try:
                    lang_resp = self._request("GET", repo["languages_url"])
                    if lang_resp.status_code == 200:
                        for lang, bytes_count in lang_resp.json().items():
                            languages[lang] = languages.get(lang, 0) + bytes_count
                    else:
                        logger.warning(
                            "Could not fetch languages for %s (HTTP %d)",
                            repo.get("full_name", "unknown"),
                            lang_resp.status_code,
                        )
                except requests.exceptions.RequestException as e:
                    logger.warning(
                        "Error fetching languages for %s: %s",
                        repo.get("full_name", "unknown"),
                        e,
                    )
        return languages
