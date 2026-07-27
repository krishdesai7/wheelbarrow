# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```zsh
uv sync --dev --frozen     # set up the environment
uv run pytest              # full suite (~410 tests, a few seconds)
uv run pytest tests/test_build.py::TestLauncherLayouts::test_shim_keeps_the_binary_in_the_package
uv run pytest -k universal # select by name
uv run ruff check
uv run ruff format
uv run pyrefly check       # note: bare `uv run pyrefly` only prints help
```

Run the CLI during development with `uv run wheelbarrow ...`.

## Architecture

Wheelbarrow wraps a prebuilt native executable in a correctly tagged Python wheel. One pipeline runs
end to end in `builder.build_package` -- `build_packages` is that same pipeline in a loop, for a
directory -- and each stage is a separate module that can be used alone:

```
discover.collect      -> Discovery       a file, or every executable under a directory
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

`fetch.py` is network-first by definition, but it sits *before* the build path, not inside it —
`build` still never reaches out. Its verification policy is the opposite of `check_name`'s: an
asset that cannot be verified is fatal, because unverified bytes that become a published wheel
is exactly the case worth interrupting for. `--allow-unverified` is the escape hatch, and it has
to stay explicit.

A platform tag states where an installer may *place* a wheel, not where the code can run — the two
come apart for a statically linked ELF, which needs no libc at all. `_linux_tag` therefore emits a
PEP 425 compressed tag set for `libc == "static"` (`manylinux_..._<arch>.musllinux_1_2_<arch>`):
manylinux alone would withhold it from Alpine, musllinux alone from glibc users on architectures
with no glibc build. `wheelfix.expand_tags` exists because the file name compresses the set but
`WHEEL` and `WHEEL.json` take it expanded, one entry per tag.

The manylinux floor for a *dynamic* glibc binary is measured, not defaulted: `probe._elf_glibc_min`
reads `.gnu.version_r` for the highest `GLIBC_x.y` imported. `tags._glibc_baseline` takes the max of
that and `DEFAULT_GLIBC` — a measurement may only raise the floor, since importing nothing newer
than 2.5 is not evidence a binary runs on a 2.5 system. Getting this wrong by one version is not
cosmetic: RHEL 7 is exactly 2.17.

`builder._place_wheel` is what stops two inputs that resolve to one tag from silently overwriting
each other in `-o`. It relies on the reproducibility invariant: identical bytes are a rebuild and
pass, differing bytes are an error unless `--overwrite`. It is also why `build_package` builds into
a temp dir and only then places the finished wheel — nothing half-built or wrongly tagged should
ever appear somewhere the user might later `publish dist/*.whl`.

ELF does not imply Linux. `_parse_elf` reads `EI_OSABI` (byte 7) and maps it through `ELF_OSABI`;
only 0 (SysV) and 3 (GNU) are Linux, and 0 is what Linux toolchains actually emit, so that byte can
rule Linux out but never confirm it. `tags.platform_tag` then refuses anything else by name, because
Python packaging has no tag for the BSDs and a silent `manylinux` tag on a FreeBSD binary is the
worst possible outcome. An unrecognised OSABI is refused rather than assumed.

Not every input is machine code. `probe.py` reports a `#!` file as `format="script"`, `os="any"`,
`arch="any"` plus the shebang line, and `tags.platform_tag` maps that to `any`. That is deliberately
broader than the truth — a `py3-none-any` wheel installs on Windows too — because no wheel tag can
express "needs /bin/sh"; `cli._note_tagging_caveats` says so instead of narrowing it silently.
Everything downstream (staging, both launchers, the 0o755 bit, alias derivation) already worked for
scripts, so nothing there is special-cased.

**The backend cannot know the wheel is platform-specific.** The generated project looks pure-Python
to `uv_build`, so the build always emits `py3-none-any` and `wheelfix.py` must rewrite the archive
afterwards: file name, `Tag:`/`Root-Is-Purelib:` in `WHEEL`, uv's non-standard `WHEEL.json` (which
would otherwise contradict `WHEEL`), mode `0o755` on the embedded binary, and a regenerated `RECORD`.
`Root-Is-Purelib` is derived from the tag by `wheelfix._is_pure`, not hardcoded: `false` for a real
platform tag, `true` for `any`, and `WHEEL.json` must keep agreeing with `WHEEL` about it.
Every entry is written at `ZIP_EPOCH`, so identical inputs produce byte-identical wheels — tests
assert this, so do not introduce real timestamps or non-deterministic ordering.

### A directory is a batch, and a batch is refused before it starts

