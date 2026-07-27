"""CLI surface: how help is reached, and when an error is shown instead."""

import json
import re
import shlex
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from wheelbarrow import cli
from wheelbarrow.cli import app
from wheelbarrow.pypi import NameStatus

from .conftest import make_elf, make_pe

runner = CliRunner()
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def wide_console(monkeypatch) -> None:
    """Stop rich wrapping messages the assertions below match on.

    The consoles are module-level, so they take their width at import time --
    80 columns when nothing is a terminal, which is narrow enough to split a
    file path or an option name across two lines and defeat an `in` check.
    """
    monkeypatch.setattr(cli.console, "width", 300)
    monkeypatch.setattr(cli.err_console, "width", 300)


#: Every command the app exposes, `help` included -- it documents itself.
COMMANDS = ["fetch", "inspect", "build", "publish", "help"]


def run(*args: str) -> Result:
    """Invoke the app under its installed name, so usage lines read correctly."""
    return runner.invoke(app, list(args), prog_name="wheelbarrow")


def everything_written(result: Result) -> str:
    """Both streams, since rich sends errors to stderr and help to stdout."""
    return ANSI_ESCAPE_RE.sub("", result.output + (result.stderr or ""))


def plain_output(result: Result) -> str:
    return ANSI_ESCAPE_RE.sub("", result.output)


class TestBareSubcommandShowsHelp:
    """`wheelbarrow COMMAND` with no arguments behaves like the bare app."""

    @pytest.mark.parametrize("command", ["fetch", "inspect", "build", "publish"])
    def test_no_arguments_prints_the_commands_help(self, command) -> None:
        result = run(command)
        rendered = plain_output(result)
        assert f"Usage: wheelbarrow {command}" in rendered
        assert "--help" in rendered

    @pytest.mark.parametrize("command", ["fetch", "inspect", "build", "publish"])
    def test_no_arguments_matches_explicit_help(self, command) -> None:
        # Compared modulo trailing whitespace: click's no-args path omits the
        # blank line that its `--help` handler prints after the rendered help.
        assert run(command).output.rstrip() == run(command, "--help").output.rstrip()


class TestHelpCommand:
    """`wheelbarrow help COMMAND` is an alias for `COMMAND --help`."""

    @pytest.mark.parametrize("command", COMMANDS)
    def test_matches_the_help_flag(self, command) -> None:
        assert run("help", command).output == run(command, "--help").output

    @pytest.mark.parametrize("command", COMMANDS)
    def test_usage_line_is_fully_qualified(self, command) -> None:
        # The sub-context is parented to the root, so the usage line names the
        # program too rather than starting at the subcommand.
        assert f"Usage: wheelbarrow {command}" in plain_output(run("help", command))

    def test_without_an_argument_describes_the_app(self) -> None:
        assert run("help").output == run("--help").output

    def test_unknown_command_is_an_error_listing_the_real_ones(self) -> None:
        result = run("help", "frobnicate")
        assert result.exit_code == 1
        written = everything_written(result)
        assert "frobnicate" in written
        for command in COMMANDS:
            assert command in written


