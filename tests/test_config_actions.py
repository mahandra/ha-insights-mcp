"""Actions are off by default and fail closed when misconfigured."""

from pathlib import Path

import pytest

from ha_insights_mcp.actions.policy import PolicyError
from ha_insights_mcp.config import Settings
from ha_insights_mcp.server import actions_from_settings
from tests.conftest import EXAMPLE_POLICY


def settings(**values) -> Settings:
    return Settings(_env_file=None, ha_token="t", **values)


def test_actions_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ACTIONS_ENABLED", raising=False)
    assert actions_from_settings(settings()) is None


def test_enabled_without_policy_file_fails(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        actions_from_settings(
            settings(actions_enabled=True, actions_policy_path=tmp_path / "missing.toml")
        )


def test_enabled_with_policy(tmp_path: Path) -> None:
    actions = actions_from_settings(
        settings(
            actions_enabled=True,
            actions_policy_path=EXAMPLE_POLICY,
            audit_db_path=tmp_path / "audit.db",
        )
    )
    assert actions is not None
    assert actions.policy.allows("light.turn_on")
    assert (tmp_path / "audit.db").exists()
    actions.audit_log.close()
