"""Config validation and defaults for the Galaxy Profile generator."""

from generator.utils import resolve_theme, HEX_COLOR_RE


class ConfigError(ValueError):
    """Raised when config.yml has invalid or missing data."""


def _validate_gitlab(gitlab: dict) -> dict:
    """Validate the optional 'gitlab' block and apply its own defaults.

    Only called when the key is actually present, so a config without a
    'gitlab' block is left byte-for-byte untouched.
    """
    if not isinstance(gitlab, dict):
        raise ConfigError("'gitlab' must be a mapping.")

    enabled = gitlab.setdefault("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("gitlab.enabled must be true or false.")
    if not enabled:
        # An explicitly disabled block is inert; nothing else is required.
        return gitlab

    host = gitlab.get("host")
    if not host or not isinstance(host, str) or not host.strip():
        raise ConfigError("gitlab.host is required (e.g. https://gitlab.com).")
    if not host.startswith(("http://", "https://")):
        raise ConfigError(
            f"gitlab.host must start with http:// or https://, got '{host}'."
        )

    username = gitlab.get("username")
    if not username or not isinstance(username, str) or not username.strip():
        raise ConfigError("gitlab.username is required and must be a non-empty string.")

    # Commits are attributed by git author email, not by username, so an empty
    # list would silently yield zero commits.
    emails = gitlab.get("emails")
    if not isinstance(emails, list) or not emails:
        raise ConfigError(
            "gitlab.emails must be a non-empty list of the git author emails "
            "you commit with; GitLab attributes commits by email, not username."
        )
    for i, email in enumerate(emails):
        if not isinstance(email, str) or "@" not in email:
            raise ConfigError(
                f"gitlab.emails[{i}] must be an email address, got '{email}'."
            )

    include_membership = gitlab.setdefault("include_membership", True)
    if not isinstance(include_membership, bool):
        raise ConfigError("gitlab.include_membership must be true or false.")

    return gitlab


def validate_config(config: dict) -> dict:
    """Validate and apply defaults to a parsed config dict.

    Args:
        config: raw dict from yaml.safe_load()

    Returns:
        config dict with defaults applied for optional fields

    Raises:
        ConfigError: if required fields are missing or values are invalid
    """
    if not isinstance(config, dict):
        raise ConfigError("Config must be a YAML mapping (dict).")

    # username — required
    username = config.get("username")
    if not username or not isinstance(username, str) or not username.strip():
        raise ConfigError("'username' is required and must be a non-empty string.")

    # profile.name — required
    profile = config.get("profile", {})
    if not isinstance(profile, dict):
        raise ConfigError("'profile' must be a mapping.")
    if not profile.get("name"):
        raise ConfigError("'profile.name' is required.")

    # galaxy_arms — required, must be a list
    galaxy_arms = config.get("galaxy_arms", [])
    if not isinstance(galaxy_arms, list) or not galaxy_arms:
        raise ConfigError("'galaxy_arms' must be a non-empty list.")
    for i, arm in enumerate(galaxy_arms):
        if not isinstance(arm, dict):
            raise ConfigError(f"galaxy_arms[{i}] must be a mapping.")
        if not arm.get("name"):
            raise ConfigError(f"galaxy_arms[{i}].name is required.")
        if not arm.get("color"):
            raise ConfigError(f"galaxy_arms[{i}].color is required.")
        if not isinstance(arm.get("items", []), list):
            raise ConfigError(f"galaxy_arms[{i}].items must be a list.")

    # projects — optional, validate entries if present
    projects = config.get("projects", [])
    if not isinstance(projects, list):
        raise ConfigError("'projects' must be a list.")
    for i, proj in enumerate(projects):
        if not isinstance(proj, dict):
            raise ConfigError(f"projects[{i}] must be a mapping.")
        if not proj.get("repo"):
            raise ConfigError(f"projects[{i}].repo is required.")
        arm_idx = proj.get("arm", 0)
        if not isinstance(arm_idx, int) or arm_idx < 0 or arm_idx >= len(galaxy_arms):
            raise ConfigError(
                f"projects[{i}].arm must be an integer from 0 to {len(galaxy_arms) - 1}."
            )

    # theme — optional, validate hex codes
    user_theme = config.get("theme", {})
    if not isinstance(user_theme, dict):
        raise ConfigError("'theme' must be a mapping.")
    for key, value in user_theme.items():
        if not isinstance(value, str) or not HEX_COLOR_RE.match(value):
            raise ConfigError(
                f"theme.{key} must be a valid hex color (e.g. #00d4ff), got '{value}'."
            )

    # Apply theme defaults
    config["theme"] = resolve_theme(user_theme)

    # Apply other defaults
    config["profile"].setdefault("tagline", "")
    config["profile"].setdefault("philosophy", "")
    config.setdefault("social", {})
    config.setdefault("projects", [])
    config.setdefault("stats", {}).setdefault(
        "metrics", ["commits", "stars", "prs", "issues", "repos"]
    )
    lang_cfg = config.setdefault("languages", {})
    lang_cfg.setdefault("exclude", [])
    lang_cfg.setdefault("max_display", 8)

    # gitlab — optional second data source. When the key is absent no default
    # is injected, so the generator behaves exactly as it did before GitLab
    # support existed.
    if "gitlab" in config:
        config["gitlab"] = _validate_gitlab(config["gitlab"])

    return config
