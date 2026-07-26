"""Dynamic project templating and asset staging.

Given an inspected binary plus package metadata, `scaffold_project` writes a
complete, buildable Python project to disk. The layout depends on the launcher.

`Launcher.DIRECT` stages the binary outside the module, so uv_build maps it
into the wheel's `.data/scripts/` directory and nothing sweeps it up a second
time as package data:

    <root>/
      pyproject.toml
      README.md
      scripts/<alias>       <- staged executable, mode 0o755, one per alias
      src/<module>/
        __init__.py         <- binary_path() locates the installed script
        __main__.py

`Launcher.SHIM` keeps the binary inside the package and exposes console script
entry points that `execv` it:

    <root>/
      pyproject.toml
      README.md
      src/<module>/
        __init__.py
        __main__.py
        bin/<binary>        <- staged executable, mode 0o755
"""

import re
import shutil
import stat
from dataclasses import dataclass, field
from enum import StrEnum, auto
from pathlib import Path
from typing import Final

from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import InvalidVersion, Version

from .errors import MetadataError
from .templates import (
    DATA_TABLE,
    INIT_DIRECT,
    INIT_SHIM,
    MAIN,
    PYPROJECT,
    README,
    toml_array,
    toml_str,
)

#: Directory, relative to the project root, holding binaries destined for
#: `.data/scripts/`. Deliberately outside `src/` so it is not package data.
SCRIPTS_DIR: Final[str] = "scripts"


class Launcher(StrEnum):
    """How the packaged binary is exposed on `PATH`."""

    #: Binary installed straight into the environment's scripts directory.
    #: No Python runs on invocation.
    DIRECT = auto()
    #: Console script entry point that `execv`s a binary inside the package.
    SHIM = auto()