`discover.collect` is the front door for both `inspect` and `build`, and it keeps the rule
that nothing is inferred from the host: membership is decided by whether `inspect_binary`
parses the file, never by the executable bit, which a `.zip` extraction loses and a
`.tar.gz` never had. The asymmetry to preserve is that an `InspectionError` on a *named*
file propagates while the same error inside a directory only means "not a binary" — that
is what lets a directory of archives-beside-executables work untouched.

`Discovery.is_single` is what keeps single-file behaviour byte-identical: it selects the
detailed `_print_info` view, and in `_plan_builds` it re-raises a tagging failure verbatim
instead of folding it into the aggregated batch message. Tests depend on both.

Every batch check runs in `_plan_builds`/`refuse_tag_collisions` *before* the first
`build_package`, because `_place_wheel` catching a collision on wheel five leaves four
wheels in `-o` that nothing removes. The three refusals are a tag collision between
differing binaries (identical ones are a reproducible rebuild, so they are exempt),
binaries whose default aliases disagree (the installed command would vary by platform),
and `--platform-tag` over more than one input. Tagging failures are collected across the
whole batch and reported together: a user who must re-run after each complaint gives up
before the seventh.

`_keep_dir` gives each build its own subdirectory under `--keep-project` when batching,
since `build_package` does `rmtree` then `copytree` and would otherwise leave only the
last one.

`builder.with_variants` is why a batch's README describes the batch. PyPI renders one description
for a project and takes it from one of the uploaded files, so a README naming only its own binary
is wrong on that page for everyone who installed a different platform's wheel. Every spec in a
batch therefore gets the whole set — tag, kind and digest per wheel — and `scaffold._variant_facts`
renders it. Two things are deliberately dropped in that mode: the `tag_note`, since each note
explains one tag and the eleven do not share an explanation, and the `- **file**` line, since the
packaged name varies (`starship` vs `starship.exe`). Without it every wheel in the batch renders a
byte-identical block, which is the property that makes PyPI's choice of file not matter — there is
a test asserting exactly that. `with_variants` copies the specs rather than mutating them: a
caller's `PackageSpec` is theirs, and rewriting one in place would make a wheel's contents depend
on whether the same object had been passed to an earlier batch.

Every command ends by printing the one that usually follows it, via `cli._suggest` — the
fetch → inspect → build → publish pipeline is only obvious to someone who already knows it.
`_quote` is not `shlex.quote`: it must leave `dist/*.whl` unquoted so the shell still expands it,
and leave `<name>` placeholders readable. When `inspect` or `build` meets a binary it cannot tag,
`_removal_advice` prints the literal `rm -r` that clears the directory, naming both the unpacked
tree *and* the archive beside it — leaving the tarball behind means the next `fetch --extract`
puts the binary straight back. A test parses that line out of the output and runs it, so advice
naming the wrong paths fails the suite rather than the user.

### Fetching is one HTTP choke point and two digest sources

Every request `fetch.py` makes goes through `fetch._open`, which is what makes the suite
hermetic: `conftest.fake_http` replaces that one function with a routing table, and nothing else
needs stubbing. Adding a second way out to the network would break that guarantee silently, so
route new calls through `_open` too.

`_StripAuthOnRedirect` drops `Authorization` when a redirect crosses to another host. This is not
optional politeness — urllib copies every header onto the redirected request, release downloads
redirect from the API to object storage, and that host rejects a request carrying a second set of
credentials. So it is simultaneously what stops a GitHub token leaking to a third party and the
reason authenticated downloads work at all.

