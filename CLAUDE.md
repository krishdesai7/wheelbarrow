# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```zsh
uv sync --dev --frozen     # set up the environment
uv run pytest              # full suite (~130 tests, a few seconds)
uv run pytest tests/test_build.py::TestLauncherLayouts::test_shim_keeps_the_binary_in_the_package
uv run pytest -k universal # select by name
uv run ruff check
uv run ruff format
uv run pyrefly check       # note: bare `uv run pyrefly` only prints help
```

Run the CLI during development with `uv run wheelbarrow ...`.

## Architecture

Wheelbarrow wraps a prebuilt native executable in a correctly tagged Python wheel. One pipeline runs
end to end in `builder.build_package`, and each stage is a separate module that can be used alone:

```
probe.inspect_binary  -> BinaryInfo      read ELF/Mach-O/PE headers off disk
tags.platform_tag     -> str             BinaryInfo -> PEP 425 platform tag
scaffold.make_spec    -> PackageSpec     validate + normalise user metadata
scaffold.scaffold_project                render templates.py into a temp dir, stage the binary
builder.build_wheel                      PEP 517 via build.ProjectBuilder -> py3-none-any wheel
wheelfix.retag_wheel  -> RetagResult     rewrite the archive with the real platform tag
```

Two invariants drive most of the design:

**Nothing is inferred from the host machine.** `probe.py` parses header bytes directly rather than
consulting `platform`/`sysconfig`, so a Linux wheel can be built on a Mac. Anything that would break
cross-building is a bug.

`pypi.check_name` is the one exception that proves the rule: it is the only network call in the
build path. It must stay advisory — every failure mode returns `NameStatus.UNKNOWN` rather than
raising, so builds keep working offline, and a `TAKEN` name is a warning, never an error (a 200
cannot distinguish your own project from someone else's). Tests stub `urlopen`; never let the
suite make a real request.

**The backend cannot know the wheel is platform-specific.** The generated project looks pure-Python
to `uv_build`, so the build always emits `py3-none-any` and `wheelfix.py` must rewrite the archive
afterwards: file name, `Tag:`/`Root-Is-Purelib:` in `WHEEL`, uv's non-standard `WHEEL.json` (which
would otherwise contradict `WHEEL`), mode `0o755` on the embedded binary, and a regenerated `RECORD`.
Every entry is written at `ZIP_EPOCH`, so identical inputs produce byte-identical wheels — tests
assert this, so do not introduce real timestamps or non-deterministic ordering.

### The launcher choice reshapes everything downstream

`Launcher.DIRECT` (default) vs `Launcher.SHIM` is not a flag checked in one place; it changes the
project layout, which template is rendered, and which archive members get the executable bit. When
touching one, check all of:

- `scaffold.staged_paths` — where the binary is copied before the build (`scripts/<alias>` per alias
  for direct, a single `src/<module>/bin/<binary>` for shim).
- `scaffold.archive_executables` — the matching member paths *inside* the wheel
  (`<dist>-<version>.data/scripts/<alias>` vs `<module>/bin/<binary>`); `build_package` fails if this
  set comes back empty.
- `scaffold.render_pyproject` — direct mode emits `[tool.uv.build-backend.data] scripts = ...` and
  deliberately **no** `[project.scripts]`, because a console script of the same name would overwrite
  the binary at install time. Shim mode is the reverse.
- `templates.INIT_DIRECT` vs `INIT_SHIM` — shim computes `binary_path()` relative to `__file__`;
  direct must *locate* the installed file, checking paths beside the package before `sysconfig`
  (which describes the running interpreter, and is wrong under `pip install --target`).

`PackageSpec.installed_name` encodes the consequence: in direct mode the staged file *becomes* the
command, so it is named after the first alias, not after the source file.

### Conventions

- All user-facing failures raise a `WheelbarrowError` subclass from `errors.py`. `cli.py` catches
  only that base class and turns it into `error: ...` on stderr with exit code 1; anything else
  escaping as a traceback is a bug.
- Generated file bodies live entirely in `templates.py` as `string.Template` objects — keep them
  readable as the Python/TOML they become, and render TOML values through `toml_str`/`toml_array`
  rather than interpolating raw strings.
- `uv_build` is a direct runtime dependency of wheelbarrow so the default (non-`--isolated`) build
  path can run the backend in-process instead of provisioning a venv. It is pinned to `>=0.11.30,<0.12`
  in both `pyproject.toml` and the generated `PYPROJECT` template; those pins must move together.
- Publishing shells out to `uv publish`. The token is read from `UV_PUBLISH_TOKEN` by
  `publish.resolve_token()` and never travels in `argv`: there is deliberately no `--token`
  option, and `run_publish` lets uv inherit the environment rather than forwarding the value.
  Keep credentials out of `PublishPlan.argv` and `display()`.
- User-facing spelling is `licence`, but `scaffold.render_pyproject` must emit `license` —
  PEP 621 fixes that key, and a backend silently ignores an unrecognised one, dropping the
  field from the wheel's METADATA with no error.

### Tests

`tests/conftest.py` assembles synthetic ELF, Mach-O, fat Mach-O and PE images byte by byte, so the
suite is hermetic and every platform path is exercised from any host. Add new format coverage by
extending those builders rather than committing binary fixtures.

`TestInstalledWheelRuns` in `tests/test_build.py` really installs a built wheel (pip, falling back to
uv) into a `--target` directory and executes it, using a `/bin/sh` script plus an explicit
`--platform-tag any` to stay portable. It skips on Windows and when no installer is available.
