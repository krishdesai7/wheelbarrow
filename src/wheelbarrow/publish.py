"""Publishing built distributions with `uv publish`."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import PublishError


@dataclass(frozen=True)
class PublishPlan:
    """
    The token is deliberately not part of `argv`: it is passed through the
    environment so it cannot leak into `ps` output or a shell history.
    """

    argv: list[str]
    files: list[Path]
    uses_token: bool

    def display(self) -> str:
        return " ".join(self.argv)


def plan_publish(
    files: list[Path],
    *,
    index: str | None = None,
    publish_url: str | None = None,
    token: str | None = None,
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
        argv=argv, files=[Path(f) for f in files], uses_token=bool(token)
    )


def run_publish(plan: PublishPlan, *, token: str | None = None) -> None:
    """Execute a `PublishPlan`, streaming uv's output to the terminal."""
    env: dict[str, str] = dict(os.environ)
    if token:
        env["UV_PUBLISH_TOKEN"] = token

    try:
        completed: subprocess.CompletedProcess[bytes] = subprocess.run(
            plan.argv, env=env, check=False
        )
    except OSError as exc:  # pragma: no cover - uv vanished between check and run
        raise PublishError(f"could not run uv publish: {exc}") from exc

    if completed.returncode != 0:
        raise PublishError(
            f"uv publish exited with status {completed.returncode}; "
            f"the distributions were not published"
        )
