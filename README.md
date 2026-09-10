# venv-repair

**Repair a renamed or moved Windows `venv` in place — without reinstalling a single package.**

<sub>[English](README.md) · [简体中文](README.zh-CN.md)</sub>

If you have ever done this:

```bat
ren E:\jupyyer jupyter
```

…and then found that `pip.exe`, `jupyter.exe` and every other console script in `venv\Scripts`
now do *nothing at all* — no error message, just exit code 1 — this tool fixes it in about a second.

```console
> venv-repair E:\jupyter\venv --dry-run
old path : E:\jupyyer\venv
new path : E:\jupyter\venv
length   : 15 -> 15 characters (15 -> 15 bytes)

  [ ok ] pip.exe                            1 occurrence(s)
  [ ok ] jupyter-lab.exe                    1 occurrence(s)
  ...
patched 40 file(s), 0 unrepairable

dry run: no file was modified.
```

## The problem

A console script on Windows is not an ordinary program. pip ships it as a small
launcher stub, a shebang line holding the **absolute** interpreter path, and an
embedded zip payload:

```
[ NUL padding ][ #!E:\jupyyer\venv\Scripts\python.exe\n ][ embedded ZIP ]
```

The path is baked in when the script is installed. Rename or move the `venv`
folder and every launcher points at a directory that no longer exists.

Two details make this confusing:

- **The failure is silent.** The launcher exits 1 without printing anything, so it
  looks like the tool broke rather than the path.
- **`python.exe` keeps working.** It finds `pyvenv.cfg` relative to itself, which is why
  `python -m pip install ...` still works while `pip install ...` does not. That is also
  your escape hatch while the environment is broken.

## Install

Nothing to install — it is a single file with no dependencies:

```console
> curl -O https://raw.githubusercontent.com/Suzuka-sama/venv-repair/main/venv_repair.py
> python venv_repair.py --help
```

Or install the console script:

```console
> pip install git+https://github.com/Suzuka-sama/venv-repair
> venv-repair --help
```

Requires Python 3.9+ and Windows.

## Usage

```console
venv-repair <venv-dir> [--old <old-path>] [--dry-run] [--no-backup] [--verify] [--quiet]
```

| Flag | Meaning |
|---|---|
| `<venv-dir>` | The venv directory, **at its current path** |
| `--old PATH` | The path the venv used to live at. Auto-detected when omitted |
| `--dry-run` | Report what would change; write nothing |
| `--no-backup` | Skip copying `Scripts` to `<venv>_Scripts_backup` |
| `--verify` | Afterwards, actually run `pip.exe --version` to prove it works |
| `--quiet` | Only print errors |

Exit codes: `0` repaired · `1` something could not be repaired (or `--verify` failed) ·
`2` not a venv · `3` stale path ambiguous, pass `--old`.

Recommended flow:

```console
> venv-repair E:\jupyter\venv --dry-run          # 1. see what it would do
> venv-repair E:\jupyter\venv --verify           # 2. repair, then prove it
```

## How it works

`venv-repair` rewrites the baked-in paths **in place**. It never reinstalls packages and
it never changes the size of a launcher file — the embedded zip keeps its original offset,
so the PE image is left byte-for-byte untouched.

Three strategies are used, all of them length-preserving:

| New path vs. old | What happens |
|---|---|
| same length | direct byte-for-byte replacement |
| longer | shift left into the NUL padding in front of the shebang; the trailing newline does not move |
| shorter | pad the line with trailing spaces (the launcher parser ignores them) |

It also rewrites `pyvenv.cfg` and the text scripts in `Scripts\` (`activate`,
`activate.bat`, `deactivate.bat`, …).

If a path grows further than the available padding can absorb, the file is **left alone
and reported as unrepairable** rather than corrupted on a guess. In that case use the
fallback below.

### Details that matter

- The encoding of the baked-in path is **detected per venv**, never assumed. pip 23.1+
  (the launchers bundled with Python 3.12/3.13) writes the shebang as UTF-8, while older
  distlib launchers used the ANSI code page. Guessing wrong is exactly how a "repaired"
  venv stays broken, so the tool searches for the old path in the actual bytes to decide.
  Non-ASCII directories such as `E:\projects\中文目录\venv` work either way.
- **8.3 short names are understood.** A directory has two valid spellings on Windows,
  `C:\Users\RUNNER~1\...` and the long one, and pip and the `venv` module disagree about
  which to use inside one venv: pip resolves the interpreter path for the launchers, while
  `venv` writes into `pyvenv.cfg` whatever spelling it was handed. Both sources are read,
  candidates are compared by *directory identity* rather than by string, and every
  spelling found is repaired. Without this, a venv under a short-named directory gets only
  its text files fixed and every `.exe` left broken.
- **Verification ignores the console code page.** `--verify` runs the launchers with
  `PYTHONIOENCODING=utf-8` set for the child. pip prints its own location, which contains
  the venv path, and on a machine whose ANSI code page cannot represent that path (cp1252
  against a Chinese directory, say) the *print* raises `UnicodeEncodeError`. That is a
  console limitation, not a broken launcher, and reporting it as a failed repair would
  send you off to rebuild a perfectly good environment.
- `pip.exe` has an extra CRLF after the shebang newline; detection tolerates it instead
  of assuming the zip starts immediately.
- The shebang is `#!<venv root>\Scripts\python.exe`, not just the venv root — checking
  only the root reports healthy files as broken.
