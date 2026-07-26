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

```zsh
$ tree
pyproject.toml              # Hatchling backend, console scripts, metadata
README.md                   # The README for the tool
src/                        # The source code for the tool
└── <tool-name>/
    ├── __init__.py    # Exposes __version__ and binary_path()
    ├── __main__.py    # The launcher
    └── bin/<binary>   # The staged executable
tests/                 # Tests for the tool
```

### 3. Asset staging

The binary is copied into the package's `bin/` directory and its mode is set explicitly to `0o755`. Binaries extracted from release archives frequently arrive as `0o644`, so the executable bit is set rather than inherited.

### 4. Wheel compilation and tagging

`build.ProjectBuilder` drives the backend over PEP 517. Because the generated project looks pure-Python to the backend, the resulting wheel is `py3-none-any`; wheelbarrow then rewrites the archive to:

- Set the platform tag in both the file name and `WHEEL`,
- Set `Root-Is-Purelib: false`,
- Force mode `0o755` on the embedded binary, independently of what the backend chose to store,
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

A `UV_PUBLISH_TOKEN` must be passed to uv through the environment. It cannot be passed on the command line, to avoid leaking it in `ps` output.

To publish one package for several platforms, build a wheel per binary and upload them together. Installers will select which wheel to install based on the platform tag.

```zsh
wheelbarrow build ./<binary-macos-arm64>  -n <tool-name>-bin -V <version> -a <alias> -o dist
wheelbarrow build ./<binary-linux-x86_64> -n <tool-name>-bin -V <version> -a <alias> -o dist
wheelbarrow publish dist/<tool-name>_bin-<version>-py3-none-<platform-tag>.whl
```

## The launcher

The console script is a Python entry point that `execv`s the bundled binary. Because it is an `exec` rather than a subprocess, signals, exit codes, stdin and terminal control all pass through to the real tool untouched. On Windows,
which has no real `exec`, it waits on a child process and forwards the exit code.

The binary is also reachable from Python:

```python
from <tool-name>_bin import binary_path

subprocess.run([str(binary_path()), "--json", "pattern"])
```

The tradeoff is one Python interpreter startup per invocation, measured at about 24 ms on an Apple Silicon Mac (14 ms direct vs 39 ms wrapped). For an interactive tool use, this is unnoticeable; but can be significant for invocations in a tight shell loop.

Future versions may implement functionality to place the binary in the wheel's `.data/scripts/` directory, at the cost of the `binary_path()` API and the `python -m` entry point.

## Command reference

```zsh
wheelbarrow inspect BINARY [--glibc VERSION]

wheelbarrow build BINARY --name NAME --version VERSION
    -a, --alias NAME          console script to expose (repeatable)
    -o, --output DIR          output directory (default: dist)
    -d, --description TEXT
        --license EXPR
        --author NAME / --author-email EMAIL
        --homepage URL
        --keyword WORD        repeatable
        --requires-python SPEC
        --platform-tag TAG    skip detection, use TAG verbatim
        --glibc VERSION       manylinux baseline, e.g. 2.28
        --macos-min VERSION   e.g. 12.0
        --universal2          tag a fat Mach-O for both architectures
        --keep-project DIR    keep the generated project for inspection
        --isolated            build in an isolated PEP 517 environment
    -v, --verbose             show build backend output

wheelbarrow publish WHEELS...
        --index NAME | --publish-url URL
        --token TOKEN / --username NAME
        --dry-run             print the uv command without running it
    -y, --yes                 skip the confirmation prompt
```

## Notes and limitations

- Wheelbarrow only repackages binaries. It never compiles or modifies them.
- Dynamically linked Linux binaries are tagged `manylinux_2_17` by default. That is a claim about glibc compatibility that Wheelbarrow cannot verify. If the binary needs a newer glibc version, pass `--glibc`. Statically linked binaries, the most common case for Rust and Go tools, are unaffected.
- One wheel carries one binary for one platform. Tools that need companion files (man pages, completions, shared libraries) are out of scope.
- Check the upstream license before republishing someone else's binary.

## Development

```zsh
uv sync --dev --frozen
uv run pytest
uv run ruff check
uv run pyrefly
```

## Licence

MIT OR Apache-2.0
