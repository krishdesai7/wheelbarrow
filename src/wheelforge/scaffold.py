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

import hashlib
import re
import shutil
import stat
import textwrap
from dataclasses import dataclass, field
from enum import StrEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final

from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import InvalidVersion, Version

from .errors import MetadataError
from .templates import (
    DATA_TABLE,
    INIT_DIRECT,
    INIT_SHIM,
    LAUNCHER_NOTES,
    MAIN,
    PROVENANCE_INTROS,
    PYPROJECT,
    README,
    Template,
    toml_array,
    toml_str,
)

if TYPE_CHECKING:
    from .probe import BinaryInfo

#: Directory, relative to the project root, holding binaries destined for
#: `.data/scripts/`. Deliberately outside `src/` so it is not package data.
SCRIPTS_DIR: Final[str] = "scripts"

#: Suffixes Windows requires in order to treat a file on `PATH` as executable
#: (its `PATHEXT`). Direct mode renames the staged file after its alias, and
#: dropping `.exe` there would install something Windows refuses to run. Every
#: other suffix is still dropped: `.sh` and `.py` carry no such meaning, and on
#: POSIX an extension on a command name is just noise.
WINDOWS_EXEC_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".exe", ".com", ".bat", ".cmd"}
)


#: How a format identifies itself in prose. `describe()` on `BinaryInfo` is the
#: terse form meant for a terminal; this is the one that ends up on PyPI.
FORMAT_NAMES: Final[dict[str, str]] = {
    "elf": "ELF executable",
    "macho": "Mach-O executable",
    "macho-universal": "Mach-O universal binary",
    "pe": "PE executable",
}

LINKAGE_NAMES: Final[dict[str, str]] = {
    "static": "statically linked",
    "glibc": "dynamically linked against glibc",
    "musl": "dynamically linked against musl",
}

#: The glibc floor a manylinux tag claims, wherever it sits in a tag set.
_MANYLINUX_RE: Final[re.Pattern[str]] = re.compile(r"manylinux_(\d+)_(\d+)_")

#: Generated Markdown is wrapped to this, so a long interpreter path or tag
#: does not leave a single 200-column line in a file people will read.
_WRAP: Final[int] = 79


class Launcher(StrEnum):
    """How the packaged binary is exposed on `PATH`."""

    #: Binary installed straight into the environment's scripts directory.
    #: No Python runs on invocation.
    DIRECT = auto()
    #: Console script entry point that `execv`s a binary inside the package.
    SHIM = auto()


@dataclass(frozen=True)
class Provenance:
    """What the generated README says about the file the wheel wraps.

    Prose rather than raw fields, because the README is the only consumer and
    the cases that need explaining -- a script, a static binary, a universal
    one -- are better decided here, where they can be tested, than inside a
    template.
    """

    #: Digest of the file exactly as packaged, so a reader can check it against
    #: whatever the tool's own publisher lists.
    sha256: str
    #: One line naming the format, the platform and the linkage.
    kind: str
    #: What the wheel tag means in words, or empty when it speaks for itself.
    note: str = ""


@dataclass(frozen=True)
class Variant:
    """One wheel in a multi-platform set, as the README lists it.

    A batch shares a project name, so PyPI shows a single description page for
    all of it -- and picks one wheel's METADATA to render. A README describing
    only the file in its own wheel is therefore wrong on that page eleven times
    out of eleven, which is what this exists to fix.
    """

    platform_tag: str
    sha256: str
    #: The `Provenance.kind` line for this variant, or empty if never inspected.
    kind: str = ""


def describe_input(info: BinaryInfo, binary: Path, platform_tag: str) -> Provenance:
    """Summarise a packaged file for its README.

    `platform_tag` is passed in rather than derived so that every claim made
    here is a claim the wheel actually carries. Under `--platform-tag` the tag
    and the binary can disagree, and then the honest thing is to describe the
    file and stay quiet about what the tag means.
    """
    return Provenance(
        sha256=_sha256(binary),
        kind=_describe_kind(info),
        note=_describe_tag(info, platform_tag),
    )


def _sha256(path: Path) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _describe_kind(info: BinaryInfo) -> str:
    if info.is_script:
        return f"script run by `{info.interpreter}`"

    parts: list[str] = [
        FORMAT_NAMES.get(info.format, info.format),
        f"{info.os}/{info.arch}",
    ]
    linkage: str | None = LINKAGE_NAMES.get(info.libc or "")
    if linkage:
        parts.append(linkage)
    if info.macos_min:
        parts.append(f"macOS {info.macos_min[0]}.{info.macos_min[1]}+")
    if info.is_universal:
        parts.append("slices: " + ", ".join(info.slices))
    return ", ".join(parts)


