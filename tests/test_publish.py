"""Tests for the `uv publish` wrapper."""

from __future__ import annotations

import pytest

from wheelbarrow.errors import PublishError
from wheelbarrow.publish import plan_publish


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / "demo_bin-1.0.0-py3-none-any.whl"
    path.write_bytes(b"not really a wheel")
    return path


class TestPlanPublish:
    def test_builds_a_uv_publish_command(self, wheel) -> None:
        plan = plan_publish([wheel])
        assert plan.argv[1] == "publish"
        assert plan.argv[0].endswith("uv")
        assert str(wheel.resolve()) in plan.argv

    def test_token_never_reaches_argv(self, wheel) -> None:
        """Tokens go through the environment so they cannot leak into `ps`."""
        plan = plan_publish(
            [wheel],
            token="pypi-not-a-real-secret",  # ruff: ignore[hardcoded-password-func-arg]
        )
        assert plan.uses_token is True
        assert not any("secret" in arg for arg in plan.argv)

    def test_index_is_forwarded(self, wheel) -> None:
        plan = plan_publish([wheel], index="testpypi")
        assert "--index" in plan.argv
        assert "testpypi" in plan.argv

    def test_publish_url_is_forwarded(self, wheel) -> None:
        plan = plan_publish([wheel], publish_url="https://example.com/legacy/")
        assert "--publish-url" in plan.argv

    def test_index_and_publish_url_conflict(self, wheel) -> None:
        with pytest.raises(PublishError, match="mutually exclusive"):
            plan_publish([wheel], index="testpypi", publish_url="https://x/")

    def test_empty_file_list_is_rejected(self) -> None:
        with pytest.raises(PublishError, match="no distributions"):
            plan_publish([])

    def test_missing_files_are_reported(self, tmp_path) -> None:
        with pytest.raises(PublishError, match="do not exist"):
            plan_publish([tmp_path / "absent.whl"])

    def test_display_is_printable(self, wheel) -> None:
        assert "publish" in plan_publish([wheel]).display()
