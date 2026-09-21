"""Tests for combining GitLab stats with GitHub's, and for staying inert.

The contract these lock down:
  * No gitlab config and no token  -> byte-identical output to a pre-GitLab run.
  * A GitLab failure              -> GitHub numbers untouched, card says so.
  * Success                       -> per-metric sums, card tagged.
"""

import copy
import os
import pathlib

import pytest

from generator.config import validate_config
from generator.gitlab_api import GitLabStatsError
from generator.main import _merge_gitlab_stats
from generator.svg_builder import SVGBuilder

GOLDEN = pathlib.Path(__file__).parent / "golden" / "stats_card_baseline.svg"

GITHUB_STATS = {"commits": 659, "stars": 1, "prs": 27, "issues": 61, "repos": 35}
GITLAB_STATS = {"commits": 49, "stars": 0, "prs": 8, "issues": 0, "repos": 2}

GITLAB_CFG = {
    "enabled": True,
    "host": "https://gitlab.example.org",
    "username": "vcastelli",
    "emails": ["vcastelli@usp.br"],
    "include_membership": True,
}


class FakeGitLabAPI:
    """Stands in for GitLabAPI; records construction kwargs."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeGitLabAPI.instances.append(self)

    def fetch_stats(self):
        return dict(GITLAB_STATS)


class ExplodingGitLabAPI(FakeGitLabAPI):
    def fetch_stats(self):
        raise GitLabStatsError("instance unreachable")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    FakeGitLabAPI.instances = []
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


# --- not configured: must behave exactly as before ----------------------


def test_no_gitlab_block_leaves_stats_identical():
    stats = dict(GITHUB_STATS)
    merged, label = _merge_gitlab_stats({}, stats)

    assert merged == GITHUB_STATS
    assert label is None, "a card with no GitLab config must carry no tag"


def test_disabled_gitlab_block_is_inert(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    cfg = {"gitlab": {**GITLAB_CFG, "enabled": False}}
    merged, label = _merge_gitlab_stats(cfg, dict(GITHUB_STATS))

    assert merged == GITHUB_STATS
    assert label is None


def test_no_gitlab_config_renders_byte_identical_card(svg_builder):
    """Guards the 'unchanged output' promise against template drift."""
    assert svg_builder.source_label is None
    assert svg_builder.render_stats_card() == GOLDEN.read_text()


def test_stats_card_gains_no_element_without_a_label(svg_builder):
    svg = svg_builder.render_stats_card()
    assert "GITLAB" not in svg
    assert "GITHUB" not in svg


# --- configured but degraded: GitHub must survive -----------------------


def test_missing_token_keeps_github_numbers_and_tags_card():
    merged, label = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert merged == GITHUB_STATS, "GitHub numbers must not be disturbed"
    assert label == "GITHUB ONLY"


def test_gitlab_failure_keeps_github_numbers_and_tags_card(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setattr("generator.main.GitLabAPI", ExplodingGitLabAPI)

    merged, label = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert merged == GITHUB_STATS, "a GitLab outage must not zero out GitHub"
    assert label == "GITHUB ONLY"


def test_gitlab_failure_is_logged_loudly(monkeypatch, caplog):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setattr("generator.main.GitLabAPI", ExplodingGitLabAPI)

    with caplog.at_level("ERROR"):
        _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert "FAILED" in caplog.text
    assert "instance unreachable" in caplog.text


def test_gitlab_failure_emits_actions_annotation(monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("generator.main.GitLabAPI", ExplodingGitLabAPI)

    _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert "::warning title=GitLab stats fetch failed::" in capsys.readouterr().err


def test_failed_run_is_distinguishable_from_an_empty_one(monkeypatch):
    """Zero GitLab activity and a broken fetch must not look the same."""
    monkeypatch.setenv("GITLAB_TOKEN", "t")

    class EmptyGitLabAPI(FakeGitLabAPI):
        def fetch_stats(self):
            return {"commits": 0, "stars": 0, "prs": 0, "issues": 0, "repos": 0}

    monkeypatch.setattr("generator.main.GitLabAPI", EmptyGitLabAPI)
    _, empty_label = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    monkeypatch.setattr("generator.main.GitLabAPI", ExplodingGitLabAPI)
    _, failed_label = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert empty_label == "GITHUB + GITLAB"
    assert failed_label == "GITHUB ONLY"
    assert empty_label != failed_label


# --- configured and working --------------------------------------------


def test_stats_are_summed_per_metric(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setattr("generator.main.GitLabAPI", FakeGitLabAPI)

    merged, label = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    assert merged == {
        "commits": 708,  # 659 + 49
        "stars": 1,      # 1 + 0
        "prs": 35,       # 27 + 8
        "issues": 61,    # 61 + 0
        "repos": 37,     # 35 + 2
    }
    assert label == "GITHUB + GITLAB"


def test_merge_keeps_the_github_key_set(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setattr("generator.main.GitLabAPI", FakeGitLabAPI)

    merged, _ = _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))
    assert set(merged) == set(GITHUB_STATS)


def test_config_values_reach_the_client(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setattr("generator.main.GitLabAPI", FakeGitLabAPI)

    _merge_gitlab_stats({"gitlab": dict(GITLAB_CFG)}, dict(GITHUB_STATS))

    kwargs = FakeGitLabAPI.instances[0].kwargs
    assert kwargs["host"] == "https://gitlab.example.org"
    assert kwargs["username"] == "vcastelli"
    assert kwargs["emails"] == ["vcastelli@usp.br"]
    assert kwargs["include_membership"] is True
    assert "token" not in kwargs, "the token must come from the env, not config"


def test_tagged_card_shows_the_source_label(cfg):
    config = validate_config(copy.deepcopy(cfg))
    builder = SVGBuilder(config, dict(GITHUB_STATS), {}, source_label="GITHUB + GITLAB")
    svg = builder.render_stats_card()

    assert "GITHUB + GITLAB" in svg
    assert svg.strip().startswith("<svg") and svg.strip().endswith("</svg>")


def test_github_only_tag_is_visible_on_the_card(cfg):
    config = validate_config(copy.deepcopy(cfg))
    builder = SVGBuilder(config, dict(GITHUB_STATS), {}, source_label="GITHUB ONLY")
    assert "GITHUB ONLY" in builder.render_stats_card()
