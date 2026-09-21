"""GitLab API client for fetching user stats, shaped to match GitHubAPI.

GitLab's REST v4 API differs from GitHub's in ways that drive this design:

* Counts arrive by different routes per metric. Most list endpoints expose a
  total in the ``X-Total`` response header, but the commits endpoint sends no
  total at all, so its pages must be walked.
* There is no "stars received" endpoint. Summing ``star_count`` over the
  user's projects is the only route.
* ``/users/:id/projects`` lists only the user's *personal* namespace. For an
  account whose work lives in group projects it returns an empty list, so
  projects come from ``/projects?membership=true`` instead.

There is deliberately no ``fetch_languages()`` here. ``/projects/:id/languages``
returns **percentages**, while GitHub returns **byte counts**, and
``utils.calculate_language_percentages()`` sums bytes. Feeding percentages into
it would let a tiny GitLab project outweigh a large GitHub one, so language
data stays GitHub-only rather than being silently fabricated.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

PER_PAGE = 100

# The commits endpoint sends no X-Total, so pages are walked until one comes
# back empty. This cap stops a pathologically large project from running the
# job forever; reaching it logs a warning instead of truncating in silence.
MAX_COMMIT_PAGES = 50


class GitLabStatsError(Exception):
    """Raised when GitLab stats could not be fetched.

    Mirrors ``github_api.StatsFetchError``: callers must not substitute zeros,
    because a card of zeros is indistinguishable from genuinely empty activity.
    """


class GitLabAPI:
    """Fetches GitLab stats for a user, returning GitHubAPI's exact key set."""

    def __init__(
        self,
        host: str,
        username: str,
        emails=None,
        token: str = None,
        include_membership: bool = True,
        max_commit_pages: int = MAX_COMMIT_PAGES,
    ):
        self.host = (host or "").rstrip("/")
        self.api_url = f"{self.host}/api/v4"
        self.username = username
        # Commits are attributed by git author *email*, the only stable
        # identity available. The same person routinely appears under several
        # author names (observed on this instance: "castelli", "vcastelli" and
        # "Castelli" across two emails), so matching on name would silently
        # drop a large share of their commits.
        self.emails = {e.strip().lower() for e in (emails or []) if e and e.strip()}
        self.include_membership = include_membership
        self.max_commit_pages = max_commit_pages
        self.token = token or os.environ.get("GITLAB_TOKEN", "")
        self.headers = {"Accept": "application/json"}
        if self.token:
            # Never logged: only the header name ever reaches the log.
            self.headers["PRIVATE-TOKEN"] = self.token

    # --- transport ------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an HTTP request, honouring GitLab's RateLimit-* headers.

        GitLab uses ``RateLimit-Remaining``/``RateLimit-Reset`` (no X- prefix)
        and answers 429 rather than 403 when a bucket is exhausted.
        """
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", 20)
        url = path if path.startswith("http") else f"{self.api_url}{path}"

        try:
            resp = requests.request(method, url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # Transient read timeouts were observed against a real instance on
            # roughly two runs in three. Without one retry a network blip would
            # keep flipping the committed card between the combined totals and
            # "GITHUB ONLY". A second failure propagates and is reported.
            logger.warning("GitLab request to %s failed (%s); retrying once...", path, e)
            resp = requests.request(method, url, **kwargs)

        remaining = resp.headers.get("RateLimit-Remaining")
        if remaining is not None:
            try:
                if int(remaining) < 10:
                    logger.warning(
                        "GitLab rate limit low: %s remaining on bucket %s",
                        remaining,
                        resp.headers.get("RateLimit-Name", "unknown"),
                    )
            except ValueError:
                pass

        if resp.status_code == 429:
            reset_ts = resp.headers.get("RateLimit-Reset")
            try:
                wait = max(int(reset_ts) - int(time.time()), 1)
            except (TypeError, ValueError):
                wait = 5
            wait = min(wait, 60)
            logger.warning("GitLab rate limited. Waiting %ds before one retry...", wait)
            time.sleep(wait)
            resp = requests.request(method, url, **kwargs)

        return resp

    def _get_json(self, path: str, params: dict = None):
        """GET a path and return ``(parsed_body, headers)``, failing loudly."""
        try:
            resp = self._request("GET", path, params=params or {})
        except requests.exceptions.RequestException as e:
            raise GitLabStatsError(f"GET {path} failed: {e}") from e

        if resp.status_code != 200:
            raise GitLabStatsError(f"GET {path} returned HTTP {resp.status_code}.")

        try:
            return resp.json(), resp.headers
        except ValueError as e:
            raise GitLabStatsError(f"GET {path} returned invalid JSON: {e}") from e

    def _count_via_x_total(self, path: str, params: dict) -> int:
        """Return a total from the X-Total header, refusing to guess without it."""
        _, headers = self._get_json(path, {**params, "per_page": 1})
        total = headers.get("X-Total")
        if total is None:
            raise GitLabStatsError(
                f"GET {path} returned no X-Total header, so its total is "
                "unknown; refusing to substitute an estimate."
            )
        try:
            return int(total)
        except ValueError as e:
            raise GitLabStatsError(
                f"X-Total for {path} was not an integer: {total!r}"
            ) from e

    # --- collection -----------------------------------------------------

    def _resolve_user_id(self) -> int:
        """Resolve the configured username to its numeric GitLab user id."""
        users, _ = self._get_json("/users", {"username": self.username})
        if not users:
            raise GitLabStatsError(f"No GitLab user found for '{self.username}'.")
        return users[0]["id"]

    def _fetch_projects(self) -> list:
        """Return every project the user belongs to.

        ``include_membership`` selects group/team projects as well as owned
        ones. With it off, only the personal namespace is counted, which
        mirrors GitHub's ``ownerAffiliations: OWNER`` but yields nothing on an
        instance where all work happens inside group namespaces.
        """
        params = {"per_page": PER_PAGE}
        params["membership" if self.include_membership else "owned"] = "true"

        projects, page = [], 1
        while True:
            batch, _ = self._get_json("/projects", {**params, "page": page})
            if not batch:
                break
            projects.extend(batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
        return projects

    def _count_commits(self, projects: list) -> int:
        """Count commits authored by ``self.emails`` across every branch.

        ``all=true`` walks all refs rather than the default branch alone. Where
        merge requests are squashed, the default branch retains only the
        squashed commit, so a default-branch count understates the real
        contribution (measured on this instance: 11 versus 49).
        """
        total = 0
        for project in projects:
            pid = project.get("id")
            name = project.get("path_with_namespace") or str(pid)

            if project.get("empty_repo"):
                logger.info("GitLab: skipping %s (empty repository).", name)
                continue

            counted = 0
            unlisted = set()
            page = 1
            while page <= self.max_commit_pages:
                batch, _ = self._get_json(
                    f"/projects/{pid}/repository/commits",
                    {"all": "true", "per_page": PER_PAGE, "page": page},
                )
                if not batch:
                    break
                for commit in batch:
                    email = (commit.get("author_email") or "").strip().lower()
                    author = (commit.get("author_name") or "").strip()
                    if email in self.emails:
                        counted += 1
                    elif self.username.lower() in author.lower():
                        # A name that looks like the user but an email that is
                        # not configured means a third git identity exists and
                        # its commits are going uncounted. Surface it.
                        unlisted.add((author, email))
                page += 1
            else:
                logger.warning(
                    "GitLab: reached the %d-page commit cap on %s; its count "
                    "is a floor, not a total.",
                    self.max_commit_pages,
                    name,
                )

            for author, email in sorted(unlisted):
                logger.warning(
                    "GitLab: commits by '%s' <%s> in %s are NOT counted - add "
                    "that address to gitlab.emails in config.yml if it is yours.",
                    author,
                    email,
                    name,
                )

            logger.info("GitLab: %d commit(s) attributed to you in %s", counted, name)
            total += counted
        return total

    # --- public API -----------------------------------------------------

    def fetch_stats(self) -> dict:
        """Fetch GitLab stats, returning GitHubAPI.fetch_stats()'s key set.

        Raises:
            GitLabStatsError: on any failure. Never returns partial data, so a
                broken run can never be mistaken for a quiet one.
        """
        if not self.token:
            raise GitLabStatsError(
                "No GitLab token available (set GITLAB_TOKEN); refusing to "
                "report public-only stats as if they were complete."
            )
        if not self.emails:
            raise GitLabStatsError(
                "gitlab.emails is empty, so no commit could be attributed to "
                "you; refusing to report 0 commits as a real figure."
            )

        user_id = self._resolve_user_id()
        projects = self._fetch_projects()

        stats = {
            "commits": self._count_commits(projects),
            "stars": sum(p.get("star_count", 0) for p in projects),
            "prs": self._count_via_x_total(
                "/merge_requests",
                {"author_id": user_id, "scope": "all", "state": "all"},
            ),
            "issues": self._count_via_x_total(
                "/issues",
                {"author_id": user_id, "scope": "all", "state": "all"},
            ),
            "repos": len(projects),
        }
        logger.info("GitLab stats for @%s: %s", self.username, stats)
        return stats