Digests come from `Asset.digest` (the API's own, computed by GitHub on upload) or, when that is
null, from a checksum file in the release. Both are needed: GitHub only began recording digests
in 2025 and never backfilled, so any older release falls through to `_sidecar_digest` then
`_manifest_digest`. `parse_checksums` handles both conventions in the wild, and `allow_bare` is
what separates them — a lone digest is only meaningful in a file whose *name* says what it
covers, which is why only the sidecar path passes it.

`is_checksum_asset` keeps checksum files out of the payload set even when a pattern matches them,
so `'*.tar.gz*'` behaves the way anyone would expect. `unpackable_reason` extends the same rule to
assets that could never become a wheel — installers (`.msi`, `.deb`, …), signatures, documentation
— so `--list` shows what `build` could consume rather than listing a `.msi` and letting the user
discover three steps later that nothing can open it. Both are stated as a blacklist because the
asset with *no* extension is the one that matters: a release shipping the bare executable is
exactly what `build` wants, and a whitelist would drop it. The reasons are phrased as plural nouns
so one string serves both the `--list` footer (`3 installers wheelbarrow cannot unpack`) and the
error on a pattern that matched only those. `select_assets` raising on a pattern that matches
nothing is deliberate: a quiet empty result is how an upstream rename turns into a missing wheel
three steps later.

`FetchedAsset.executables` decides membership by parsing the headers, never by the executable bit
— the same rule as `discover.collect`, and for the same reason. A Windows-produced zip stores DOS
attributes rather than a Unix mode, so `starship.exe` comes out of one with no `+x`; counting the
bit reported "12 fetched, 9 extracted" for a release whose every archive held a binary, while
`build` went on to package all twelve.

`_extract_tar` reconstructs member paths with `m.name.lstrip("/")` rather than `m.name`. The
`data` filter defangs an absolute member by stripping the leading separator instead of refusing
it, and `Path(dest) / "/abs"` is `/abs` — so joining the raw name would report a path nothing was
written to.

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

`PackageSpec.installed_names` encodes the consequence, and is the single source of truth that
`staged_paths` and `archive_executables` both derive from — change it and they follow. In direct
mode the staged file *becomes* the command, so it is named after each alias rather than the source
file, except that a `WINDOWS_EXEC_SUFFIXES` extension is carried over: `Scripts\starship` without
the `.exe` is a file Windows will not execute, while a `.sh` on `PATH` is only noise, so the rule
is deliberately narrow rather than "keep the suffix".

### Conventions

- All user-facing failures raise a `WheelbarrowError` subclass from `errors.py`. `cli.py` catches
  only that base class and turns it into `error: ...` on stderr with exit code 1; anything else
  escaping as a traceback is a bug.
- Generated file bodies live entirely in `templates.py` as `string.Template` objects — keep them
  readable as the Python/TOML they become, and render TOML values through `toml_str`/`toml_array`
  rather than interpolating raw strings. The generated `README.md` is the wheel's long
  description, so it is written for whoever installs the package: no wheelbarrow jargon
  (`LAUNCHER_NOTES` exists because "direct" and "shim" mean nothing on a PyPI page), and nothing
  the input may not be — a `#!` script is a "program", never "a prebuilt executable".
- `scaffold.describe_input` builds the README's provenance block, and `_describe_tag` is the part
  to be careful with: every branch is guarded on what the *tag* says, not only on what the binary
  is. `--platform-tag` can put the two in contradiction, and a gloss drawn from the binary would
  then describe a wheel that does not exist. `PackageSpec.provenance` stays optional so a library
  caller assembling a spec by hand still renders a correct README, just a barer one.
- `uv_build` is a direct runtime dependency of wheelbarrow so the default (non-`--isolated`) build
  path can run the backend in-process instead of provisioning a venv. It is pinned to `>=0.11.30,<0.12`
  in both `pyproject.toml` and the generated `PYPROJECT` template; those pins must move together.
- Publishing shells out to `uv publish`. The token is read from `UV_PUBLISH_TOKEN` by
  `publish.resolve_token()` and never travels in `argv`: there is deliberately no `--token`
  option, and `run_publish` lets uv inherit the environment rather than forwarding the value.
  Keep credentials out of `PublishPlan.argv` and `display()`. `fetch.resolve_token()` follows
  the same rule for `GH_TOKEN`/`GITHUB_TOKEN`: environment only, no option, never printed.
- User-facing spelling is `licence`, but `scaffold.render_pyproject` must emit `license` —
  PEP 621 fixes that key, and a backend silently ignores an unrecognised one, dropping the
  field from the wheel's METADATA with no error.

### Tests

**No test may touch the network.** `tests/conftest.py` assembles synthetic ELF, Mach-O, fat Mach-O
and PE images byte by byte, so the suite is hermetic and every platform path is exercised from any
host. Add new format coverage by extending those builders rather than committing binary fixtures.
`fake_http` (also in conftest) replaces `fetch._open`, the single function every GitHub request
goes through, and `pypi` tests stub `urlopen`; credential tests always monkeypatch the token
variables rather than reading whatever the developer has exported.

`tests/test_cli.py` pins the rich console width in an autouse fixture. The consoles are module
level and size themselves at import -- 80 columns when nothing is a terminal, narrow enough to wrap
a path or an option name across two lines and quietly defeat an `in` assertion.

`TestInstalledWheelRuns` in `tests/test_build.py` really installs a built wheel (pip, falling back to
uv) into a `--target` directory and executes it, using a `/bin/sh` script plus an explicit
`--platform-tag any` to stay portable. It skips on Windows and when no installer is available.
