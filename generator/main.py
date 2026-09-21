"""Entry point for the Galaxy Profile README generator."""

import argparse
import logging
import os
import sys

import requests
import yaml

from generator.config import ConfigError, validate_config
from generator.github_api import GitHubAPI, StatsFetchError
from generator.gitlab_api import GitLabAPI, GitLabStatsError
from generator.svg_builder import SVGBuilder

logger = logging.getLogger(__name__)

DEMO_STATS = {"commits": 1847, "stars": 342, "prs": 156, "issues": 89, "repos": 42}
DEMO_LANGUAGES = {
    "Python": 450000,
    "TypeScript": 380000,
    "JavaScript": 120000,
    "Go": 95000,
    "Rust": 45000,
    "Shell": 30000,
    "Dockerfile": 15000,
    "CSS": 10000,
}


def _actions_error(title: str, message: str):
    """Emit a GitHub Actions error annotation so failures surface in the run UI."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("\n", " ").replace("\r", " ")
        print(f"::error title={title}::{flat}", file=sys.stderr)


def _actions_warning(title: str, message: str):
    """Emit a GitHub Actions warning annotation for a non-fatal degradation."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("\n", " ").replace("\r", " ")
        print(f"::warning title={title}::{flat}", file=sys.stderr)


def _gitlab_config(config: dict):
    """Return the gitlab block if present and enabled, else None."""
    gitlab = config.get("gitlab")
    if not gitlab or not gitlab.get("enabled", True):
        return None
    return gitlab


def _merge_gitlab_stats(config: dict, stats: dict):
    """Add GitLab numbers onto the GitHub ones.

    Returns (stats, source_label). A GitLab problem never alters the GitHub
    figures, but it always changes the label to "GITHUB ONLY" so a degraded run
    is distinguishable from a genuinely quiet one on the card itself, not just
    in a log line. source_label is None when GitLab is not configured at all,
    which keeps the rendered SVG byte-identical to a pre-GitLab run.
    """
    gitlab = _gitlab_config(config)
    if gitlab is None:
        logger.info("GitLab not configured; using GitHub data only.")
        return stats, None

    if not os.environ.get("GITLAB_TOKEN"):
        msg = (
            "GitLab is configured but GITLAB_TOKEN is not set, so GitLab data "
            "is EXCLUDED from this run."
        )
        logger.warning(msg)
        _actions_warning("GitLab token missing", msg)
        return stats, "GITHUB ONLY"

    logger.info("Fetching GitLab stats for @%s...", gitlab["username"])
    api = GitLabAPI(
        host=gitlab["host"],
        username=gitlab["username"],
        emails=gitlab.get("emails", []),
        include_membership=gitlab.get("include_membership", True),
    )

    try:
        gitlab_stats = api.fetch_stats()
    except GitLabStatsError as e:
        logger.error("GitLab stats fetch FAILED: %s", e)
        logger.error(
            "GitHub numbers are unaffected. The card will be tagged GITHUB ONLY."
        )
        _actions_warning("GitLab stats fetch failed", str(e))
        return stats, "GITHUB ONLY"

    merged = {key: stats.get(key, 0) + gitlab_stats.get(key, 0) for key in stats}
    logger.info("GitHub stats:   %s", stats)
    logger.info("GitLab stats:   %s", gitlab_stats)
    logger.info("Combined stats: %s", merged)
    return merged, "GITHUB + GITLAB"


def generate(args):
    """Generate SVGs from config (existing behavior extracted into a function)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    demo = getattr(args, "demo", False)

    # Load config
    if demo:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.example.yml")
    else:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.yml")

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        if demo:
            logger.error("config.example.yml not found.")
        else:
            logger.error("config.yml not found. Copy config.example.yml to config.yml and edit it.")
        sys.exit(1)

    try:
        config = validate_config(config)
    except ConfigError as e:
        logger.error("Invalid config: %s", e)
        sys.exit(1)

    username = config["username"]

    logger.info("Generating profile SVGs for @%s...", username)

    if demo:
        logger.info("Demo mode: using hardcoded stats and languages.")
        stats = DEMO_STATS
        languages = DEMO_LANGUAGES
        # Demo makes no network calls, so GitLab is not consulted and the card
        # carries no provenance tag.
        source_label = None
    else:
        # Fetch GitHub data
        api = GitHubAPI(username)

        logger.info("Fetching stats...")
        try:
            stats = api.fetch_stats()
        except StatsFetchError as e:
            # Never fall back to zeros: a card of zeros looks like real data and
            # hides the failure. Fail the run so it is visible in the Actions log.
            logger.error("Could not fetch stats: %s", e)
            _actions_error("Stats fetch failed", str(e))
            logger.error("Refusing to generate SVGs with placeholder stats.")
            sys.exit(1)

        stats, source_label = _merge_gitlab_stats(config, stats)

        # Languages stay GitHub-only: GitLab's /languages endpoint returns
        # percentages while this pipeline works in bytes, and mixing the two
        # would silently distort the chart.
        logger.info("Fetching languages...")
        try:
            languages = api.fetch_languages()
        except (requests.exceptions.RequestException, ValueError, KeyError) as e:
            logger.warning("Could not fetch languages (%s). Using defaults.", e)
            languages = {}

    logger.info("Stats: %s", stats)
    logger.info("Languages: %d found", len(languages))

    # Build SVGs
    builder = SVGBuilder(config, stats, languages, source_label=source_label)
    output_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "generated")
    os.makedirs(output_dir, exist_ok=True)

    svgs = {
        "galaxy-header.svg": builder.render_galaxy_header(),
        "stats-card.svg": builder.render_stats_card(),
        "tech-stack.svg": builder.render_tech_stack(),
        "projects-constellation.svg": builder.render_projects_constellation(),
    }

    for filename, content in svgs.items():
        path = os.path.join(output_dir, filename)
        with open(path, "w") as f:
            f.write(content)
        logger.info("Wrote %s", path)

    logger.info("Done! 4 SVGs generated.")


def main():
    parser = argparse.ArgumentParser(description="Generate Galaxy Profile SVGs")
    subparsers = parser.add_subparsers(dest="command")

    # Subcommand: init
    subparsers.add_parser("init", help="Interactive setup wizard to create config.yml")

    # Subcommand: generate
    gen_parser = subparsers.add_parser("generate", help="Generate SVGs from config")
    gen_parser.add_argument(
        "--demo",
        action="store_true",
        help="Generate SVGs with demo data (no API calls, uses config.example.yml)",
    )

    # Top-level --demo for backward compatibility (python -m generator.main --demo)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Generate SVGs with demo data (no API calls, uses config.example.yml)",
    )

    args = parser.parse_args()

    if args.command == "init":
        from generator.cli_init import run_init
        run_init()
    else:
        # Default behavior: generate (supports both `generate --demo` and `--demo`)
        generate(args)


if __name__ == "__main__":
    main()