class TestNameCheck:
    """`build` warns about a registered name, but never fails over it."""

    @pytest.fixture
    def built(self, tmp_path, elf_binary, monkeypatch):
        """Run a build with a stubbed index, returning (invoke, calls)."""
        calls: list[str] = []

        def build_with(status, *extra: str) -> Result:
            def fake_check(name, **_kwargs):
                calls.append(name)
                return status

            monkeypatch.setattr("wheelbarrow.pypi.check_name", fake_check)
            return run(
                "build",
                str(elf_binary),
                "-n",
                "demo-bin",
                "-V",
                "1.0.0",
                "-o",
                str(tmp_path / "dist"),
                *extra,
            )

        return build_with, calls

    def test_a_taken_name_warns_but_still_builds(self, built) -> None:
        build_with, _ = built
        result = build_with(NameStatus.TAKEN)
        assert result.exit_code == 0
        written = everything_written(result)
        assert "already registered" in written
        assert "built" in written

    def test_an_available_name_says_nothing(self, built) -> None:
        build_with, _ = built
        result = build_with(NameStatus.AVAILABLE)
        assert result.exit_code == 0
        assert "already registered" not in everything_written(result)

    def test_an_unreachable_index_is_quiet_but_still_builds(self, built) -> None:
        """Building offline must not be noisy, nor fail."""
        build_with, _ = built
        result = build_with(NameStatus.UNKNOWN)
        assert result.exit_code == 0
        assert "could not reach" not in everything_written(result)
        assert "built" in everything_written(result)

    def test_verbose_explains_an_unreachable_index(self, built) -> None:
        build_with, _ = built
        result = build_with(NameStatus.UNKNOWN, "--verbose")
        assert "could not reach" in everything_written(result)

    def test_no_check_name_makes_no_request_at_all(self, built) -> None:
        build_with, calls = built
        result = build_with(NameStatus.TAKEN, "--no-check-name")
        assert result.exit_code == 0
        assert calls == []
        assert "already registered" not in everything_written(result)

    def test_the_normalised_name_is_what_gets_looked_up(self, built) -> None:
        """PyPI's simple index is keyed by the PEP 503 form."""
        build_with, calls = built
        build_with(NameStatus.AVAILABLE)
        assert calls == ["demo-bin"]


class TestScriptSupport:
    """A `#!` script packages like a binary, but constrains no platform."""

    def build_script(self, script, tmp_path, *extra: str) -> Result:
        return run(
            "build",
            str(script),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
            *extra,
        )

    def test_inspect_reports_the_interpreter(self, shell_script) -> None:
        result = run("inspect", str(shell_script))
        rendered = plain_output(result)
        assert result.exit_code == 0
        assert "script" in rendered
        assert "/bin/sh" in rendered
        assert "py3-none-any" in rendered

    def test_building_a_script_needs_no_platform_tag(
        self, shell_script, tmp_path
    ) -> None:
        result = self.build_script(shell_script, tmp_path)
        assert result.exit_code == 0
        assert (tmp_path / "dist" / "demo_bin-1.0.0-py3-none-any.whl").is_file()

    def test_the_any_tag_is_explained(self, shell_script, tmp_path) -> None:
        """`any` installs on Windows too, where /bin/sh does not exist."""
        written = everything_written(self.build_script(shell_script, tmp_path))
        assert "/bin/sh" in written
        assert "--platform-tag" in written

    def test_an_explicit_tag_says_nothing(self, shell_script, tmp_path) -> None:
        """The note is advice on a detected tag; an override is a decision."""
        result = self.build_script(
            shell_script, tmp_path, "--platform-tag", "manylinux_2_17_x86_64"
        )
        assert result.exit_code == 0
        assert "--platform-tag" not in everything_written(result)


class TestInspectReporting:
    """What the detailed single-file view has to show."""

    def test_the_measured_glibc_floor_is_shown(self, write_binary) -> None:
        """It decides the manylinux tag, so it must not be invisible."""
        binary = write_binary("tool", make_elf(0x3E, glibc_versions=["GLIBC_2.18"]))
        rendered = plain_output(run("inspect", str(binary)))
        assert "glibc min" in rendered
        assert "2.18" in rendered
        assert "manylinux_2_18_x86_64" in rendered

    def test_nothing_is_claimed_when_nothing_was_measured(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E))
        assert "glibc min" not in plain_output(run("inspect", str(binary)))

    def test_a_static_binary_shows_the_compressed_tag_set(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E, interp=None))
        rendered = plain_output(run("inspect", str(binary)))
        assert "manylinux_2_17_x86_64.musllinux_1_2_x86_64" in rendered


