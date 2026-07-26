"""Publishing built distributions with `uv publish`."""

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - intentionally invokes uv without a shell
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import PublishError

#: uv reads the API token from this variable. Taking it from the environment
#: rather than an option is what keeps it out of `argv`, `ps` output and shell
#: history; there is deliberately no way to pass it on the command line.
TOKEN_ENV: Final[str] = "UV_PUBLISH_TOKEN"  # ruff: ignore[hardcoded-password-string]


@dataclass(frozen=True)
class PublishPlan:
    """A `uv publish` invocation, ready to run.

    Credentials are deliberately absent from `argv`: the token reaches uv
    through the environment, so it cannot leak into `ps` output or a shell
    history. `needs_token` records whether this plan relies on it, which is
    false when a username was given and uv authenticates some other way.
    """

    argv: list[str]
    files: list[Path]
    needs_token: bool

    def display(self) -> str:
        return " ".join(self.argv)


def resolve_token() -> str:
    """Return the API token from the environment, or explain how to set one."""
    token: str = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise PublishError(
            f"{TOKEN_ENV} is not set, so there is no way to authenticate. "
            f"Export an API token and try again:\n"
            f"    export {TOKEN_ENV}=pypi-...\n"
            f"It is read from the environment so that it never appears in the "
            f"command line. Note that a .env file is not picked up "
            f"automatically; load it with `uv run --env-file .env ...` or "
            f"export the variable yourself."
        )
    return token


def plan_publish(
    files: list[Path],
    *,
    index: str | None = None,
    publish_url: str | None = None,
    username: str | None = None,
) -> PublishPlan:
    """Assemble the `uv publish` invocation for `files`."""
    if not files:
        raise PublishError("no distributions to publish")

    missing: list[Path] = [f for f in files if not Path(f).is_file()]
    if missing:
        listed: str = ", ".join(str(m) for m in missing)
        raise PublishError(f"cannot publish files that do not exist: {listed}")

    if index and publish_url:
        raise PublishError("--index and --publish-url are mutually exclusive")

    uv: str | None = shutil.which("uv")
    if uv is None:
        raise PublishError(
            "uv was not found on PATH. Install it from https://docs.astral.sh/uv/ "
            "to publish, or upload the wheels manually with twine."
        )

    argv: list[str] = [uv, "publish"]
    if index:
        argv += ["--index", index]
    if publish_url:
        argv += ["--publish-url", publish_url]
    if username:
        argv += ["--username", username]
    argv += [str(Path(f).resolve()) for f in files]

    return PublishPlan(
        argv=argv, files=[Path(f) for f in files], needs_token=username is None
    )


def run_publish(plan: PublishPlan) -> None:
    """Execute a `PublishPlan`, streaming uv's output to the terminal.

    The token is not forwarded explicitly: uv reads `UV_PUBLISH_TOKEN` from the
    environment this process already passes on to it. It is only checked here,
    so a missing one is reported before any upload is attempted.
    """
    if plan.needs_token:
        resolve_token()

    try:
        completed: subprocess.CompletedProcess[bytes] = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            plan.argv, check=False
        )
    except OSError as exc:  # pragma: no cover - uv vanished between check and run
        raise PublishError(f"could not run uv publish: {exc}") from exc

    if completed.returncode != 0:
        raise PublishError(
            f"uv publish exited with status {completed.returncode}; "
            f"the distributions were not published"
        )
