"""Command line interface for wheelbarrow."""

from pathlib import Path
from typing import Annotated, Protocol

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, pypi
from .builder import BuildResult, build_package
from .errors import WheelbarrowError
from .probe import BinaryInfo, inspect_binary
from .publish import PublishPlan, plan_publish, resolve_token, run_publish
from .pypi import NameStatus
from .scaffold import Launcher, PackageSpec, make_spec
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


class _Helpable(Protocol):
    """Any context that can render help.

    Structural, because `Context.parent` is typed as click's own Context while
    typer hands out a subclass of it, and click is vendored inside typer rather
    than importable in its own right.
    """

    def get_help(self) -> str: ...


def _show_help(ctx: _Helpable) -> None:
    """Print the help for `ctx`'s command, exactly as `--help` would.

    This mirrors click's own `--help` handler rather than improving on it, so
    `wheelbarrow help build` and `wheelbarrow build --help` cannot drift apart.
    Under rich markup mode `get_help` renders straight to stdout and returns an
    empty string, and echoing that is what yields the trailing blank line
    `--help` also prints; the return value only carries text in the plain
    fallback. `typer.echo` rather than the rich console, because help text is
    full of `[OPTIONS]`-style brackets that the console would read as markup.
    """
    typer.echo(ctx.get_help())


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


@app.command("inspect", no_args_is_help=True)
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


@app.command("build", no_args_is_help=True)
def build_command(
    binary: Annotated[Path, typer.Argument(help="Path to the executable to package.")],
    name: Annotated[str, typer.Option("--name", "-n", help="PyPI project name.")],
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
    licence_: Annotated[
        str | None, typer.Option("--licence", help="License expression, e.g. `MIT`.")
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
    launcher: Annotated[
        Launcher,
        typer.Option(
            "--launcher",
            help="**direct** installs the binary straight onto `PATH` "
            "(no Python startup cost). **shim** exposes a console script that "
            "`execv`s a binary kept inside the package.",
        ),
    ] = Launcher.DIRECT,
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
    check_name: Annotated[
        bool,
        typer.Option(
            "--check-name/--no-check-name",
            help="Ask PyPI whether **--name** is already registered, and warn "
            "if it is. Advisory only; never fails the build.",
        ),
    ] = True,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show build backend output.")
    ] = False,
) -> None:
    """Build a wheel that bundles **BINARY** and exposes it as a console script.

    The binary's headers decide the wheel's platform tag, so a wheel built from
    a Linux binary will only install on Linux.

    Example:

        wheelbarrow build <path-to-binary> -n <binary-name> -V <version> -a <alias>
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
            launcher=launcher,
            description=description,
            requires_python=requires_python,
            licence=licence_,
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

        if check_name:
            _warn_if_name_is_taken(spec.dist_name, verbose=verbose)

        if spec.launcher is Launcher.DIRECT and len(spec.aliases) > 1:
            # Each alias *is* the installed file, so each needs its own copy.
            console.print(
                f"[yellow]note:[/] {len(spec.aliases)} aliases in direct mode "
                f"means {len(spec.aliases)} copies of the binary in the wheel. "
                f"Use --launcher shim to share one copy."
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
    console.print(f"[dim]  launcher [/] {spec.launcher.value}")
    console.print(f"[dim]  scripts  [/] {', '.join(spec.aliases)}")
    if result.project_dir:
        console.print(f"[dim]  project  [/] {result.project_dir}")


def _warn_if_name_is_taken(dist_name: str, *, verbose: bool) -> None:
    """Report a name that is already registered on PyPI.

    Only the taken case is worth interrupting for. A free name needs no
    comment, and an unreachable index is not the user's problem unless they
    asked for detail, so both stay quiet.
    """
    status: NameStatus = pypi.check_name(dist_name)
    if status is NameStatus.TAKEN:
        console.print(
            f"[yellow]note:[/] [bold]{dist_name}[/] is already registered on "
            f"PyPI. Publishing will only work if the project is yours; "
            f"otherwise choose a different --name."
        )
    elif status is NameStatus.UNKNOWN and verbose:
        console.print(
            f"[dim]note:[/] could not reach PyPI to check whether "
            f"{dist_name} is registered; continuing."
        )


def _human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
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


@app.command("publish", no_args_is_help=True)
def publish_command(
    wheels: Annotated[list[Path], typer.Argument(help="Wheel files to upload.")],
    index: Annotated[
        str | None,
        typer.Option(help="Named index from your uv configuration."),
    ] = None,
    publish_url: Annotated[
        str | None,
        typer.Option(help="Upload URL of the target index."),
    ] = None,
    username: Annotated[
        str | None, typer.Option(help="Username for the index.")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(help="Show the uv command without running it."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Upload built wheels with `uv publish`.

    The API token is read from `UV_PUBLISH_TOKEN` in the environment; there is
    no option for it, so it cannot end up in your shell history.

    Publishing is permanent: PyPI does not allow re-uploading a version that
    has already been released, so wheelbarrow asks for confirmation first.
    """
    try:
        plan: PublishPlan = plan_publish(
            list(wheels),
            index=index,
            publish_url=publish_url,
            username=username,
        )
        if plan.needs_token and not dry_run:
            # Checked up front so a missing token is reported before the user
            # is asked to confirm, rather than after they commit to the upload.
            resolve_token()
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
        run_publish(plan)
    except WheelbarrowError as exc:
        raise _fail(str(exc)) from exc

    console.print("[bold green]published[/]")


@app.command("help")
def help_command(
    ctx: typer.Context,
    command: Annotated[
        str | None,
        typer.Argument(help="Command to describe. Omitted, describes wheelbarrow."),
    ] = None,
) -> None:
    """Show help for a command, equivalent to `COMMAND --help`."""
    # ctx is this command's own context; its parent is the top-level group,
    # which owns both the root help text and the table of subcommands.
    # Left unannotated: `parent` is typed as click's base Context, not typer's.
    root_ctx = ctx.parent or ctx
    if command is None:
        _show_help(root_ctx)
        return

    group = root_ctx.command
    subcommand = group.get_command(root_ctx, command)  # type: ignore[attr-defined]
    if subcommand is None:
        known: str = ", ".join(sorted(group.list_commands(root_ctx)))  # type: ignore[attr-defined]
        raise _fail(f"unknown command {command!r}. Available commands: {known}")

    # Parenting the context to the root keeps the usage line fully qualified,
    # so it reads `wheelbarrow build ...` rather than just `build ...`.
    _show_help(typer.Context(subcommand, info_name=command, parent=root_ctx))


if __name__ == "__main__":
    app()
