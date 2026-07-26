# Wheelbarrow

Convert OS binaries into PyPI-installable Python wheels.

A variety of tools ship only as prebuilt binaries and can therefore only be installed through a system package manager Wheelbarrow wraps such binaries in a correctly tagged wheel so it can be installed with Python's tooling:

```zsh
uv tool install <tool-name>
```

## Installing Wheelbarrow

Wheelbarrow is available as [`wheelbarrow`](https://pypi.org/project/wheelbarrow/) on PyPI.

Wheelbarrow can be invoked directly with [`uvx`](https://docs.astral.sh/uv/guides/tools/#running-tools):

```zsh
uvx wheelbarrow build <binary>
```

Or installed with `uv` (recommended), `pip` or `pipx`:

```zsh
# Install Wheelbarrow globally.
$ uv tool install wheelbarrow@latest

# Or add Wheelbarrow to your project.
$ uv add --dev wheelbarrow

# With pip.
$ pip install wheelbarrow

# With pipx.
$ pipx install wheelbarrow
```

## Quick start

Once installed you can run Wheelbarrow from the command line:

```zsh
$ wheelbarrow build ./<binary> --name <tool-name> --version <version> --alias <alias>
built dist/<tool-name>-<version>-py3-none-<platform-tag>.whl (<size>)
  tag       <platform-tag>
  launcher  direct
  scripts   <alias>
```

Installing that wheel puts a working `<alias>` on `PATH`:

```zsh
$ uv tool install dist/<tool-name>_bin-<version>-py3-none-<platform-tag>.whl
Installed 1 executable: <alias>
$ <alias> --version
<tool-name> <version>
```

## How it works

Wheelbarrow comprises four modules, each available individually if one wants to script them.

### 1. Input inspection

Wheelbarrow reads the binary's own headers, namely, ELF, Mach-O (including universal binaries) and PE/COFF, to recover the target OS, CPU architecture, libc flavour and/or macOS deployment target. Nothing is inferred from the machine one is running on, so cross-packaging works. For example, one can build a Linux wheel from a Mac.

```zsh
$ wheelbarrow inspect ./rg-linux
file       ./rg-linux
format     elf
os         linux
arch       x86_64
libc       static
wheel tag  py3-none-manylinux_2_17_x86_64
```

### 2. Dynamic project templating

A complete project is rendered from `string.Template` into a temporary directory:

The layout depends on the launcher (see [The launcher](#the-launcher)). With the default `direct` launcher the binary is staged outside the module, so it is not also swept in as package data:

```zsh
$ tree
pyproject.toml            # uv_build backend, data mapping, metadata
README.md                 # The README for the tool
scripts/                  # Staged outside src/ so it is not package data
└── <alias>               # The staged executable
src/                      # The source code for the tool
└── <tool-name>/
    ├── __init__.py       # Exposes __version__ and binary_path()
    └── __main__.py       # Supports `python -m <tool-name>`
```

With `--launcher shim` the binary lives inside the package at `<tool-name>/bin/<binary>` instead, and `pyproject.toml` gains a `[project.scripts]` entry point per alias.

### 3. Asset staging

The binary is copied into place and its mode is set explicitly to `0o755`. Binaries extracted from release archives frequently arrive as `0o644`, so the executable bit is set rather than inherited.

In `direct` mode the staged file _becomes_ the installed command, so it is named after the alias rather than the source file, and each alias needs its own copy. In `shim` mode a single copy is shared by every alias.

### 4. Wheel compilation and tagging

`build.ProjectBuilder` drives the backend over PEP 517. Because the generated project looks pure-Python to the backend, the resulting wheel is `py3-none-any`; wheelbarrow then rewrites the archive to:

- Set the platform tag in both the file name and `WHEEL`,
- Set `Root-Is-Purelib: false`,
- Force mode `0o755` on the embedded binary, independently of what the backend chose to store,
- Rewrite uv's non-standard `WHEEL.json` when present, so it cannot contradict `WHEEL`,
- Regenerate `RECORD` with fresh hashes.

Entries are written with a fixed timestamp, so identical inputs produce byte-identical wheels.

## Resolving the platform tag

Correctly resolving the platform tag ensures that a wheel is installed where it cannot run. E.g.,

```zsh
$ uv add faketool_bin-1.0.0-py3-none-manylinux_2_17_x86_64.whl
error: Failed to determine installation plan
  Caused by: A path dependency is incompatible with the current platform
hint: The wheel is compatible with Linux (`manylinux_2_17_x86_64`),
      but you're on macOS (Apple Silicon) (`macosx_26_0_arm64`)
```

Detected platforms map to tags as follows.

| Binary                         | Tag                                 | Description           |
| ------------------------------ | ----------------------------------- | --------------------- |
| ELF, glibc or static           | `manylinux_2_17_<arch>`             | Linux (glibc)         |
| ELF, musl                      | `musllinux_1_2_<arch>`              | Linux (musl)          |
| Mach-O arm64                   | `macosx_11_0_arm64`                 | macOS (Apple Silicon) |
| Mach-O x86_64                  | `macosx_10_12_x86_64`               | macOS (Intel)         |
| Mach-O universal (both slices) | `macosx_<min>_0_universal2`         | macOS (Universal)     |
| PE amd64 / arm64 / i386        | `win_amd64` / `win_arm64` / `win32` | Windows (PE/COFF)     |

The macOS minimum comes from the binary's `LC_BUILD_VERSION` when present. It can be overridden with `--glibc 2.28`, `--macos-min 12.0`, or replaced entirely with `--platform-tag manylinux_2_28_aarch64`.

## Publishing

```zsh
$ wheelbarrow publish dist/<tool-name>_bin-<version>-py3-none-<platform-tag>.whl --index <index-name>
About to publish 1 file(s) to <index-name>:
  dist/<tool-name>_bin-<version>-py3-none-<platform-tag>.whl
This cannot be undone: a released version cannot be re-uploaded.
Publish now? [y/N]:
```

This shells out to `uv publish`. Use `--dry-run` to see the exact command, and
`--yes` to skip the prompt in CI.

The API token is read from `UV_PUBLISH_TOKEN` in the environment. There is deliberately no `--token` option, so the token cannot leak into `ps` output or your shell history:

```zsh
export UV_PUBLISH_TOKEN=pypi-...
```

Wheelbarrow checks for it before asking for confirmation, so a missing token is reported straight away rather than after you have committed to the upload. Note that a `.env` file is not loaded automatically; either export the variable or run `uv run --env-file .env wheelbarrow publish ...`.

To publish one package for several platforms, build a wheel per binary and upload them together. Installers will select which wheel to install based on the platform tag.

```zsh
wheelbarrow build ./<binary-macos-arm64>  -n <tool-name>-bin -V <version> -a <alias> -o dist
wheelbarrow build ./<binary-linux-x86_64> -n <tool-name>-bin -V <version> -a <alias> -o dist
wheelbarrow publish dist/<tool-name>_bin-<version>-py3-none-<platform-tag>.whl
```

## The launcher

Two launchers are available via `--launcher`. Both keep `binary_path()` and `python -m <tool-name>` working.

### `direct` (default)

The binary is mapped into the wheel's `.data/scripts/` directory, so installers place the real executable straight onto `PATH`. No Python runs on invocation, and the installed command is indistinguishable from the binary itself.

There is deliberately no `[project.scripts]` entry in this mode: a console script of the same name is written to the same directory and would silently overwrite the binary at install time.

### `shim`

The binary lives inside the package, and a console script entry point `execv`s it. Because it is an `exec` rather than a subprocess, signals, exit codes, stdin and terminal control all pass through to the real tool untouched. On Windows, which has no real `exec`, it waits on a child process and forwards the exit code.

This costs one Python interpreter startup per invocation, but a single copy of the binary serves every alias.

### Which to use

Measured on an Apple Silicon Mac, invoking `rg --version` 60 times and taking the median:

| Launcher               | Time    |
| ---------------------- | ------- |
| `direct`               | 4.3 ms  |
| `shim`                 | 29.3 ms |
| Native (Homebrew `rg`) | 4.3 ms  |

`direct` is exactly as fast as the binary installed by a system package manager, which is the point of the tool. Prefer `shim` only when a package exposes several aliases for one binary and wheel size matters, since `direct` needs a copy per alias.

Either way the binary is reachable from Python:

```python
from <tool-name>_bin import binary_path

subprocess.run([str(binary_path()), "--json", "pattern"])
```

In `direct` mode `binary_path()` locates the installed file rather than computing it, checking paths beside the package before consulting `sysconfig`. That ordering matters under `pip install --target`, where `sysconfig` describes the running interpreter instead of the install target and could otherwise return an unrelated tool of the same name.

## Command reference

```zsh
wheelbarrow inspect BINARY [--glibc VERSION]

wheelbarrow build BINARY --name NAME --version VERSION
    -a, --alias NAME          console script to expose (repeatable)
    -o, --output DIR          output directory (default: dist)
    -d, --description TEXT
        --licence EXPR
        --author NAME / --author-email EMAIL
        --homepage URL
        --keyword WORD        repeatable
        --requires-python SPEC
        --platform-tag TAG    skip detection, use TAG verbatim
        --glibc VERSION       manylinux baseline, e.g. 2.28
        --macos-min VERSION   e.g. 12.0
        --universal2          tag a fat Mach-O for both architectures
        --launcher MODE       direct (default) or shim
        --keep-project DIR    keep the generated project for inspection
        --isolated            build in an isolated PEP 517 environment
    -v, --verbose             show build backend output

wheelbarrow publish WHEELS...      token comes from $UV_PUBLISH_TOKEN
        --index NAME | --publish-url URL
        --username NAME
        --dry-run             print the uv command without running it
    -y, --yes                 skip the confirmation prompt

wheelbarrow help [COMMAND]    same as `wheelbarrow COMMAND --help`
```

Running a command with no arguments at all prints its help, so `wheelbarrow build`,
`wheelbarrow help build` and `wheelbarrow build --help` are interchangeable.

## Notes and limitations

- Wheelbarrow only repackages binaries. It never compiles or modifies them.
- Dynamically linked Linux binaries are tagged `manylinux_2_17` by default. That is a claim about glibc compatibility that Wheelbarrow cannot verify. If the binary needs a newer glibc version, pass `--glibc`. Statically linked binaries, the most common case for Rust and Go tools, are unaffected.
- One wheel carries one binary for one platform. Tools that need companion files (man pages, completions, shared libraries) are out of scope.
- Generated projects use the `uv_build` backend, which is pinned to a narrow range (`>=0.11.30,<0.12`). A project kept with `--keep-project` and rebuilt much later may need that pin refreshed.
- In `direct` mode each alias is a separate copy of the binary in the wheel. Wheelbarrow warns when more than one alias is requested.
- Check the upstream licence before republishing someone else's binary.

## Development

### Mutable

The following commands are useful for development. They **will** modify the files in the repository.

```zsh
# Install dependencies
$ uv sync -U

# Type infer
$ uv run pyrefly infer

# Format
$ uv format

# Lint
$ uv run ruff check --fix [--unsafe-fixes]

# Run tests
$ uv run pytest -q
```

### Immutable

The following commands are useful for development. They **will not** modify the files in the repository.

```zsh
# Install dependencies
$ uv sync --frozen

# Type check/infer
$ uv run pyrefly check

# Format
$ uv format --diff

# Lint
$ uv run ruff check

# Dependency audit
$ uv audit

# Run tests
$ uv run pytest -q
```

## Licence

MIT OR Apache-2.0
