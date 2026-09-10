# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-10

### Added

- Initial release.
- In-place repair of launcher paths in a renamed or moved Windows venv, using three
  strictly length-preserving strategies (equal length, shift into NUL padding, pad with
  spaces).
- Automatic detection of the stale path from launcher shebangs and `pyvenv.cfg`, with
  `--old` to override and a clear error when several candidates conflict.
- Coverage of `Scripts\*.exe` launchers, the text scripts in `Scripts\` (`activate`,
  `activate.bat`, `deactivate.bat`, …) and `pyvenv.cfg`.
- `--dry-run`, automatic `Scripts` backup, and a post-repair check that reports any
  launcher still pointing at the old path.
- `--verify`, which runs `pip.exe --version` (and friends) to prove the repair worked.
- Non-ASCII path support: the encoding of the baked-in path is detected per venv
  (UTF-8 for pip 23.1+ launchers, the ANSI code page for older distlib ones) instead of
  being assumed.
- 8.3 short-name awareness. One directory has two valid spellings on Windows and a single
  venv uses both -- pip writes the resolved (long) path into the launchers, while
  `pyvenv.cfg` keeps whatever spelling `venv` was handed. Both sources are read,
  candidates are compared by directory identity rather than by string, and every spelling
  found is repaired, so the `.exe` launchers are fixed as well instead of only the text
  scripts.
- `--verify` pins `PYTHONIOENCODING=utf-8` for the launchers it runs, so the check reports
  whether the *launcher* works rather than whether the console code page can represent the
  venv path.
- End-to-end test suite that builds real virtual environments, moves them, and asserts
  that no file changes size during the repair.
- GitHub Actions CI on Windows across Python 3.9, 3.11 and 3.13.

[1.0.0]: https://github.com/Suzuka-sama/venv-repair/releases/tag/v1.0.0
