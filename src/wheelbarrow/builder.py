"""Wheel compilation: drive the build backend, then tag the result.

`build_wheel` is a thin wrapper over PEP 517 (`build.ProjectBuilder`), and
`build_package` is the end-to-end pipeline that most callers want:

    inspect -> scaffold -> stage -> build -> retag
"""

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from build import BuildBackendException, BuildException, ProjectBuilder
from build.env import DefaultIsolatedEnv
from pyproject_hooks import (
    SubprocessRunner,
    default_subprocess_runner,
    quiet_subprocess_runner,
)

from .errors import BuildError
from .scaffold import PackageSpec, scaffold_project
from .tags import full_tag
from .wheelfix import RetagResult, retag_wheel


@dataclass(frozen=True)
class BuildResult:
    wheel: Path
    tag: str
    spec: PackageSpec
    project_dir: Path | None  # kept only when the caller asked for it


def build_wheel(
    project_root: Path,
    output_dir: Path,
    *,
    isolated: bool = False,
    verbose: bool = False,
) -> Path:
    """Build a wheel from `project_root` via PEP 517 and return its path."""
    project_root = Path(project_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner: SubprocessRunner = (
        default_subprocess_runner if verbose else quiet_subprocess_runner
    )

    try:
        if isolated:
            with DefaultIsolatedEnv() as env:
                builder: ProjectBuilder = ProjectBuilder.from_isolated_env(
                    env, project_root, runner=runner
                )
                env.install(builder.build_system_requires)
                env.install(builder.get_requires_for_build("wheel"))
                built: str = builder.build("wheel", str(output_dir))
        else:
            # hatchling is a direct dependency of wheelbarrow, so the backend
            # is already importable and we can skip building a venv.
            builder = ProjectBuilder(project_root, runner=runner)
            built = builder.build("wheel", str(output_dir))
    except BuildBackendException as exc:
        raise BuildError(f"the build backend failed: {exc}") from exc
    except BuildException as exc:
        raise BuildError(f"could not build the generated project: {exc}") from exc

    return Path(built)


def build_package(
    binary: Path,
    spec: PackageSpec,
    output_dir: Path,
    *,
    isolated: bool = False,
    verbose: bool = False,
    keep_project: Path | None = None,
) -> BuildResult:
    """Scaffold, build and tag a wheel wrapping `binary`.

    The intermediate project is written to a temporary directory unless
    `keep_project` names somewhere to keep it, which is useful for debugging
    what was generated.
    """
    binary = Path(binary)
    output_dir = Path(output_dir)

    with tempfile.TemporaryDirectory(prefix="wheelbarrow-") as tmp:
        project_root: Path = Path(tmp) / spec.dist_name
        project_root.mkdir(parents=True)
        scaffold_project(spec, binary, project_root)

        raw_wheel: Path = build_wheel(
            project_root, output_dir, isolated=isolated, verbose=verbose
        )

        kept: Path | None = None
        if keep_project is not None:
            kept = Path(keep_project)
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(project_root, kept)

    tag: str = full_tag(spec.platform_tag)
    result: RetagResult = retag_wheel(
        raw_wheel,
        tag=tag,
        executable_paths={f"{spec.module}/bin/{spec.binary_name}"},
    )
    if not result.executables:  # pragma: no cover - defensive
        raise BuildError("no executable entries were marked in the wheel")

    return BuildResult(wheel=result.path, tag=tag, spec=spec, project_dir=kept)
