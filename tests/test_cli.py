"""CLI surface: how help is reached, and when an error is shown instead."""

import pytest
from typer.testing import CliRunner, Result

from wheelbarrow.cli import app

runner = CliRunner()

#: Every command the app exposes, `help` included -- it documents itself.
COMMANDS = ["inspect", "build", "publish", "help"]


def run(*args: str) -> Result:
    """Invoke the app under its installed name, so usage lines read correctly."""
    return runner.invoke(app, list(args), prog_name="wheelbarrow")


def everything_written(result: Result) -> str:
    """Both streams, since rich sends errors to stderr and help to stdout."""
    return result.output + (result.stderr or "")


class TestBareSubcommandShowsHelp:
    """`wheelbarrow COMMAND` with no arguments behaves like the bare app."""

    @pytest.mark.parametrize("command", ["inspect", "build", "publish"])
    def test_no_arguments_prints_the_commands_help(self, command) -> None:
        result = run(command)
        assert f"Usage: wheelbarrow {command}" in result.output
        assert "--help" in result.output

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
        assert f"Usage: wheelbarrow {command}" in run("help", command).output

    def test_without_an_argument_describes_the_app(self) -> None:
        assert run("help").output == run("--help").output

    def test_unknown_command_is_an_error_listing_the_real_ones(self) -> None:
        result = run("help", "frobnicate")
        assert result.exit_code == 1
        written = everything_written(result)
        assert "frobnicate" in written
        for command in COMMANDS:
            assert command in written


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