class TestDirectoryInput:
    """`inspect` and `build` take a directory of binaries, not just one file."""

    @pytest.fixture
    def release_dir(self, tmp_path):
        """Two platforms unpacked beside the archives they came out of."""
        root = tmp_path / "downloads"
        for name, data in [
            ("tool-linux", make_elf(0x3E, interp=None)),
            ("tool-windows", make_pe(0x8664)),
        ]:
            (root / name).mkdir(parents=True)
            (root / name / "tool").write_bytes(data)
            (root / f"{name}.tar.gz").write_bytes(b"\x1f\x8b\x08 not a real archive")
        return root

    def build_dir(self, path, tmp_path, *extra: str) -> Result:
        return run(
            "build",
            str(path),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
            *extra,
        )

    def test_inspect_tabulates_every_binary(self, release_dir) -> None:
        rendered = plain_output(run("inspect", str(release_dir)))
        assert "manylinux_2_17_x86_64" in rendered
        assert "win_amd64" in rendered
        assert "2 executable(s)" in rendered

    def test_inspect_says_what_it_passed_over(self, release_dir) -> None:
        """Silence about the archives would look like they had been packaged."""
        rendered = plain_output(run("inspect", str(release_dir)))
        assert "2 other file(s) ignored" in rendered

    def test_inspect_reports_an_untaggable_binary_rather_than_aborting(
        self, release_dir
    ) -> None:
        """Reporting is what inspect is for; refusing is build's job."""
        (release_dir / "bsd").mkdir()
        (release_dir / "bsd" / "tool").write_bytes(make_elf(0x3E, osabi=0x09))
        result = run("inspect", str(release_dir))
        assert result.exit_code == 0
        rendered = plain_output(result)
        assert "no tag" in rendered
        assert "freebsd" in rendered
        assert "win_amd64" in rendered  # the others are still listed

    def test_a_lone_file_still_prints_the_detailed_view(self, release_dir) -> None:
        rendered = plain_output(
            run("inspect", str(release_dir / "tool-linux" / "tool"))
        )
        assert "format" in rendered
        assert "arch" in rendered

    def test_build_produces_one_wheel_per_binary(self, release_dir, tmp_path) -> None:
        result = self.build_dir(release_dir, tmp_path)
        assert result.exit_code == 0
        wheels = sorted(p.name for p in (tmp_path / "dist").glob("*.whl"))
        assert wheels == [
            # Static, so it claims both libc families in one compressed set.
            "demo_bin-1.0.0-py3-none-manylinux_2_17_x86_64.musllinux_1_2_x86_64.whl",
            "demo_bin-1.0.0-py3-none-win_amd64.whl",
        ]

    def test_build_reports_each_wheel_and_the_total(
        self, release_dir, tmp_path
    ) -> None:
        written = plain_output(self.build_dir(release_dir, tmp_path))
        assert "building 2 wheels" in written
        assert "built 2 wheels" in written

    def test_build_refuses_an_untaggable_binary_naming_it(
        self, release_dir, tmp_path
    ) -> None:
        (release_dir / "bsd").mkdir()
        (release_dir / "bsd" / "tool").write_bytes(make_elf(0x3E, osabi=0x09))
        result = self.build_dir(release_dir, tmp_path)
        assert result.exit_code == 1
        written = everything_written(result)
        assert "bsd" in written
        assert not (tmp_path / "dist").exists()

    def test_every_untaggable_binary_is_named_at_once(
        self, release_dir, tmp_path
    ) -> None:
        """One re-run should be enough to fix the directory, not one per file."""
        for name in ("bsd", "netbsd"):
            (release_dir / name).mkdir()
            (release_dir / name / "tool").write_bytes(make_elf(0x3E, osabi=0x09))
        written = everything_written(self.build_dir(release_dir, tmp_path))
        assert "/bsd/" in written
        assert "/netbsd/" in written

    def test_an_explicit_platform_tag_cannot_cover_a_directory(
        self, release_dir, tmp_path
    ) -> None:
        """One tag over many binaries means they all overwrite one another."""
        result = self.build_dir(
            release_dir, tmp_path, "--platform-tag", "manylinux_2_17_x86_64"
        )
        assert result.exit_code == 1
        # The distinctive phrase: a tag collision would also name the option.
        assert "names one tag" in everything_written(result)
        assert not (tmp_path / "dist").exists()

    def test_it_still_works_on_a_single_file(self, release_dir, tmp_path) -> None:
        """--platform-tag is only refused for a directory of several."""
        result = self.build_dir(
            release_dir / "tool-linux" / "tool",
            tmp_path,
            "--platform-tag",
            "manylinux_2_28_x86_64",
        )
        assert result.exit_code == 0
        assert (
            tmp_path / "dist" / "demo_bin-1.0.0-py3-none-manylinux_2_28_x86_64.whl"
        ).is_file()

    def test_binaries_of_differing_names_are_refused(self, tmp_path) -> None:
        """Otherwise the installed command would change with the platform."""
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "tool-linux").write_bytes(make_elf(0x3E, interp=None))
        (root / "tool.exe").write_bytes(make_pe(0x8664))
        result = self.build_dir(root, tmp_path)
        assert result.exit_code == 1
        written = everything_written(result)
        assert "--alias" in written
        assert not (tmp_path / "dist").exists()

    def test_an_explicit_alias_settles_it(self, tmp_path) -> None:
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "tool-linux").write_bytes(make_elf(0x3E, interp=None))
        (root / "tool.exe").write_bytes(make_pe(0x8664))
        result = self.build_dir(root, tmp_path, "-a", "tool")
        assert result.exit_code == 0
        assert len(list((tmp_path / "dist").glob("*.whl"))) == 2

    def test_the_name_is_looked_up_once_for_the_whole_batch(
        self, release_dir, tmp_path, monkeypatch
    ) -> None:
        """Every wheel carries the same project name; one question suffices."""
        calls: list[str] = []
        monkeypatch.setattr(
            "wheelbarrow.pypi.check_name",
            lambda name, **_kw: calls.append(name) or NameStatus.AVAILABLE,
        )
        result = run(
            "build",
            str(release_dir),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
        )
        assert result.exit_code == 0
        assert calls == ["demo-bin"]

    def test_a_directory_with_nothing_in_it_is_an_error(self, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = run("inspect", str(empty))
        assert result.exit_code == 1
        assert "no executable" in everything_written(result)


class TestUntaggableBinariesComeWithTheFix:
    """Naming the problem is half of it; the `rm` that clears it is the rest.

    `build` refuses the whole batch, so the user has to delete something before
    anything works -- and working out which paths that means, with an unpacked
    directory sitting beside the archive it came from, is where they get it
    wrong and leave the tarball behind for the next extract to restore.
    """

    @pytest.fixture
    def with_bsd(self, tmp_path):
        """A fetch-shaped directory: unpacked trees beside their archives."""
        root = tmp_path / "downloads"
        for name, data in [
            ("tool-linux", make_elf(0x3E, interp=None)),
            ("tool-freebsd", make_elf(0x3E, osabi=0x09)),
        ]:
            (root / name).mkdir(parents=True)
            (root / name / "tool").write_bytes(data)
            (root / f"{name}.tar.gz").write_bytes(b"\x1f\x8b\x08 not a real archive")
        return root

    def test_inspect_spells_out_the_removal(self, with_bsd) -> None:
        rendered = plain_output(run("inspect", str(with_bsd)))
        assert "rm -r" in rendered
        assert str(with_bsd / "tool-freebsd") in rendered

    def test_the_archive_is_removed_too_not_just_the_directory(self, with_bsd) -> None:
        """Leaving it means the next --extract puts the binary straight back."""
        rendered = plain_output(run("inspect", str(with_bsd)))
        assert str(with_bsd / "tool-freebsd.tar.gz") in rendered

    def test_the_usable_binaries_are_not_offered_for_deletion(self, with_bsd) -> None:
        rendered = plain_output(run("inspect", str(with_bsd)))
        advice = rendered[rendered.index("rm -r") :]
        assert "tool-linux" not in advice

    def test_inspect_then_says_how_to_verify(self, with_bsd) -> None:
        rendered = plain_output(run("inspect", str(with_bsd)))
        assert f"wheelbarrow inspect {with_bsd}" in rendered

    def test_build_gives_the_same_removal(self, with_bsd, tmp_path) -> None:
        result = run(
            "build",
            str(with_bsd),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
        )
        assert result.exit_code == 1
        written = everything_written(result)
        assert "rm -r" in written
        assert str(with_bsd / "tool-freebsd.tar.gz") in written
        assert not (tmp_path / "dist").exists()

    def test_the_advice_is_what_actually_clears_the_directory(
        self, with_bsd, tmp_path
    ) -> None:
        """The end-to-end claim: run the printed command, and the build works.

        Parsed out of the output and executed rather than reimplemented, so a
        suggestion that names the wrong paths fails here.
        """
        rendered = plain_output(run("inspect", str(with_bsd)))
        line = next(line for line in rendered.splitlines() if "rm -r " in line)
        for target in shlex.split(line[line.index("rm -r ") :])[2:]:
            path = Path(target)
            shutil.rmtree(path) if path.is_dir() else path.unlink()

        result = run(
            "build",
            str(with_bsd),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
        )
        assert result.exit_code == 0, everything_written(result)
        assert len(list((tmp_path / "dist").glob("*.whl"))) == 1


class TestCommandsPointAtTheNextStep:
    """Each command ends by naming the one that usually follows it."""

    @pytest.fixture
    def binary(self, tmp_path):
        path = tmp_path / "tool"
        path.write_bytes(make_elf(0x3E, interp=None))
        return path

    def test_inspect_of_one_file_suggests_building_it(self, binary) -> None:
        rendered = plain_output(run("inspect", str(binary)))
        assert f"wheelbarrow build {binary}" in rendered

    def test_inspect_of_a_clean_directory_suggests_building_it(self, binary) -> None:
        """The directory, not the first binary: a release is built as a batch."""
        rendered = plain_output(run("inspect", str(binary.parent)))
        assert f"wheelbarrow build {binary.parent}" in rendered

    def test_build_suggests_a_publish_dry_run(self, binary, tmp_path) -> None:
        result = run(
            "build",
            str(binary),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
        )
        rendered = plain_output(result)
        assert "wheelbarrow publish" in rendered
        assert "--dry-run" in rendered

    def test_a_batch_build_suggests_publishing_all_of_them(self, tmp_path) -> None:
        """One `uv publish`, or the project sits half-uploaded between runs."""
        root = tmp_path / "downloads"
        root.mkdir()
        (root / "linux").mkdir()
        (root / "linux" / "tool").write_bytes(make_elf(0x3E, interp=None))
        (root / "windows").mkdir()
        (root / "windows" / "tool").write_bytes(make_pe(0x8664))
        result = run(
            "build",
            str(root),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
        )
        assert result.exit_code == 0
        assert "*.whl" in plain_output(result)

    def test_a_publish_dry_run_suggests_the_real_thing(self, binary, tmp_path) -> None:
        run(
            "build",
            str(binary),
            "-n",
            "demo-bin",
            "-V",
            "1.0.0",
            "-o",
            str(tmp_path / "dist"),
            "--no-check-name",
        )
        wheel = next((tmp_path / "dist").glob("*.whl"))
        rendered = plain_output(
            run(
                "publish",
                str(wheel),
                "--publish-url",
                "https://test.pypi.org/legacy/",
                "--dry-run",
            )
        )
        assert "upload them for real" in rendered
        # The flag that decides *where* it goes has to survive into the rerun.
        assert "--publish-url https://test.pypi.org/legacy/" in rendered
        assert "--dry-run" not in rendered.split("upload them for real")[1]

    def test_a_batch_rerun_is_a_glob_not_eleven_paths(self, tmp_path) -> None:
        """A line nobody reads before running defeats the point of echoing it."""
        wheels = [tmp_path / f"demo-{i}.whl" for i in range(11)]
        assert cli._as_glob(wheels) == [str(tmp_path / "*.whl")]

    def test_wheels_from_different_directories_stay_spelled_out(self, tmp_path) -> None:
        """A glob over one of them would not name the others at all."""
        wheels = [tmp_path / "a" / "x.whl", tmp_path / "b" / "y.whl"]
        assert cli._as_glob(wheels) == [str(w) for w in wheels]

    def test_a_glob_is_not_quoted_out_of_usefulness(self) -> None:
        """`dist/*.whl` must reach the shell as a glob, not a literal name."""
        assert cli._quote("dist/*.whl") == "dist/*.whl"

    def test_a_path_with_a_space_still_comes_out_runnable(self) -> None:
        assert cli._quote("my downloads/tool") == "'my downloads/tool'"


class TestFetchCommand:
    """`fetch`, with GitHub replaced by the `fake_http` routing table."""

    API = "https://api.github.com/repos/acme/tool/releases/tags/v1.0.0"
    ASSET = "https://example.test/tool-linux.tar.gz"

    def stub_release(self, routes, tarball, *, digest: bool) -> None:
        data, checksum = tarball
        routes[self.API] = json.dumps(
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {
                        "name": "tool-linux.tar.gz",
                        "browser_download_url": self.ASSET,
                        "size": len(data),
                        "digest": f"sha256:{checksum}" if digest else None,
                    },
                    {
                        "name": "tool-windows.zip",
                        "browser_download_url": "https://example.test/w.zip",
                        "size": 10,
                        "digest": f"sha256:{'a' * 64}" if digest else None,
                    },
                ],
            }
        ).encode()
        routes[self.ASSET] = data

    def test_a_release_is_downloaded_verified_and_unpacked(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=True)
        result = run(
            "fetch",
            "https://github.com/acme/tool/releases/tag/v1.0.0",
            str(tmp_path),
            "-p",
            "*.tar.gz",
        )
        assert result.exit_code == 0
        assert (tmp_path / "tool-linux.tar.gz").is_file()
        extracted = tmp_path / "tool-linux" / "tool"
        assert extracted.is_file()
        assert extracted.stat().st_mode & 0o111

    def test_the_summary_points_at_the_next_command(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=True)
        result = run(
            "fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "-p", "*.tar.gz"
        )
        written = plain_output(result)
        assert "wheelbarrow build" in written

    def test_list_shows_the_assets_without_downloading(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=True)
        result = run("fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "--list")
        assert result.exit_code == 0
        rendered = plain_output(result)
        assert "tool-linux.tar.gz" in rendered
        assert "tool-windows.zip" in rendered
        assert list(tmp_path.iterdir()) == []

    def test_no_extract_leaves_the_archive_packed(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=True)
        result = run(
            "fetch",
            "acme/tool",
            str(tmp_path),
            "-t",
            "v1.0.0",
            "-p",
            "*.tar.gz",
            "--no-extract",
        )
        assert result.exit_code == 0
        assert (tmp_path / "tool-linux.tar.gz").is_file()
        assert not (tmp_path / "tool-linux").exists()

    def test_an_unverifiable_release_fails_and_names_the_override(
        self, fake_http, tarball, tmp_path
    ) -> None:
        """Old releases carry no digest; that must stop rather than warn."""
        self.stub_release(fake_http, tarball, digest=False)
        result = run(
            "fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "-p", "*.tar.gz"
        )
        assert result.exit_code == 1
        assert "--allow-unverified" in everything_written(result)
        assert list(tmp_path.iterdir()) == []

    def test_the_override_downloads_it_anyway(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=False)
        result = run(
            "fetch",
            "acme/tool",
            str(tmp_path),
            "-t",
            "v1.0.0",
            "-p",
            "*.tar.gz",
            "--allow-unverified",
        )
        assert result.exit_code == 0
        assert (tmp_path / "tool-linux.tar.gz").is_file()

    def test_a_pattern_matching_nothing_is_an_error(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball, digest=True)
        result = run(
            "fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "-p", "*-freebsd*"
        )
        assert result.exit_code == 1
        assert "no asset" in everything_written(result)

    def test_a_tag_contradicting_the_url_is_refused(self, tmp_path) -> None:
        """Guessing which one was meant would silently fetch the wrong release."""
        result = run(
            "fetch",
            "https://github.com/acme/tool/releases/tag/v1.0.0",
            str(tmp_path),
            "-t",
            "v2.0.0",
        )
        assert result.exit_code == 1
        assert "v2.0.0" in everything_written(result)

    def test_there_is_no_token_option(self) -> None:
        """Like publishing, the credential is environment-only."""
        assert "--token" not in plain_output(run("fetch", "--help"))


class TestFetchHidesWhatItCannotUse:
    """`--list` shows the assets `build` could consume, and nothing else.

    Listing an installer and declining it later sends the user hunting for a
    rename that never happened.
    """

    API = "https://api.github.com/repos/acme/tool/releases/tags/v1.0.0"

    def stub_release(self, routes, tarball) -> None:
        data, checksum = tarball
        assets = [
            ("tool-linux.tar.gz", f"sha256:{checksum}"),
            ("tool-windows.msi", f"sha256:{'a' * 64}"),
            ("tool-linux.deb", f"sha256:{'b' * 64}"),
            ("tool-linux.tar.gz.sha256", f"sha256:{'c' * 64}"),
        ]
        routes[self.API] = json.dumps(
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": f"https://example.test/{name}",
                        "size": len(data),
                        "digest": digest,
                    }
                    for name, digest in assets
                ],
            }
        ).encode()
        routes["https://example.test/tool-linux.tar.gz"] = data

    def listed(self, fake_http, tarball, tmp_path) -> str:
        self.stub_release(fake_http, tarball)
        return plain_output(
            run("fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "--list")
        )

    def test_installers_are_not_listed(self, fake_http, tarball, tmp_path) -> None:
        rendered = self.listed(fake_http, tarball, tmp_path)
        assert "tool-linux.tar.gz" in rendered
        assert "tool-windows.msi" not in rendered
        assert "tool-linux.deb" not in rendered

    def test_the_hidden_ones_are_still_accounted_for(
        self, fake_http, tarball, tmp_path
    ) -> None:
        """Silently dropping three of four assets would look like a bad release."""
        rendered = self.listed(fake_http, tarball, tmp_path)
        assert "not shown" in rendered
        assert "cannot unpack" in rendered
        assert "checksum files" in rendered

    def test_list_suggests_the_download(self, fake_http, tarball, tmp_path) -> None:
        rendered = self.listed(fake_http, tarball, tmp_path)
        assert "wheelbarrow fetch acme/tool" in rendered

    def test_the_suggestion_carries_the_patterns_through(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball)
        rendered = plain_output(
            run(
                "fetch",
                "acme/tool",
                str(tmp_path),
                "-t",
                "v1.0.0",
                "-p",
                "*.tar.gz",
                "--list",
            )
        )
        assert "-p *.tar.gz" in rendered

    def test_a_bare_fetch_downloads_only_the_usable_assets(
        self, fake_http, tarball, tmp_path
    ) -> None:
        """No -p at all means every eligible asset, not every asset."""
        self.stub_release(fake_http, tarball)
        result = run("fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0")
        assert result.exit_code == 0
        assert (tmp_path / "tool-linux.tar.gz").is_file()
        assert not (tmp_path / "tool-windows.msi").exists()
        assert not (tmp_path / "tool-linux.deb").exists()

    def test_asking_for_an_installer_explains_the_refusal(
        self, fake_http, tarball, tmp_path
    ) -> None:
        self.stub_release(fake_http, tarball)
        result = run("fetch", "acme/tool", str(tmp_path), "-t", "v1.0.0", "-p", "*.msi")
        assert result.exit_code == 1
        assert "cannot unpack" in everything_written(result)


class TestPublishTakesNoToken:
    """The token is environment-only, so it cannot reach a shell history."""

    def test_there_is_no_token_option(self) -> None:
        assert "--token" not in run("publish", "--help").output

    def test_missing_token_is_reported_before_the_prompt(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("UV_PUBLISH_TOKEN", raising=False)
        wheel = tmp_path / "demo_bin-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"not really a wheel")

        # No input supplied: if the confirmation prompt were reached first, the
        # runner would fail on the empty stdin rather than report the token.
        result = run("publish", str(wheel))
        assert result.exit_code == 1
        assert "UV_PUBLISH_TOKEN" in everything_written(result)


class TestErrorsAreNotReplacedByHelp:
    """Only the zero-argument case is special; bad input still errors."""

    def test_missing_required_option_reports_the_error(self, elf_binary) -> None:
        result = run("build", str(elf_binary))
        assert result.exit_code != 0
        assert "--name" in everything_written(result)

    def test_unknown_option_reports_the_error(self, elf_binary) -> None:
        result = run("inspect", str(elf_binary), "--bogus")
        assert result.exit_code != 0
        assert "--bogus" in everything_written(result)
