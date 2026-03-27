import os
from unittest.mock import patch

import pytest

from zbbx_mcp.config import load_all_configs, load_config, load_global_policy


class TestLoadConfig:
    @patch.dict(os.environ, {"ZABBIX_URL": "https://zabbix.example.com", "ZABBIX_TOKEN": "abc123"}, clear=True)
    def test_basic_config(self):
        cfg = load_config()
        assert cfg.url == "https://zabbix.example.com"
        assert cfg.token == "abc123"
        assert cfg.read_only is False
        assert cfg.disabled_tools == frozenset()

    @patch.dict(os.environ, {
        "ZABBIX_URL": "https://zabbix.example.com",
        "ZABBIX_TOKEN": "abc",
        "ZABBIX_READ_ONLY": "true",
    }, clear=True)
    def test_read_only(self):
        cfg = load_config()
        assert cfg.read_only is True

    @patch.dict(os.environ, {
        "ZABBIX_URL": "https://zabbix.example.com",
        "ZABBIX_TOKEN": "abc",
        "DISABLED_TOOLS": "acknowledge_problem, get_host",
    }, clear=True)
    def test_disabled_tools(self):
        cfg = load_config()
        assert "acknowledge_problem" in cfg.disabled_tools
        assert "get_host" in cfg.disabled_tools

    @patch.dict(os.environ, {"ZABBIX_URL": "https://zabbix.example.com/", "ZABBIX_TOKEN": "abc"}, clear=True)
    def test_trailing_slash_stripped(self):
        cfg = load_config()
        assert cfg.url == "https://zabbix.example.com"

    @patch.dict(os.environ, {"ZABBIX_URL": "http://badhost.com", "ZABBIX_TOKEN": "abc"}, clear=True)
    def test_http_blocked_without_allow(self):
        with pytest.raises(ValueError, match="ZABBIX_URL is required"):
            load_config()

    @patch.dict(os.environ, {
        "ZABBIX_URL": "http://badhost.com",
        "ZABBIX_TOKEN": "abc",
        "ZABBIX_ALLOW_HTTP": "1",
    }, clear=True)
    def test_http_allowed_with_flag(self):
        cfg = load_config()
        assert cfg.url == "http://badhost.com"

    @patch.dict(os.environ, {"ZABBIX_URL": "http://localhost:8080", "ZABBIX_TOKEN": "abc"}, clear=True)
    def test_localhost_always_allowed(self):
        cfg = load_config()
        assert cfg.url == "http://localhost:8080"

    @patch.dict(os.environ, {"ZABBIX_URL": "https://z.com"}, clear=True)
    def test_missing_token_raises(self):
        with pytest.raises(ValueError, match="ZABBIX_TOKEN is required"):
            load_config()

    @patch.dict(os.environ, {"ZABBIX_TOKEN": "abc"}, clear=True)
    def test_missing_url_raises(self):
        with pytest.raises(ValueError, match="ZABBIX_URL is required"):
            load_config()


class TestLoadGlobalPolicy:
    @patch.dict(os.environ, {"ZABBIX_READ_ONLY": "true", "DISABLED_TOOLS": "get_host"}, clear=True)
    def test_global_policy(self):
        read_only, disabled = load_global_policy()
        assert read_only is True
        assert "get_host" in disabled


class TestLoadAllConfigs:
    @patch.dict(os.environ, {"ZABBIX_URL": "https://zabbix.example.com", "ZABBIX_TOKEN": "abc"}, clear=True)
    def test_single_instance_fallback(self):
        configs = load_all_configs()
        assert "default" in configs
        assert configs["default"].url == "https://zabbix.example.com"

    @patch.dict(os.environ, {
        "ZABBIX_INSTANCES": "prod,staging",
        "ZABBIX_PROD_URL": "https://zabbix.prod.com",
        "ZABBIX_PROD_TOKEN": "prod-token",
        "ZABBIX_STAGING_URL": "https://zabbix.staging.com",
        "ZABBIX_STAGING_TOKEN": "staging-token",
    }, clear=True)
    def test_multi_instance(self):
        configs = load_all_configs()
        assert len(configs) == 2
        assert configs["prod"].url == "https://zabbix.prod.com"
        assert configs["staging"].token == "staging-token"

    @patch.dict(os.environ, {
        "ZABBIX_INSTANCES": "main",
        "ZABBIX_URL": "https://fallback.com",
        "ZABBIX_TOKEN": "fallback-token",
    }, clear=True)
    def test_first_instance_falls_back_to_unprefixed(self):
        configs = load_all_configs()
        assert configs["main"].url == "https://fallback.com"
        assert configs["main"].token == "fallback-token"

    @patch.dict(os.environ, {"ZABBIX_URL": "https://z.com", "ZABBIX_TOKEN": "t"}, clear=True)
    def test_config_is_frozen(self):
        cfg = load_config()
        with pytest.raises(AttributeError):
            cfg.url = "https://other.com"