def _describe_tag(info: BinaryInfo, tag: str) -> str:
    """Explain the tag, but only where it and the binary agree.

    Each branch is guarded on what the tag actually says, so an overridden tag
    never gets a gloss drawn from the binary it contradicts.
    """
    if info.is_script and tag == "any":
        return (
            f"The tag is `any` because no wheel tag can express "
            f"“needs {info.interpreter}”. This wheel will therefore "
            f"install anywhere, including on systems where that interpreter "
            f"does not exist."
        )
    if info.libc == "static" and "." in tag:
        return (
            "Statically linked, so it needs no C library at all. The tag is a "
            "compressed set naming both families deliberately: manylinux alone "
            "would withhold it from Alpine, and musllinux alone from glibc "
            "systems on architectures with no glibc build."
        )
    if info.libc == "glibc":
        floor: re.Match[str] | None = _MANYLINUX_RE.search(tag)
        if floor:
            return (
                f"Dynamically linked against glibc. It needs version "
                f"{floor[1]}.{floor[2]} or newer, which is what the manylinux "
                f"tag records."
            )
    if info.libc == "musl" and "musllinux" in tag:
        return "Dynamically linked against musl, so it installs on Alpine and kin."
    if info.is_universal and tag.endswith("universal2"):
        return (
            f"A universal binary carrying {' and '.join(info.slices)}; the "
            f"`universal2` tag covers both."
        )
    return ""


