# Wheelbarrow

Convert OS binaries into PyPI-installable Python wheels.

A variety of tools ship only as prebuilt binaries, or as standalone shell scripts, and can therefore only be installed through a system package manager. Wheelbarrow wraps such executables in a correctly tagged wheel so they can be installed with Python's tooling:

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

Most tools you want to package are published as a GitHub release, so `fetch` gets them
and checks them in one step:

```zsh
$ wheelbarrow fetch https://github.com/starship/starship/releases/tag/v1.26.0 ~/starship
starship/starship v1.26.0 -- 12 of 30 assets selected
  ok starship-aarch64-apple-darwin.tar.gz (3.9 MiB, the GitHub API)
  ok starship-aarch64-unknown-linux-musl.tar.gz (4.6 MiB, the GitHub API)
  ...
fetched 12 asset(s) into ~/starship, 12 executable(s) extracted
check what they are:
  wheelbarrow inspect ~/starship
```

Every asset is verified before it is unpacked, and each archive lands in a directory
named after it. Installers and checksum files are skipped, so no `--pattern` is needed for
the common case. See [Fetching release assets](#fetching-release-assets) for the details.

`build` then takes that whole directory and turns it into one wheel per platform:

```zsh
$ wheelbarrow build ~/starship -n py-starship -V 1.26.0
building 11 wheels from 11 executables in ~/starship
  ok py_starship-1.26.0-py3-none-macosx_11_0_arm64.whl (3.9 MiB)
  ok py_starship-1.26.0-py3-none-manylinux_2_17_aarch64.musllinux_1_2_aarch64.whl (4.6 MiB)
  ...
built 11 wheels into dist (launcher direct, scripts starship)
check what would be uploaded:
  wheelbarrow publish dist/*.whl --dry-run
```

Each command ends by naming the one that usually follows it, so the pipeline is
discoverable without reading the rest of this file. If `inspect` or `build` meets a binary
it has no wheel tag for — a FreeBSD build, say — it prints the exact `rm -r` that clears
the directory, archive included, and the command to re-check afterwards.

Two commands for a whole release. See [Directories of binaries](#directories-of-binaries)
for what `build` refuses and why.

A single binary works exactly as before, with a more detailed report:

```zsh
$ wheelbarrow build ./<binary> --name <tool-name> --version <version> --alias <alias>
built dist/<tool-name>-<version>-py3-none-<platform-tag>.whl (<size>)
  tag       <platform-tag>
  launcher  direct
  scripts   <alias>
```

Installing that wheel puts a working `<alias>` on `PATH`:

```zsh
$ uv tool install dist/<tool-name>-<version>-py3-none-<platform-tag>.whl
Installed 1 executable: <alias>
$ <alias> --version
<tool-name> <version>
```

## Fetching release assets

`wheelbarrow fetch` replaces the download-verify-extract routine you would otherwise do
by hand. It takes a release URL — or `owner/repo` with `--tag`, or neither for the latest
release — and a directory to work in.

```zsh
wheelbarrow fetch <release-url> <dir> -p '<glob>'
```

It talks to the GitHub API directly rather than shelling out to `gh`, so `uvx wheelbarrow`
works on a machine with nothing else installed. Public releases need no credentials. A
token is read from `GH_TOKEN` or `GITHUB_TOKEN` when one is set, for private repositories
and for the larger rate limit; as with the publish token there is deliberately no
`--token` option, so it cannot land in a shell history.

### Selecting assets

`--pattern` is optional. Left out, you get every asset wheelbarrow could build a wheel
from, which for a typical release is the whole thing:

```zsh
wheelbarrow fetch <release-url> <dir>
```

Give one or more `--pattern` globs to narrow that. Anything matching nothing is an error
rather than a quiet omission — a pattern that stops matching after an upstream rename would
otherwise surface much later as a missing wheel. `--list` shows what would be downloaded
and exits.

Two kinds of asset are never treated as payload, even when a pattern matches them:

- **Checksum files**, which is what lets `'*.tar.gz*'` — the natural way to ask for the
  tarballs — do the obvious thing rather than trying to unpack a `.sha256` alongside them.
- **Anything that cannot become a wheel**: installers (`.msi`, `.deb`, `.rpm`, `.dmg`, …),
  signatures and attestations, documentation. wheelbarrow cannot open an installer to get
  at the executable inside, so listing one and declining it later would just send you
  looking for a rename that never happened.

`--list` reports how many it left out and why, so a release of nothing but `.msi` files
reads as "wheelbarrow cannot use these" rather than as an empty release. An asset with no
extension at all is always kept — a release shipping the bare executable is exactly the
case `build` wants.

### Verification

Nothing is unpacked before it is verified, and a file that fails is deleted rather than
left on disk where a later `build` might sweep it up. The digest comes from one of two
places, in order:

1. **The GitHub API.** Every release asset carries a `digest` field that GitHub computes
   itself on upload, so unlike a checksum file it is not simply another artefact the
   publisher supplied.
2. **A checksum file in the same release.** Assets uploaded before GitHub began recording
   digests in 2025 have none, and then a per-asset sidecar (`<asset>.sha256`) is tried
   first, followed by a whole-release manifest (`SHA256SUMS`, `checksums.txt`). Both
   conventions found in the wild are parsed: a bare digest, and `<digest>  <name>` rows.

Neither is a signature. Both attest that these are the bytes that were uploaded, not who
uploaded them.

A release offering no digest and no checksum file fails, naming the assets it could not
verify. `--allow-unverified` downloads them anyway, and the summary marks them `??`
instead of `ok`. Verification is resolved for every asset up front, so an unverifiable one
stops the run before anything is written.

An asset whose digest already matches a file in the destination is not downloaded again,
so re-running over a populated directory costs nothing.

### Extraction

Each archive is unpacked into a directory named after it, so `starship-x86_64-apple-darwin.tar.gz`
becomes `starship-x86_64-apple-darwin/`. Tarballs are extracted under Python's `data`
filter, which refuses `..` traversal, links pointing outside the destination and device
nodes, and drops setuid bits — while keeping the executable bit, the one permission that
has to survive. Zip archives get that bit restored explicitly, since `zipfile` discards
the stored Unix mode and would otherwise leave a binary unrunnable.

An asset that is not an archive — a bare executable, say — is downloaded and verified but
not unpacked, which is not an error. `--no-extract` skips unpacking entirely.

The count of extracted executables is decided the same way `build` decides it: by parsing
the headers, never by the executable bit. A zip written on Windows stores DOS attributes
rather than a Unix mode, so a `.exe` comes out of one with no `+x` — counting the bit would
report fewer binaries than the very next command goes on to package.

Fetching is not on the build path: `build` itself never reaches the network.

## Directories of binaries

`inspect` and `build` both accept a directory as readily as a file. What they find in it is
decided by reading headers, not by asking the file system what looks runnable — a binary
extracted from a `.zip` may have lost its mode bits, and the `.tar.gz` it came out of is
mode 0644 either way. So a directory that still holds the archives beside the executables,
which is exactly what `fetch` leaves, needs no tidying first.

A failure to read a file means different things in the two cases. A file you named must be
readable: refusing to parse it answers the question you asked. The same failure inside a
directory just means "not a binary", which is the common case rather than an error —
`inspect` says how many files it passed over so that silence is never mistaken for
success.

```zsh
$ wheelbarrow inspect ~/starship
path                                       platform tag
starship-aarch64-apple-darwin/starship     macosx_11_0_arm64
starship-aarch64-unknown-linux-musl/...    manylinux_2_17_aarch64.musllinux_1_2_aarch64
...
11 executable(s), 11 other file(s) ignored
build the whole directory:
  wheelbarrow build ~/starship -n <name> -V <version>
```

Every wheel in a batch shares one `--name` and `--version`, so the platform tag is the only
thing keeping them apart. That makes three situations worth refusing outright, all of them
checked before a single wheel is built — a batch that fails half way through leaves
finished wheels behind that nothing will clean up:

- **Two binaries resolving to one tag.** They would overwrite each other in the output
  directory, and the survivor gives no hint that anything was lost. A dynamically linked
  glibc build sitting next to a static build of the same architecture is the usual cause.
  Identical inputs are exempt: builds are reproducible, so a duplicate is a rebuild.
- **Binaries with different file names.** The console script defaults to the file name, so
  a directory of `tool-linux` and `tool-darwin` would produce one package whose command
  changes depending on which platform it was installed on. Pass `--alias` to settle it.
- **`--platform-tag` over more than one binary.** One tag cannot describe several, and
  applying it to all of them collapses them onto one wheel name.

An executable that has no wheel tag at all — a FreeBSD ELF, say — is listed by `inspect`
with its reason, because reporting is what that command is for. `build` refuses it instead,
naming every such file at once so that one pass through the directory is enough. Both then
print the removal that fixes it:

```
no tag: starship-x86_64-unknown-freebsd/starship: no wheel platform tag exists for
freebsd; Python packaging defines tags for Linux, macOS and Windows only.
remove them with: rm -r ~/starship/starship-x86_64-unknown-freebsd ~/starship/starship-x86_64-unknown-freebsd.tar.gz
then confirm the directory is clean:
  wheelbarrow inspect ~/starship
```

The archive is named alongside the unpacked directory on purpose: leaving it behind means
the next `fetch` puts the binary straight back.

## How it works

Building a wheel is one pipeline, and every stage is a module you can import and use on its own. `fetch` and `discover` sit in front of it; the four numbered stages below are the build itself.

```
discover.collect      an executable, or every one under a directory
probe.inspect_binary  read ELF/Mach-O/PE headers off disk
tags.platform_tag     BinaryInfo -> PEP 425 platform tag
scaffold              validate metadata, render the project, stage the binary
builder.build_wheel   PEP 517 via build.ProjectBuilder -> py3-none-any wheel
wheelfix.retag_wheel  rewrite the archive with the real platform tag
```

### 1. Input inspection

Wheelbarrow reads the binary's own headers, namely, ELF, Mach-O (including universal binaries) and PE/COFF, to recover the target OS, CPU architecture, libc flavour and/or macOS deployment target. Nothing is inferred from the machine one is running on, so cross-packaging works. For example, one can build a Linux wheel from a Mac. A `#!` script has no headers to read and no platform to detect (see [Shell scripts](#shell-scripts)).

```zsh
$ wheelbarrow inspect ./rg-linux
file       ./rg-linux
format     elf
os         linux
arch       x86_64
libc       static
wheel tag  py3-none-manylinux_2_17_x86_64.musllinux_1_2_x86_64
```

That tag is a compressed set rather than a typo — see [Static binaries claim both libc families](#static-binaries-claim-both-libc-families).

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

The generated `README.md` becomes the wheel's long description — the PyPI project page — so it is written for whoever installs the package rather than for whoever built it. It records what the wheel actually contains:

```markdown
## Provenance

- **file** — `starship`
- **kind** — ELF executable, linux/x86_64, statically linked
- **sha256** — `3696a6cf…`
- **wheel tag** — `manylinux_2_17_x86_64.musllinux_1_2_x86_64`

Statically linked, so it needs no C library at all. The tag is a compressed set
naming both families deliberately: manylinux alone would withhold it from
Alpine, and musllinux alone from glibc systems on architectures with no glibc
build.
```

The digest is of the file exactly as packaged, so anyone can check it against whatever the tool's own publisher lists. The closing paragraph explains what the wheel tag means in words, which matters most in the two cases where the tag alone misleads: a static binary, whose compressed tag set looks like a typo until you know why both families are named, and a shell script, whose `any` tag promises far more than the script can deliver. That paragraph is only written when the tag and the binary agree — under `--platform-tag` they can contradict each other, and then the README describes the file and stays quiet about the tag.

When a directory is built as a batch, every wheel's README describes the **whole set** instead:

```markdown
## Provenance

- **`macosx_11_0_arm64`**
  Mach-O executable, macos/arm64, macOS 11.0+
  sha256 `01532ebb…`
- **`manylinux_2_17_x86_64.musllinux_1_2_x86_64`**
  ELF executable, linux/x86_64, statically linked
  sha256 `3696a6cf…`
…
```

PyPI shows one description for a project and takes it from one of the uploaded files, so a README naming only its own binary would be wrong on that page for everyone who installed a different platform's wheel. Listing the set makes every wheel's README byte-identical, which is what makes PyPI's choice of file not matter. The per-tag explanation is dropped in this mode — each one explains a single tag, and eleven tags do not share an explanation.

### 3. Asset staging

The binary is copied into place and its mode is set explicitly to `0o755`. Binaries extracted from release archives frequently arrive as `0o644`, so the executable bit is set rather than inherited.

In `direct` mode the staged file _becomes_ the installed command, so it is named after the alias rather than the source file, and each alias needs its own copy. In `shim` mode a single copy is shared by every alias.

### 4. Wheel compilation and tagging

`build.ProjectBuilder` drives the backend over PEP 517. Because the generated project looks pure-Python to the backend, the resulting wheel is `py3-none-any`; wheelbarrow then rewrites the archive to:

- Set the platform tag in both the file name and `WHEEL`,
- Set `Root-Is-Purelib` to match it: `false` for a real platform tag, `true` for `any`,
- Force mode `0o755` on the embedded binary, independently of what the backend chose to store,
- Rewrite uv's non-standard `WHEEL.json` when present, so it cannot contradict `WHEEL`,
- Regenerate `RECORD` with fresh hashes.

Entries are written with a fixed timestamp, so identical inputs produce byte-identical wheels.

## Resolving the platform tag

Correctly resolving the platform tag is what stops a wheel being installed where it cannot run:

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
| ELF, dynamic glibc             | `manylinux_<measured>_<arch>`       | Linux (glibc)         |
| ELF, dynamic musl              | `musllinux_1_2_<arch>`              | Linux (musl)          |
| ELF, static                    | `manylinux_…_<arch>.musllinux_…`    | Linux (either libc)   |
| Mach-O arm64                   | `macosx_11_0_arm64`                 | macOS (Apple Silicon) |
| Mach-O x86_64                  | `macosx_10_12_x86_64`               | macOS (Intel)         |
| Mach-O universal (both slices) | `macosx_<min>_0_universal2`         | macOS (Universal)     |
| PE amd64 / arm64 / i386        | `win_amd64` / `win_arm64` / `win32` | Windows (PE/COFF)     |
| `#!` script                    | `any`                               | Any (no machine code) |
| ELF, non-Linux `EI_OSABI`      | _refused_                           | FreeBSD, NetBSD, …    |

ELF is not a Linux format. FreeBSD, NetBSD, OpenBSD and Solaris binaries are ELF too, and are identical to Linux ones in machine and libc; only `EI_OSABI` tells them apart. Python packaging defines no tag for those systems, so wheelbarrow names the system and refuses rather than passing a FreeBSD binary off as `manylinux`:

```zsh
$ wheelbarrow inspect ./starship-x86_64-unknown-freebsd
error: no wheel platform tag exists for freebsd; Python packaging defines tags
for Linux, macOS and Windows only. Pass --platform-tag explicitly to package it
anyway.
```

The macOS minimum comes from the binary's `LC_BUILD_VERSION` when present. It can be overridden with `--glibc 2.28`, `--macos-min 12.0`, or replaced entirely with `--platform-tag manylinux_2_28_aarch64`.

### Static binaries claim both libc families

A platform tag says where an installer may *place* a wheel, not merely where the code can run. pip on Alpine accepts only `musllinux_*` tags, so tagging a statically linked binary `manylinux` alone withholds it from exactly the systems a static build exists to serve — while tagging it `musllinux` alone abandons every glibc user on architectures that ship no glibc build.

Neither is true, so wheelbarrow emits both, as a PEP 425 compressed tag set:

```
py_starship-1.26.0-py3-none-manylinux_2_17_x86_64.musllinux_1_2_x86_64.whl
```

One wheel, honestly installable under either libc. The file name compresses the set; `WHEEL` gets the expanded form, one `Tag:` per line, as the spec requires.

### The glibc floor is measured, not assumed

For a *dynamically* linked glibc binary the manylinux baseline is read out of `.gnu.version_r` — the highest `GLIBC_x.y` symbol version the binary imports:

```zsh
$ wheelbarrow inspect ./starship-x86_64-unknown-linux-gnu
...
libc         glibc
glibc min    2.18
wheel tag    py3-none-manylinux_2_18_x86_64
```

This matters more than one version's difference suggests: RHEL and CentOS 7 ship glibc *exactly* 2.17, so a default `manylinux_2_17` tag on a binary needing 2.18 installs there and then dies with `version GLIBC_2.18 not found`.

A measurement can only raise the floor, never lower it. A binary importing nothing newer than 2.5 is not evidence that it runs on a 2.5 system, so the per-architecture default stays a lower bound. `--glibc` overrides both.

## Shell scripts

Not every tool is machine code. A file beginning with `#!` is recognised as a script and needs no special handling:

```zsh
$ wheelbarrow inspect ./greet.sh
file         ./greet.sh
format       script
os           any
arch         any
interpreter  /usr/bin/env bash
wheel tag    py3-none-any
```

Nothing in a script constrains where it can be installed, so it is tagged `any` and the wheel is marked `Root-Is-Purelib: true`. The file extension is dropped when deriving the default alias, so `greet.sh` installs as `greet`. Everything else — the executable bit, both launchers, `binary_path()`, `python -m` — behaves exactly as it does for a binary.

The one caveat is that `any` is broader than the truth. A wheel tagged `any` installs on Windows too, where `/bin/sh` does not exist, and no wheel tag can express "needs a POSIX shell". Wheelbarrow says so when it builds one:

```zsh
$ wheelbarrow build ./greet.sh --name greet-bin --version 0.1.0
note: greet.sh is a script run by /usr/bin/env bash, so the wheel is tagged any
and will install anywhere, including where that interpreter does not exist. Pass
--platform-tag to narrow it.
built dist/greet_bin-0.1.0-py3-none-any.whl (3.7 KiB)
```

If the script is POSIX-only and that matters, restrict it explicitly with `--platform-tag manylinux_2_17_x86_64` or similar. A script without a `#!` line cannot be detected as one; pass `--platform-tag any` for those.

## Checking the project name

Before building, wheelbarrow asks PyPI whether `--name` is already registered, with a `HEAD` of `https://pypi.org/simple/<name>/`: 200 means the name exists, 404 means it is free to claim. A registered name stays 200 even after every release has been deleted or yanked, so it cannot be reclaimed.

```zsh
$ wheelbarrow build ./rg --name ripgrep-bin --version 14.1.0
note: ripgrep-bin is already registered on PyPI. Publishing will only work if
      the project is yours; otherwise choose a different --name.
built dist/ripgrep_bin-14.1.0-py3-none-macosx_11_0_arm64.whl (4.8 MiB)
```

This is advice, not a gate. It never fails the build, because a 200 cannot tell your own project apart from someone else's, and rebuilding a package you already own is the usual case. A free name is not remarked on.

The lookup adds roughly 100 ms and is the only time wheelbarrow touches the network while building. If the index cannot be reached the build carries on regardless — pass `--verbose` to see that it was skipped, or `--no-check-name` to not ask at all. Building a directory asks once for the whole batch, since every wheel in it carries the same project name.

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

To publish one package for several platforms, build a wheel per binary and upload them together; installers pick the right one by its platform tag. Point `build` at the directory and it does the whole set at once:

```zsh
wheelbarrow fetch <release-url> ~/<tool-name>
wheelbarrow inspect ~/<tool-name>
wheelbarrow build ~/<tool-name> -n <tool-name>-bin -V <version> -o dist
wheelbarrow publish dist/*.whl --dry-run
wheelbarrow publish dist/*.whl
```

Every wheel in that set shares a name and version, so `dist/*.whl` uploads them in one call. `build` refuses up front if two of them would collide on a tag, which is what makes the glob safe to use. The `inspect` step is optional but cheap: it is where an untaggable binary shows up, along with the `rm -r` that clears it, rather than at the point `build` refuses the batch.

## The launcher

Two launchers are available via `--launcher`. Both keep `binary_path()` and `python -m <tool-name>` working.

### `direct` (default)

The binary is mapped into the wheel's `.data/scripts/` directory, so installers place the real executable straight onto `PATH`. No Python runs on invocation, and the installed command is indistinguishable from the binary itself.

There is deliberately no `[project.scripts]` entry in this mode: a console script of the same name is written to the same directory and would silently overwrite the binary at install time.

Because the staged file is renamed after its alias, a Windows executable suffix is carried across: `starship.exe` with the default alias installs as `Scripts\starship.exe`, not `Scripts\starship`, which Windows would refuse to run. The alias itself is unaffected — the command is still `starship`. Only `PATHEXT` suffixes (`.exe`, `.com`, `.bat`, `.cmd`) are kept; `.sh` and the like are dropped, since on POSIX an extension on a command name is just noise.

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
wheelbarrow fetch SOURCE [DIR]         token comes from $GH_TOKEN or $GITHUB_TOKEN
    -t, --tag TAG             release tag, if SOURCE has none
    -p, --pattern GLOB        asset names to download (repeatable)
        --list                show the release's assets and exit
        --no-extract          download without unpacking
        --allow-unverified    download assets that publish no checksum
        --timeout SECONDS     per-request timeout (default: 30)

wheelbarrow inspect PATH [--glibc VERSION]      an executable, or a directory of them

wheelbarrow build PATH --name NAME --version VERSION
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
        --overwrite           replace an existing wheel of the same name
        --isolated            build in an isolated PEP 517 environment
        --no-check-name       skip the PyPI name lookup (see below)
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
- For a dynamically linked glibc binary the manylinux floor is measured from `.gnu.version_r`, and `manylinux_2_17` is only the lower bound it can never fall below. The measurement records the highest glibc symbol the binary imports, which is a good proxy for what it needs but not a guarantee; `--glibc` overrides it. Statically linked binaries, the most common case for Rust and Go tools, are unaffected.
- One wheel carries one binary for one platform. Tools that need companion files (man pages, completions, shared libraries) are out of scope. Building a directory produces one such wheel per binary; it does not combine them.
- Symlinks are skipped when walking a directory, so a link beside its target is not packaged a second time under the link's name.
- Generated projects use the `uv_build` backend, which is pinned to a narrow range (`>=0.11.30,<0.12`). A project kept with `--keep-project` and rebuilt much later may need that pin refreshed.
- In `direct` mode each alias is a separate copy of the binary in the wheel. Wheelbarrow warns when more than one alias is requested.
- Check the upstream licence before republishing someone else's binary.

## Development

### Mutable

The following commands are useful for development. They **will** modify the files in the repository. Can be run collectively with `uv run just m[utable]`.

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

The following commands are useful for development. They **will not** modify the files in the repository. Can be run collectively with `uv run just i[mmutable]`.

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