@dataclass
class PackageSpec:
    """Everything needed to render a project, already validated."""

    dist_name: str  # PEP 503 normalised, e.g. "ripgrep-bin"
    module: str  # importable, e.g. "ripgrep_bin"
    version: str  # PEP 440 normalised
    binary_name: str  # file name inside bin/, e.g. "rg"
    aliases: list[str]  # console scripts to expose
    platform_tag: str
    launcher: Launcher = Launcher.DIRECT
    description: str = ""
    requires_python: str = ">=3.8"
    license: str | None = None
    authors: list[tuple[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    classifiers: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)

    @property
    def installed_name(self) -> str:
        """File name the executable carries once installed.

        In shim mode it keeps its original name inside the package. In direct
        mode it *becomes* the command, so it is named after the first alias.
        """
        if self.launcher is Launcher.SHIM:
            return self.binary_name
        return self.aliases[0]


_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")


def make_spec(
    *,
    name: str,
    version: str,
    binary_name: str,
    platform_tag: str,
    aliases: list[str] | None = None,
    launcher: Launcher = Launcher.DIRECT,
    description: str = "",
    requires_python: str = ">=3.8",
    license: str | None = None,
    author: str | None = None,
    author_email: str | None = None,
    keywords: list[str] | None = None,
    homepage: str | None = None,
) -> PackageSpec:
    """Validate and normalise user-supplied metadata into a `PackageSpec`."""
    dist_name: str = _normalise_name(name)
    module: str = _module_name(dist_name)
    normalised_version: str = _normalise_version(version)

    resolved_aliases: list[str] = aliases or [_default_alias(binary_name)]
    for alias in resolved_aliases:
        if not alias or not _ALIAS_RE.fullmatch(alias):
            raise MetadataError(
                f"invalid console script alias {alias!r}: expected a name made of "
                f"letters, digits, dots, underscores or hyphens"
            )

    authors: list[tuple[str, str]] = []
    if author or author_email:
        authors.append((author or "", author_email or ""))

    urls: dict[str, str] = {}
    if homepage:
        urls["Homepage"] = homepage

    return PackageSpec(
        dist_name=dist_name,
        module=module,
        version=normalised_version,
        binary_name=binary_name,
        aliases=resolved_aliases,
        platform_tag=platform_tag,
        launcher=launcher,
        description=description,
        requires_python=requires_python,
        license=license,
        authors=authors,
        keywords=keywords or [],
        urls=urls,
    )


def _normalise_name(name: str) -> str:
    if not name or not name.strip():
        raise MetadataError("package name must not be empty")
    canonical: NormalizedName = canonicalize_name(name.strip())
    if not re.fullmatch(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?", canonical):
        raise MetadataError(
            f"{name!r} is not a valid PyPI project name (PEP 508); it must start "
            f"and end with a letter or digit"
        )
    return canonical


def _module_name(dist_name: str) -> str:
    module: str = re.sub(r"[-._]+", "_", dist_name)
    if module[0].isdigit():
        # Python identifiers cannot start with a digit, but project names can.
        module = f"_{module}"
    return module


def _normalise_version(version: str) -> str:
    try:
        return str(Version(version))
    except InvalidVersion as exc:
        raise MetadataError(
            f"{version!r} is not a valid PEP 440 version: {exc}"
        ) from exc


def _default_alias(binary_name: str) -> str:
    return Path(binary_name).stem or binary_name


# Rendering


def scaffold_project(spec: PackageSpec, binary: Path, root: Path) -> Path:
    """Write the full project tree under `root` and stage the binary.

    Returns the project root (the directory containing `pyproject.toml`).
    """
    root = Path(root)
    pkg_dir: Path = root / "src" / spec.module
    pkg_dir.mkdir(parents=True, exist_ok=True)

    (root / "pyproject.toml").write_text(render_pyproject(spec), encoding="utf-8")
    (root / "README.md").write_text(render_readme(spec), encoding="utf-8")
    (pkg_dir / "__init__.py").write_text(render_init(spec), encoding="utf-8")
    (pkg_dir / "__main__.py").write_text(render_main(spec), encoding="utf-8")

    for destination in staged_paths(spec, root):
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage_binary(binary, destination)
    return root


def staged_paths(spec: PackageSpec, root: Path) -> list[Path]:
    """Where the executable is written on disk, before the build runs.

    In direct mode the installed command is named after the file in
    `.data/scripts/`, so each alias needs its own copy.
    """
    root = Path(root)
    if spec.launcher is Launcher.SHIM:
        return [root / "src" / spec.module / "bin" / spec.binary_name]
    return [root / SCRIPTS_DIR / alias for alias in spec.aliases]


def archive_executables(spec: PackageSpec) -> set[str]:
    """Archive members that must carry mode 0o755 in the finished wheel."""
    if spec.launcher is Launcher.SHIM:
        return {f"{spec.module}/bin/{spec.binary_name}"}
    data_dir = f"{spec.dist_name.replace('-', '_')}-{spec.version}.data/scripts"
    return {f"{data_dir}/{alias}" for alias in spec.aliases}


def stage_binary(source: Path, destination: Path) -> Path:
    """Copy `source` into the package and mark it executable.

    The mode is set explicitly rather than inherited: a binary downloaded from
    a release tarball or extracted from a zip often arrives as 0o644, and the
    wheel must ship it executable.
    """
    shutil.copyfile(source, destination)
    destination.chmod(
        stat.S_IRWXU  # rwx for owner
        | stat.S_IRGRP
        | stat.S_IXGRP  # r-x for group
        | stat.S_IROTH
        | stat.S_IXOTH  # r-x for others
    )  # == 0o755
    return destination


def render_pyproject(spec: PackageSpec) -> str:
    extra: list[str] = []
    if spec.license:
        extra.append(f"license = {toml_str(spec.license)}")
    if spec.authors:
        entries: list[str] = []
        for name, email in spec.authors:
            parts: list[str] = []
            if name:
                parts.append(f"name = {toml_str(name)}")
            if email:
                parts.append(f"email = {toml_str(email)}")
            entries.append("{ " + ", ".join(parts) + " }")
        extra.append("authors = [" + ", ".join(entries) + "]")
    if spec.keywords:
        extra.append(f"keywords = {toml_array(spec.keywords)}")
    if spec.classifiers:
        extra.append(f"classifiers = {toml_array(spec.classifiers)}")

    # In direct mode the binary itself lands in the scripts directory, so a
    # console script of the same name would overwrite it during install.
    scripts_table = ""
    if spec.launcher is Launcher.SHIM:
        rows = "\n".join(
            f"{toml_str(alias)} = {toml_str(spec.module + '.__main__:main')}"
            for alias in spec.aliases
        )
        scripts_table = f"\n[project.scripts]\n{rows}\n"

    urls_table = ""
    if spec.urls:
        rows = "\n".join(f"{toml_str(k)} = {toml_str(v)}" for k, v in spec.urls.items())
        urls_table = f"\n[project.urls]\n{rows}\n"

    data_table = ""
    if spec.launcher is Launcher.DIRECT:
        data_table = DATA_TABLE.substitute(scripts_dir=SCRIPTS_DIR)

    return PYPROJECT.substitute(
        name=toml_str(spec.dist_name),
        version=toml_str(spec.version),
        description=toml_str(spec.description),
        requires_python=toml_str(spec.requires_python),
        project_extra=("\n".join(extra) + "\n") if extra else "",
        scripts_table=scripts_table,
        urls_table=urls_table,
        data_table=data_table,
        module=spec.module,
    )


def render_init(spec: PackageSpec) -> str:
    template = INIT_SHIM if spec.launcher is Launcher.SHIM else INIT_DIRECT
    imports = (
        "from pathlib import Path\n"
        if spec.launcher is Launcher.SHIM
        else "import sysconfig\nfrom pathlib import Path\n"
    )
    return template.substitute(
        dist_name=spec.dist_name,
        binary_name=spec.installed_name,
        version=spec.version,
        imports=imports,
    )


def render_main(spec: PackageSpec) -> str:
    return MAIN.substitute(
        dist_name=spec.dist_name,
        binary_name=spec.installed_name,
    )


def render_readme(spec: PackageSpec) -> str:
    alias_list: str = ", ".join(f"`{a}`" for a in spec.aliases)
    return README.substitute(
        dist_name=spec.dist_name,
        binary_name=spec.binary_name,
        module=spec.module,
        alias_list=alias_list,
        platform_tag=spec.platform_tag,
        launcher=spec.launcher.value,
    )