@dataclass
class PackageSpec:
    """Everything needed to render a project, already validated."""

    dist_name: str  # PEP 503 normalised, e.g. "wheelforge-bin"
    module: str  # importable, e.g. "wheelforge_bin"
    version: str  # PEP 440 normalised
    binary_name: str  # file name inside bin/, e.g. "wf"
    aliases: list[str]  # console scripts to expose
    platform_tag: str
    launcher: Launcher = Launcher.DIRECT
    description: str = ""
    requires_python: str = ">=3.8"
    licence: str | None = None
    authors: list[tuple[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    classifiers: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    #: Optional: callers that inspected the input can describe it in the README.
    provenance: Provenance | None = None
    #: Every wheel built alongside this one, when it is part of a batch. Left
    #: empty for a lone wheel, whose README describes just itself.
    variants: list[Variant] = field(default_factory=list)

    @property
    def installed_names(self) -> list[str]:
        """File names the executable carries once installed.

        In shim mode it keeps its original name inside the package, so there is
        one, whatever the aliases. In direct mode the staged file *becomes* the
        command, so there is one per alias, named after it -- except that a
        Windows executable suffix is carried over, because a `starship.exe`
        installed as `Scripts\\starship` is a file Windows will not run.
        """
        if self.launcher is Launcher.SHIM:
            return [self.binary_name]

        suffix: str = Path(self.binary_name).suffix
        if suffix.lower() not in WINDOWS_EXEC_SUFFIXES:
            suffix = ""
        # An alias that already spells the suffix out must not gain a second.
        return [
            alias if alias.lower().endswith(suffix.lower()) else alias + suffix
            for alias in self.aliases
        ]

    @property
    def installed_name(self) -> str:
        """File name behind the first alias, which the templates refer to."""
        return self.installed_names[0]


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
    licence: str | None = None,
    author: str | None = None,
    author_email: str | None = None,
    keywords: list[str] | None = None,
    homepage: str | None = None,
    provenance: Provenance | None = None,
    variants: list[Variant] | None = None,
) -> PackageSpec:
    """Validate and normalise user-supplied metadata into a `PackageSpec`."""
    dist_name: str = _normalise_name(name)
    module: str = _module_name(dist_name)
    normalised_version: str = _normalise_version(version)

    resolved_aliases: list[str] = aliases or [default_alias(binary_name)]
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
        licence=licence,
        authors=authors,
        keywords=keywords or [],
        urls=urls,
        provenance=provenance,
        variants=list(variants or []),
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


def default_alias(binary_name: str) -> str:
    """The console script name a binary gets when none was asked for."""
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
    return [root / SCRIPTS_DIR / name for name in spec.installed_names]


def archive_executables(spec: PackageSpec) -> set[str]:
    """Archive members that must carry mode 0o755 in the finished wheel."""
    if spec.launcher is Launcher.SHIM:
        return {f"{spec.module}/bin/{spec.binary_name}"}
    data_dir = f"{spec.dist_name.replace('-', '_')}-{spec.version}.data/scripts"
    return {f"{data_dir}/{name}" for name in spec.installed_names}


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


def _render_project_extra(spec: PackageSpec) -> str:
    """Render optional fields in the generated ``[project]`` table."""
    extra: list[str] = []
    if spec.licence:
        # PEP 621 fixes the spelling of the key itself, so this one stays
        # American even though our own identifiers do not. Emitting `licence`
        # here is silently ignored by the backend and drops the field from the
        # wheel's METADATA entirely.
        extra.append(f"license = {toml_str(spec.licence)}")
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
    return ("\n".join(extra) + "\n") if extra else ""


def render_pyproject(spec: PackageSpec) -> str:
    # In direct mode the binary itself lands in the scripts directory, so a
    # console script of the same name would overwrite it during install.
    scripts_table = ""
    if spec.launcher is Launcher.SHIM:
        rows = "\n".join(
            f"{toml_str(alias)} = {toml_str(spec.module + '.__main__:main')}"
            for alias in spec.aliases
        )
        scripts_table: str = f"\n[project.scripts]\n{rows}\n"

    urls_table = ""
    if spec.urls:
        rows: str = "\n".join(
            f"{toml_str(k)} = {toml_str(v)}" for k, v in spec.urls.items()
        )
        urls_table: str = f"\n[project.urls]\n{rows}\n"

    data_table = ""
    if spec.launcher is Launcher.DIRECT:
        data_table: str = DATA_TABLE.substitute(scripts_dir=SCRIPTS_DIR)

    return PYPROJECT.substitute(
        name=toml_str(spec.dist_name),
        version=toml_str(spec.version),
        description=toml_str(spec.description),
        requires_python=toml_str(spec.requires_python),
        project_extra=_render_project_extra(spec),
        scripts_table=scripts_table,
        urls_table=urls_table,
        data_table=data_table,
        module=spec.module,
    )


def render_init(spec: PackageSpec) -> str:
    template: Template = INIT_SHIM if spec.launcher is Launcher.SHIM else INIT_DIRECT
    imports: str = (
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
    """Render the README that becomes the wheel's long description.

    The provenance block degrades rather than breaks when nothing inspected the
    input: a library caller who builds a `PackageSpec` by hand still gets a
    correct README, just without the digest and the description of the file.
    """
    batched: bool = len(spec.variants) > 1
    facts: list[str]
    note: str = ""

    if batched:
        facts = _variant_facts(spec)
    else:
        facts = [f"- **file** — `{spec.binary_name}`"]
        if spec.provenance is not None:
            facts += [
                f"- **kind** — {spec.provenance.kind}",
                f"- **sha256** — `{spec.provenance.sha256}`",
            ]
            note = spec.provenance.note
        facts.append(f"- **wheel tag** — `{spec.platform_tag}`")

    return README.substitute(
        dist_name=spec.dist_name,
        binary_name=spec.binary_name,
        module=spec.module,
        alias_list=", ".join(f"`{a}`" for a in spec.aliases),
        launcher_note=textwrap.fill(LAUNCHER_NOTES[spec.launcher.value], _WRAP),
        provenance_intro=textwrap.fill(
            PROVENANCE_INTROS["set" if batched else "solo"], _WRAP
        ),
        provenance_facts="\n".join(facts),
        tag_note=f"\n{textwrap.fill(note, _WRAP)}\n" if note else "",
    )


def _variant_facts(spec: PackageSpec) -> list[str]:
    """List every wheel in the set, digest included.

    No entry is marked as "this one": the reader is on a project page that
    renders one wheel's description for all of them, so a "this wheel" would
    point at whichever file PyPI happened to render and mean nothing to
    somebody installing a different platform's.

    `tag_note` is dropped in this mode for the same reason. Each note explains
    one tag, and the eleven tags here do not share an explanation.

    The `file` line goes too: the packaged name varies across the set --
    `starship` on Linux, `starship.exe` on Windows -- so stating one of them is
    false for the rest. Without it every wheel in the batch renders exactly the
    same block, which is the property that makes it safe for PyPI to pick any
    one of them as the project description.
    """
    facts: list[str] = []
    for variant in sorted(spec.variants, key=lambda v: v.platform_tag):
        facts.append(f"- **`{variant.platform_tag}`**")
        if variant.kind:
            # Continuation lines of a list item: markdown reflows them into one
            # entry, and wrapping keeps the source inside the same 88 columns
            # every other generated line respects.
            facts.append(
                textwrap.fill(
                    variant.kind,
                    _WRAP,
                    initial_indent="  ",
                    subsequent_indent="  ",
                )
            )
        facts.append(f"  sha256 `{variant.sha256}`")
    return facts