- Ownership is preserved: files are rewritten atomically via a temporary file plus
  `os.replace`.

## Safety

- **`--dry-run`** first — it reports findings without writing.
- **Automatic backup** — `Scripts` is copied to `<venv>_Scripts_backup` before any write.
  The previous backup is only deleted once the new copy is in place.
- **Post-check** — every launcher is re-read afterwards and its shebang compared against
  the old path; anything still broken is listed explicitly.
- **Refusal over corruption** — unrepairable launchers are skipped and reported.

## Verify manually

```console
> E:\jupyter\venv\Scripts\pip.exe --version
> E:\jupyter\venv\Scripts\jupyter.exe --version
> E:\jupyter\venv\Scripts\jupyter.exe kernelspec list
```

`pip --version` should print the **new** path. Then delete `<venv>_Scripts_backup` once
you are satisfied.

## Fallback: recreate the environment

If the tool refuses to repair, or you just want a pristine environment, recreate it.
This works as long as the moved venv's `python.exe` still runs, so you can carry the
package list across:

```console
> E:\jupyter\venv\Scripts\python.exe -m pip freeze > %TEMP%\req.txt
> python -m venv E:\jupyter\venv
> E:\jupyter\venv\Scripts\python.exe -m pip install -r %TEMP%\req.txt
```

Watch out for `-e` editable installs in `pip freeze` output — they point at the old
location and need to be reinstalled from source.

## Alternatives and prior art

This is not the first tool in this space, and it does not try to be. If your situation
does not match this tool's design, one of these may serve you better:

| Project | Approach | Notes |
|---|---|---|
| [ci-ke/venv-fix](https://github.com/ci-ke/venv-fix) | byte-patches launchers, splices the shebang | closest relative; also rewrites `Scripts\*.py`. Hardcodes `.encode('ascii')`, so it raises `UnicodeEncodeError` on non-ASCII paths, and splicing changes file size — a path that grows past the NUL padding can damage the PE image without warning |
| [rr-info/move-venv](https://github.com/rr-info/move-venv) | string replacement across a venv | self-described as *"almost GUARANTEED TO FAIL"*; shells out to `cp` and `file` |
| [hsupu/fix_entrypoints.py](https://gist.github.com/hsupu/feacdda135332d847bd5e3ccaa3ee351) | regenerates launchers via pip's vendored `distlib` | requires pip inside the venv to still be importable |
| [WildinFree/VenvRepath](https://github.com/WildinFree/VenvRepath) | GUI, replaces an old path across arbitrary project files | broader scope, not specific to launcher internals |
| `python -m venv --upgrade <dir>` | rebuilds the environment in place | does not touch third-party `Scripts\*.exe` launchers |

What `venv-repair` adds: strictly length-preserving patching, non-ASCII path support,
8.3 short-name awareness (compared by directory identity, not by string), `--dry-run`,
automatic backup, and a post-repair check that says plainly whether the files are still
broken.

## Limitations

- Only handles venvs created by the standard library (`python -m venv`). Poetry, Conda and
  `virtualenv` environments use different layouts.
- Windows only. On POSIX systems the fix is a one-line shebang edit, and `venv` is not
  relocatable there either — recreate instead.
- If a venv was moved more than once, older paths may differ between files; pass `--old`
  explicitly.
- Python 3.9's `pyvenv.cfg` does not record the venv path at all (the `command` line came
  later), so on 3.9 detection relies entirely on the launcher shebangs and there is nothing
  to rewrite in that file. Everything else behaves the same.
- Paths inside `Lib\site-packages\**\__pycache__\*.pyc` and `*.dist-info` may retain the old
  path. This is harmless and is deliberately not touched.

## Development

```console
> python -m unittest discover -s tests -v
```

The suite builds real virtual environments, moves them, and asserts that `pip.exe`
actually runs afterwards — including that no file changed size. It runs on Windows in CI
across Python 3.9, 3.11 and 3.13.

## License

MIT — see [LICENSE](LICENSE).
