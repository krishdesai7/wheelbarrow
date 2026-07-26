"""CLI surface: how help is reached, and when an error is shown instead."""

import re

import pytest
from typer.testing import CliRunner, Result

from wheelbarrow.cli import app
from wheelbarrow.pypi import NameStatus

runner = CliRunner()
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

#: Every command the app exposes, `help` included -- it documents itself.
COMMANDS = ["inspect", "build", "publish", "help"]


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

    @pytest.mark.parametrize("command", ["inspect", "build", "publish"])
    def test_no_arguments_prints_the_commands_help(self, command) -> None:
        result = run(command)
        rendered = plain_output(result)
        assert f"Usage: wheelbarrow {command}" in rendered
        assert "--help" in rendered

    @pytest.mark.parametrize("command", ["inspect", "build", "publish"])
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
