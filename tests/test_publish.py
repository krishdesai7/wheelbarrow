"""Tests for the `uv publish` wrapper."""

from __future__ import annotations

from typing import NamedTuple

import pytest

from wheelbarrow.errors import PublishError
from wheelbarrow.publish import (
    TOKEN_ENV,
    plan_publish,
    resolve_token,
    run_publish,
)

#: Stand-in for a real token. Never reaches a network, only the environment.
FAKE_TOKEN = "pypi-not-a-real-secret"  # ruff: ignore[hardcoded-password-string]


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / "demo_bin-1.0.0-py3-none-any.whl"
    path.write_bytes(b"not really a wheel")
    return path


@pytest.fixture
def token_set(monkeypatch):
    """Publish with credentials available."""
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)


@pytest.fixture
def token_unset(monkeypatch):
    """Publish with no credentials, whatever the developer's shell holds."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)


class FakeCompleted(NamedTuple):
    """Stands in for `subprocess.CompletedProcess`; only the status is read."""

    returncode: int


class TestPlanPublish:
    def test_builds_a_uv_publish_command(self, wheel) -> None:
        plan = plan_publish([wheel])
        assert plan.argv[1] == "publish"
        assert plan.argv[0].endswith("uv")
        assert str(wheel.resolve()) in plan.argv

    @pytest.mark.usefixtures("token_set")
    def test_token_never_reaches_argv(self, wheel) -> None:
        """The token goes through the environment so it cannot leak into `ps`."""
        plan = plan_publish([wheel])
        assert plan.needs_token is True
        assert not any(FAKE_TOKEN in arg for arg in plan.argv)
        assert not any("secret" in arg for arg in plan.argv)
        assert FAKE_TOKEN not in plan.display()

    def test_a_username_means_uv_authenticates_without_a_token(self, wheel) -> None:
        plan = plan_publish([wheel], username="krish")
        assert plan.needs_token is False

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


class TestResolveToken:
    """The token comes from the environment, and only from there."""

    @pytest.mark.usefixtures("token_set")
    def test_returns_the_exported_token(self) -> None:
        assert resolve_token() == FAKE_TOKEN

    def test_surrounding_whitespace_is_stripped(self, monkeypatch) -> None:
        monkeypatch.setenv(TOKEN_ENV, f"  {FAKE_TOKEN}\n")
        assert resolve_token() == FAKE_TOKEN

    @pytest.mark.usefixtures("token_unset")
    def test_missing_token_explains_how_to_set_one(self) -> None:
        with pytest.raises(PublishError, match=TOKEN_ENV) as excinfo:
            resolve_token()
        assert "export" in str(excinfo.value)

    def test_blank_token_counts_as_missing(self, monkeypatch) -> None:
        monkeypatch.setenv(TOKEN_ENV, "   ")
        with pytest.raises(PublishError, match=TOKEN_ENV):
            resolve_token()


class TestRunPublish:
    """uv is never reached without credentials."""

    @pytest.mark.usefixtures("token_unset")
    def test_missing_token_fails_before_uv_is_invoked(self, wheel, monkeypatch) -> None:
        def fail(*_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("uv publish was invoked without a token")

        monkeypatch.setattr("wheelbarrow.publish.subprocess.run", fail)
        with pytest.raises(PublishError, match=TOKEN_ENV):
            run_publish(plan_publish([wheel]))

    @pytest.mark.usefixtures("token_set")
    def test_a_token_is_not_added_to_the_command_line(self, wheel, monkeypatch) -> None:
        argv_seen: list[str] = []
        kwargs_seen: dict[str, object] = {}

        def capture(argv, **kwargs):
            argv_seen.extend(argv)
            kwargs_seen.update(kwargs)
            return FakeCompleted(0)

        monkeypatch.setattr("wheelbarrow.publish.subprocess.run", capture)
        run_publish(plan_publish([wheel]))

        assert argv_seen  # the fake really was called
        assert not any(FAKE_TOKEN in arg for arg in argv_seen)
        # No explicit env: uv inherits this process's, token included.
        assert "env" not in kwargs_seen

    @pytest.mark.usefixtures("token_set")
    def test_a_failing_upload_is_reported(self, wheel, monkeypatch) -> None:
        monkeypatch.setattr(
            "wheelbarrow.publish.subprocess.run",
            lambda argv, **_kwargs: FakeCompleted(1),  # ruff: ignore[unused-lambda-argument]
        )
        with pytest.raises(PublishError, match="not published"):
            run_publish(plan_publish([wheel]))
