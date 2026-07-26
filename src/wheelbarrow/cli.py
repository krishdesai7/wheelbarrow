"""Command line interface for wheelbarrow."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .builder import BuildResult, build_package
from .errors import WheelbarrowError
from .probe import BinaryInfo, inspect_binary
from .publish import PublishPlan, plan_publish, run_publish
from .scaffold import PackageSpec, make_spec
from .tags import full_tag, platform_tag

app = typer.Typer(
    rich_markup_mode="markdown",
    no_args_is_help=True,
    help="Convert OS binaries into PyPI-installable Python wheels.",
)

console = Console()
err_console = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"wheelbarrow {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the wheelbarrow version and exit.",
        ),
    ] = False,
) -> None:
    """Package prebuilt command line tools as Python wheels."""


@app.command("inspect")
def inspect_command(
    binary: Annotated[Path, typer.Argument(help="Path to the executable to inspect.")],
    glibc: Annotated[
        str | None,
        typer.Option(
            "--glibc", help="Override the manylinux glibc baseline, e.g. `2.28`."
        ),
    ] = None,
) -> None:
    """Report the platform a binary targets, and the wheel tag it would get.

    Reads the executable's own headers, so it works on binaries built for a
    platform other than the one you are running on.
    """
    try:
        info: BinaryInfo = inspect_binary(binary)
        tag: str = platform_tag(info, glibc_version=glibc)
    except WheelbarrowError as exc:
        raise _fail(str(exc)) from exc

    _print_info(info, tag)


def _print_info(info: BinaryInfo, tag: str) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column(style="bold")

    table.add_row("file", str(info.path))
    table.add_row("format", info.format)
    table.add_row("os", info.os)
    table.add_row("arch", info.arch)
    if info.libc:
        table.add_row("libc", info.libc)
    if info.macos_min:
        table.add_row("min macOS", f"{info.macos_min[0]}.{info.macos_min[1]}")
    if info.is_universal:
        table.add_row("slices", ", ".join(info.slices))
    table.add_row("wheel tag", f"[green]{full_tag(tag)}[/]")

    console.print(table)


@app.command("build")
def build_command(
    binary: Annotated[Path, typer.Argument(help="Path to the executable to package.")],
    name: Annotated[
        str, typer.Option("--name", "-n", help="PyPI project name, e.g. `ripgrep-bin`.")
    ],
    version: Annotated[
        str, typer.Option("--version", "-V", help="Package version (PEP 440).")
    ],
    alias: Annotated[
        list[str] | None,
        typer.Option(
            "--alias",
            "-a",
            help="Console script name to expose. Repeatable. "
            "Defaults to the binary's file name.",
        ),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Directory to write the wheel to.")
    ] = Path("dist"),
    description: Annotated[
        str, typer.Option("--description", "-d", help="One-line package summary.")
    ] = "",
    license_: Annotated[
        str | None, typer.Option("--license", help="License expression, e.g. `MIT`.")
    ] = None,
    author: Annotated[str | None, typer.Option("--author", help="Author name.")] = None,
    author_email: Annotated[
        str | None, typer.Option("--author-email", help="Author email.")
    ] = None,
    homepage: Annotated[
        str | None, typer.Option("--homepage", help="Project homepage URL.")
    ] = None,
    keyword: Annotated[
        list[str] | None,
        typer.Option("--keyword", help="Package keyword. Repeatable."),
    ] = None,
    requires_python: Annotated[
        str, typer.Option("--requires-python", help="Python requirement for the wheel.")
    ] = ">=3.8",
    platform_tag_override: Annotated[
        str | None,
        typer.Option(
            "--platform-tag",
            help="Use this platform tag verbatim instead of detecting one, "
            "e.g. `manylinux_2_17_x86_64`.",
        ),
    ] = None,
    glibc: Annotated[
        str | None,
        typer.Option("--glibc", help="manylinux glibc baseline, e.g. `2.28`."),
    ] = None,
    macos_min: Annotated[
        str | None,
        typer.Option("--macos-min", help="Minimum macOS version, e.g. `12.0`."),
    ] = None,
    universal2: Annotated[
        bool,
        typer.Option("--universal2", help="Tag a fat Mach-O binary as `universal2`."),
    ] = False,
    keep_project: Annotated[
        Path | None,
        typer.Option(
            "--keep-project",
            help="Write the generated project here instead of discarding it.",
        ),
    ] = None,
    isolated: Annotated[
        bool,
        typer.Option("--isolated", help="Build in an isolated PEP 517 environment."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show build backend output.")
    ] = False,
) -> None:
    """Build a wheel that bundles **BINARY** and exposes it as a console script.

    The binary's headers decide the wheel's platform tag, so a wheel built from
    a Linux binary will only install on Linux.

    Example:

        wheelbarrow build ./rg --name ripgrep-bin --version 14.1.0 --alias rg
    """
    try:
        info: BinaryInfo | None
        tag: str
        info, tag = _resolve_tag(
            binary,
            platform_tag_override,
            glibc=glibc,
            macos_min=macos_min,
            universal2=universal2,
        )
        spec: PackageSpec = make_spec(
            name=name,
            version=version,
            binary_name=binary.name,
            platform_tag=tag,
            aliases=list(alias) if alias else None,
            description=description,
            requires_python=requires_python,
            license=license_,
            author=author,
            author_email=author_email,
            keywords=list(keyword) if keyword else None,
            homepage=homepage,
        )

        if info is not None and info.is_universal:
            slices: str = "+".join(info.slices)
            if tag.endswith("universal2"):
                console.print(
                    f"[dim]note:[/] {binary.name} is a universal binary "
                    f"({slices}); tagged [bold]universal2[/], valid for both."
                )
            else:
                console.print(
                    f"[yellow]note:[/] {binary.name} is a universal binary "
                    f"({slices}), but only [bold]{info.arch}[/] maps to a wheel "
                    f"tag; the other slices will not be advertised."
                )

        result: BuildResult = build_package(
            binary,
            spec,
            output,
            isolated=isolated,
            verbose=verbose,
            keep_project=keep_project,
        )
    except WheelbarrowError as exc:
        raise _fail(str(exc)) from exc

    console.print(
        f"[bold green]built[/] {result.wheel} "
        f"[dim]({_human_size(result.wheel.stat().st_size)})[/]"
    )
    console.print(f"[dim]  tag      [/] {result.tag}")
    console.print(f"[dim]  scripts  [/] {', '.join(spec.aliases)}")
    if result.project_dir:
        console.print(f"[dim]  project  [/] {result.project_dir}")


def _human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size} B"  # pragma: no cover - loop always returns


def _resolve_tag(
    binary: Path,
    override: str | None,
    *,
    glibc: str | None,
    macos_min: str | None,
    universal2: bool,
) -> tuple[BinaryInfo | None, str]:
    """Return the platform tag to use, and the inspection result if we have one."""
    if override:
        return None, override

    info: BinaryInfo = inspect_binary(binary)
    tag: str = platform_tag(
        info,
        glibc_version=glibc,
        macos_min=_parse_macos_min(macos_min),
        universal2=universal2,
    )
    return info, tag


def _parse_macos_min(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    parts: list[str] = value.replace("_", ".").split(".")
    try:
        major = int(parts[0])
        minor: int = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as exc:
        raise WheelbarrowError(
            f"--macos-min expects a version like `12.0`, got {value!r}"
        ) from exc
    return major, minor


@app.command("publish")
def publish_command(
    wheels: Annotated[list[Path], typer.Argument(help="Wheel files to upload.")],
    index: Annotated[
        str | None,
        typer.Option("--index", help="Named index from your uv configuration."),
    ] = None,
    publish_url: Annotated[
        str | None,
        typer.Option("--publish-url", help="Upload URL of the target index."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="API token. Passed to uv through the environment, never argv.",
        ),
    ] = None,
    username: Annotated[
        str | None, typer.Option("--username", help="Username for the index.")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the uv command without running it."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Upload built wheels with `uv publish`.

    Publishing is permanent: PyPI does not allow re-uploading a version that
    has already been released, so wheelbarrow asks for confirmation first.
    """
    try:
        plan: PublishPlan = plan_publish(
            list(wheels),
            index=index,
            publish_url=publish_url,
            token=token,
            username=username,
        )
    except WheelbarrowError as exc:
        raise _fail(str(exc)) from exc

    target: str = publish_url or index or "PyPI"
    console.print(f"[bold]About to publish {len(plan.files)} file(s) to {target}:[/]")
    for f in plan.files:
        console.print(f"  {f}")

    if dry_run:
        console.print(f"\n[dim]dry run, would execute:[/] {plan.display()}")
        return

    if not yes:
        console.print(
            "[yellow]This cannot be undone: a released version cannot be "
            "re-uploaded.[/]"
        )
        if not typer.confirm("Publish now?"):
            console.print("aborted")
            raise typer.Exit(code=1)

    try:
        run_publish(plan, token=token)
    except WheelbarrowError as exc:
        raise _fail(str(exc)) from exc

    console.print("[bold green]published[/]")


if __name__ == "__main__":
    app()
