"""Tests for generator.config.validate_config."""

import pytest

from generator.config import ConfigError, validate_config


class TestValidateConfig:
    def test_valid_config_passes(self, cfg):
        result = validate_config(cfg)
        assert result["username"] == "galaxy-dev"
        assert result["profile"]["name"] == "Nyx Orion"

    def test_username_required(self, cfg):
        del cfg["username"]
        with pytest.raises(ConfigError, match="username"):
            validate_config(cfg)

    def test_username_empty_string(self, cfg):
        cfg["username"] = "   "
        with pytest.raises(ConfigError, match="username"):
            validate_config(cfg)

    def test_profile_name_required(self, cfg):
        cfg["profile"]["name"] = ""
        with pytest.raises(ConfigError, match="profile.name"):
            validate_config(cfg)

    def test_galaxy_arms_must_be_nonempty_list(self, cfg):
        cfg["galaxy_arms"] = []
        with pytest.raises(ConfigError, match="galaxy_arms"):
            validate_config(cfg)

    def test_galaxy_arm_without_name(self, cfg):
        cfg["galaxy_arms"] = [{"color": "synapse_cyan"}]
        with pytest.raises(ConfigError, match="name is required"):
            validate_config(cfg)

    def test_galaxy_arm_without_color(self, cfg):
        cfg["galaxy_arms"] = [{"name": "Frontend"}]
        with pytest.raises(ConfigError, match="color is required"):
            validate_config(cfg)

    def test_project_arm_index_invalid(self, cfg):
        cfg["projects"] = [{"repo": "user/repo", "arm": 99}]
        with pytest.raises(ConfigError, match="arm must be an integer"):
            validate_config(cfg)

    def test_invalid_hex_color_in_theme(self, cfg):
        cfg["theme"] = {"void": "not-a-color"}
        with pytest.raises(ConfigError, match="valid hex color"):
            validate_config(cfg)

    def test_theme_override_merges_with_defaults(self, cfg):
        cfg["theme"] = {"void": "#112233"}
        result = validate_config(cfg)
        assert result["theme"]["void"] == "#112233"
        assert result["theme"]["synapse_cyan"] == "#00d4ff"  # default preserved

    def test_defaults_applied_for_optional_fields(self, cfg):
        del cfg["stats"]
        del cfg["languages"]
        del cfg["theme"]
        result = validate_config(cfg)
        assert "metrics" in result["stats"]
        assert "exclude" in result["languages"]
        assert "void" in result["theme"]

    def test_config_not_dict_fails(self):
        with pytest.raises(ConfigError, match="dict"):
            validate_config("not a dict")

    def test_config_none_fails(self):
        with pytest.raises(ConfigError, match="dict"):
            validate_config(None)


class TestValidateGitLab:
    """The optional 'gitlab' block."""

    BLOCK = {
        "host": "https://gitlab.igem.org",
        "username": "vcastelli",
        "emails": ["vcastelli@usp.br", "castellivinicius07@gmail.com"],
    }

    def test_absent_block_injects_no_key(self, cfg):
        """The no-GitLab path must stay exactly as it was."""
        assert "gitlab" not in cfg
        result = validate_config(cfg)
        assert "gitlab" not in result

    def test_valid_block_passes_and_applies_defaults(self, cfg):
        cfg["gitlab"] = dict(self.BLOCK)
        result = validate_config(cfg)

        assert result["gitlab"]["enabled"] is True
        assert result["gitlab"]["include_membership"] is True
        assert result["gitlab"]["host"] == "https://gitlab.igem.org"

    def test_disabled_block_needs_nothing_else(self, cfg):
        cfg["gitlab"] = {"enabled": False}
        result = validate_config(cfg)
        assert result["gitlab"]["enabled"] is False

    def test_block_must_be_a_mapping(self, cfg):
        cfg["gitlab"] = ["not", "a", "mapping"]
        with pytest.raises(ConfigError, match="'gitlab' must be a mapping"):
            validate_config(cfg)

    def test_enabled_must_be_boolean(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "enabled": "yes"}
        with pytest.raises(ConfigError, match="gitlab.enabled"):
            validate_config(cfg)

    def test_host_required(self, cfg):
        cfg["gitlab"] = {k: v for k, v in self.BLOCK.items() if k != "host"}
        with pytest.raises(ConfigError, match="gitlab.host is required"):
            validate_config(cfg)

    def test_host_must_have_a_scheme(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "host": "gitlab.igem.org"}
        with pytest.raises(ConfigError, match="must start with http"):
            validate_config(cfg)

    def test_username_required(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "username": "  "}
        with pytest.raises(ConfigError, match="gitlab.username"):
            validate_config(cfg)

    def test_emails_required_and_non_empty(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "emails": []}
        with pytest.raises(ConfigError, match="gitlab.emails"):
            validate_config(cfg)

    def test_emails_must_be_a_list(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "emails": "vcastelli@usp.br"}
        with pytest.raises(ConfigError, match="gitlab.emails"):
            validate_config(cfg)

    def test_email_entries_must_look_like_addresses(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "emails": ["not-an-address"]}
        with pytest.raises(ConfigError, match=r"gitlab.emails\[0\]"):
            validate_config(cfg)

    def test_include_membership_must_be_boolean(self, cfg):
        cfg["gitlab"] = {**self.BLOCK, "include_membership": "true"}
        with pytest.raises(ConfigError, match="gitlab.include_membership"):
            validate_config(cfg)
