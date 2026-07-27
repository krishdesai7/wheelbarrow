"""Wheel compilation: drive the build backend, then tag the result.

`build_wheel` is a thin wrapper over PEP 517 (`build.ProjectBuilder`), and
`build_package` is the end-to-end pipeline that most callers want:

    inspect -> scaffold -> stage -> build -> retag

`build_packages` runs that once per binary for a whole directory, sharing one
name and version across the set. Its job beyond the loop is to refuse a batch
that cannot succeed *before* any of it runs, since a failure half way through
leaves finished wheels in the output directory that nothing will clean up.
"""

import hashlib
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from build import BuildBackendException, BuildException, ProjectBuilder
from build.env import DefaultIsolatedEnv
from pyproject_hooks import default_subprocess_runner, quiet_subprocess_runner

if TYPE_CHECKING:
    # pyproject_hooks exports this Protocol for typing only; it does not exist
    # at runtime, so importing it unguarded raises ImportError.
    from collections.abc import Callable, Sequence

    from pyproject_hooks import SubprocessRunner

from .errors import BuildError
from .scaffold import PackageSpec, archive_executables, scaffold_project
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
            # uv_build is a direct dependency of wheelbarrow, so the backend
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
    overwrite: bool = False,
) -> BuildResult:
    """Scaffold, build and tag a wheel wrapping `binary`.

    The intermediate project is written to a temporary directory unless
    `keep_project` names somewhere to keep it, which is useful for debugging
    what was generated.

    The wheel is built and retagged out of sight and only then placed in
    `output_dir`, so a failure part-way cannot leave a half-finished or
    wrongly tagged archive somewhere the caller might later publish.
    """
    binary = Path(binary)
    output_dir = Path(output_dir)
    tag: str = full_tag(spec.platform_tag)

    with tempfile.TemporaryDirectory(prefix="wheelbarrow-") as tmp:
        project_root: Path = Path(tmp) / spec.dist_name
        project_root.mkdir(parents=True)
        scaffold_project(spec, binary, project_root)

        raw_wheel: Path = build_wheel(
            project_root, Path(tmp) / "wheel", isolated=isolated, verbose=verbose
        )

        kept: Path | None = None
        if keep_project is not None:
            kept = Path(keep_project)
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(project_root, kept)

        result: RetagResult = retag_wheel(
            raw_wheel,
            tag=tag,
            executable_paths=archive_executables(spec),
        )
        if not result.executables:  # pragma: no cover - defensive
            raise BuildError("no executable entries were marked in the wheel")

        placed: Path = _place_wheel(result.path, output_dir, overwrite=overwrite)

    return BuildResult(wheel=placed, tag=tag, spec=spec, project_dir=kept)


def build_packages(
    plans: Sequence[tuple[Path, PackageSpec]],
    output_dir: Path,
    *,
    isolated: bool = False,
    verbose: bool = False,
    keep_project: Path | None = None,
    overwrite: bool = False,
    on_built: Callable[[BuildResult], None] | None = None,
) -> list[BuildResult]:
    """Build one wheel per `(binary, spec)` pair into a shared output directory.

    Collisions are refused up front rather than discovered on the wheel that
    hits one. `_place_wheel` would catch the same thing, but only after earlier
    wheels had already been written -- and the whole point of a batch is that
    the user is not watching each one.
    """
    refuse_tag_collisions(plans)

    results: list[BuildResult] = []
    for binary, spec in plans:
        result: BuildResult = build_package(
            binary,
            spec,
            output_dir,
            isolated=isolated,
            verbose=verbose,
            keep_project=_keep_dir(keep_project, spec, batched=len(plans) > 1),
            overwrite=overwrite,
        )
        results.append(result)
        if on_built:
            on_built(result)
    return results


def _keep_dir(
    keep_project: Path | None, spec: PackageSpec, *, batched: bool
) -> Path | None:
    """Give each build its own project directory, or they overwrite each other."""
    if keep_project is None:
        return None
    return Path(keep_project) / spec.platform_tag if batched else Path(keep_project)


def refuse_tag_collisions(plans: Sequence[tuple[Path, PackageSpec]]) -> None:
    """Reject a batch in which two different binaries claim one wheel name.

    Name and version are shared across a batch, so the platform tag is the only
    thing separating one wheel from the next: a glibc build and a static build
    of the same tool both resolve to `manylinux_<baseline>_x86_64`, and the
    second would silently replace the first. Identical inputs are exempt, since
    builds are reproducible and a duplicate is a rebuild rather than a loss.
    """
    by_tag: dict[str, list[Path]] = defaultdict(list)
    for binary, spec in plans:
        by_tag[spec.platform_tag].append(binary)

    clashes: list[tuple[str, list[Path]]] = [
        (tag, binaries)
        for tag, binaries in by_tag.items()
        if len(binaries) > 1 and len({_digest(b) for b in binaries}) > 1
    ]
    if not clashes:
        return

    detail: str = "\n".join(
        f"    {tag}\n" + "\n".join(f"      {b}" for b in binaries)
        for tag, binaries in sorted(clashes)
    )
    raise BuildError(
        f"these binaries resolve to the same platform tag, so they would "
        f"overwrite one another in the output directory:\n{detail}\n"
        f"Build them separately, or pass --platform-tag to distinguish them. "
        f"A dynamically linked glibc build and a static build of the same "
        f"architecture are the usual cause."
    )


def _digest(path: Path) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _place_wheel(built: Path, output_dir: Path, *, overwrite: bool) -> Path:
    """Move the finished wheel into `output_dir`, refusing to clobber.

    Two inputs can resolve to one tag -- a glibc build and a static build of
    the same tool both look like `manylinux_<baseline>_x86_64` -- and then the
    second build overwrites the first with no sign that anything was lost.
    Builds are reproducible, so identical bytes are treated as a harmless
    rebuild; only a *differing* file already in place is an error.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target: Path = output_dir / built.name
    data: bytes = built.read_bytes()

    if not overwrite and target.is_file() and target.read_bytes() != data:
        raise BuildError(
            f"{target.name} already exists in {output_dir} with different "
            f"contents. Two builds resolving to the same platform tag will "
            f"otherwise silently overwrite one another; check whether this "
            f"input duplicates an earlier one, or pass --overwrite to replace it."
        )

    target.write_bytes(data)
    return target
