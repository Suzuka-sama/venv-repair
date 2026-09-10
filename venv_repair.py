#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""venv-repair -- make a renamed or moved Windows venv work again, in place.

Why this is needed
------------------
On Windows, pip ships console-script launchers (``Scripts\\pip.exe``,
``Scripts\\jupyter.exe``, ...) that are not ordinary programs.  Each one is a
small launcher stub, followed by a shebang line holding the *absolute* path of
the interpreter, followed by an embedded zip payload::

    [ NUL padding ][ #!<venv>\\Scripts\\python.exe\\n ][ embedded ZIP ]

That path is baked in when the script is installed, so renaming or moving the
``venv`` folder makes every one of those launchers fail **silently**: no output
at all, exit code 1.  ``Scripts\\python.exe`` keeps working, because it locates
``pyvenv.cfg`` relative to itself -- which is why ``python -m pip`` still runs
fine and people conclude "only pip.exe is broken".

What this tool does
-------------------
Rewrites the baked-in paths in place.  It does not reinstall packages, and it
never changes the size of a launcher file: the embedded zip keeps its original
offset and the PE image is left byte-for-byte untouched.

Three strategies, all of them length preserving:

+----------------------+---------------------------------------------------+
| new path vs old path | what happens                                      |
+======================+===================================================+
| same length          | direct byte-for-byte replacement                  |
+----------------------+---------------------------------------------------+
| longer               | shift left into the NUL padding in front of the   |
|                      | shebang; the trailing newline does not move       |
+----------------------+---------------------------------------------------+
| shorter              | pad the line with trailing spaces                 |
+----------------------+---------------------------------------------------+

When the padding is too small to absorb a longer path, the file is left alone
and reported as unrepairable, rather than being corrupted on a guess.

Paths are matched in the encoding the file itself uses, which is *detected*
rather than assumed: the launchers shipped with pip 23.1+ (Python 3.12/3.13) store
the shebang as UTF-8, while older distlib launchers used the ANSI code page.  Both
are handled, so non-ASCII paths such as ``E:\\\u4e2d\u6587\u76ee\u5f55\\venv`` work.

Directories are compared by *identity*, not by string, because Windows gives one
directory two valid spellings (``C:\\Users\\RUNNER~1\\...`` and the long form) and
a single venv ends up using both: pip resolves the interpreter path for the
launchers while the ``venv`` module writes into ``pyvenv.cfg`` whatever spelling it
was handed.  Every spelling found is repaired, so the ``.exe`` launchers are fixed
too instead of only the text scripts.

Usage
-----
    venv-repair <venv-dir> [--old <old-path>] [--dry-run] [--no-backup] [--verify]

Exit codes
----------
    0   repaired, nothing left broken
    1   at least one launcher could not be repaired (or --verify failed)
    2   the given directory is not a venv
    3   stale path could not be determined, or is ambiguous
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

__version__ = "1.0.0"

#: Marker of the embedded zip payload that follows the shebang.
ZIP_SIG = b"PK\x03\x04"

#: Real interpreters do not carry a baked-in venv path; skip them.
INTERPRETER_NAMES = {"python.exe", "pythonw.exe", "python_d.exe", "pythonw_d.exe"}

#: Maximum number of filler bytes tolerated between the shebang newline and the
#: zip signature.  ``pip.exe`` has an extra CRLF there, hence 3 rather than 1.
ZIP_GAP_MAX = 3

#: Byte lengths of the well-known path suffix that follows the venv root.
SUFFIX = b"\\Scripts\\python.exe"


# --------------------------------------------------------------------------- #
# encoding helpers
# --------------------------------------------------------------------------- #
#: Encodings a launcher shebang may use, most likely first.
#:
#: pip 23.1+ (and the launchers bundled with Python 3.12/3.13) write the shebang
#: as UTF-8 -- the launcher's own diagnostics contain the string "Expected to
#: decode shebang line using UTF-8".  Older distlib launchers used the ANSI code
#: page (``mbcs``).  We never guess: see :func:`detect_codec`.
CODECS = ("utf-8", "mbcs")


def encode_path(path: str, codec: str = "utf-8") -> bytes:
    """Encode *path* the way this venv's launchers store it."""
    try:
        return path.encode(codec)
    except (LookupError, UnicodeEncodeError):
        pass
    for fallback in CODECS:
        try:
            return path.encode(fallback)
        except (LookupError, UnicodeEncodeError):
            continue
    return path.encode("utf-8", "replace")


def decode_path(raw: bytes, codec: str) -> str | None:
    try:
        return raw.decode(codec)
    except (LookupError, UnicodeDecodeError):
        return None


def detect_codec(venv: str, old) -> str:
    """Work out which encoding this venv's launchers used for the baked-in path.

    *old* may be a single path or a list of alternative spellings of the same
    directory.  Picking the wrong encoding is exactly how a "repaired" venv
    stays broken, so the choice is made by looking for the *old* path in the
    actual bytes.  For ASCII paths every candidate encoding is byte-identical
    and the result is moot.
    """
    wanted = [old] if isinstance(old, str) else list(old)
    for codec in CODECS:
        needles = []
        for cand in wanted:
            try:
                needles.append(cand.encode(codec))
            except (LookupError, UnicodeEncodeError):
                continue
        if not needles:
            continue
        for _name, path in _script_files(venv):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if any(needle in data for needle in needles):
                return codec
    return "utf-8"


def normalize(path: str) -> str:
    """Absolute, no trailing separator."""
    return os.path.abspath(path).rstrip("\\/") or path


def same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(a) == os.path.normcase(b)
    except Exception:  # pragma: no cover - defensive
        return a == b


def _win_long(path: str) -> str:
    """Long (non-8.3) form of an existing path; ``realpath`` elsewhere."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetLongPathNameW(path, buf, len(buf)):
            return buf.value
    except Exception:  # pragma: no cover - non-Windows or odd volume
        pass
    return os.path.realpath(path)


def _win_short(path: str) -> str | None:
    """8.3 form of an existing path, or ``None`` when the volume has none."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)):
            short = buf.value
            if short and os.path.normcase(short) != os.path.normcase(path):
                return short
    except Exception:  # pragma: no cover - defensive
        pass
    return None


def long_form(path: str) -> str:
    """Long form of *path*, tolerating a tail that no longer exists.

    Windows offers two spellings for one directory -- long
    (``...\\my-project``) and 8.3-short (``...\\MY-PRO~1``) -- and the launchers
    and ``pyvenv.cfg`` can disagree about which to use, because pip resolves the
    interpreter path while the ``venv`` module records whatever it was handed.
    Resolving the longest *existing* prefix and re-attaching the rest keeps the
    two comparable even after the directory has been renamed away.
    """
    if os.name != "nt":
        return os.path.realpath(path)
    head = os.path.abspath(path)
    tail: list[str] = []
    while not os.path.exists(head):
        parent, name = os.path.split(head)
        if not name or parent == head:
            return os.path.abspath(path)
        tail.insert(0, name)
        head = parent
    resolved = _win_long(head)
    return os.path.join(resolved, *tail) if tail else resolved


def canonical(path: str) -> str:
    """Directory identity that ignores 8.3 spelling and case."""
    try:
        return os.path.normcase(long_form(path)).rstrip("\\/")
    except Exception:  # pragma: no cover - defensive
        return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def spellings(path: str) -> list[str]:
    """Every plausible spelling of *path*: as given, long form, short form."""
    out: list[str] = []
    given = normalize(path)
    for cand in (given, long_form(given), _win_short(long_form(given))):
        if cand and cand not in out:
            out.append(cand)
    return out


def is_absolute(path: str) -> bool:
    return bool(path) and (
        (len(path) > 2 and path[1] == ":" and path[0].isalpha())
        or path.startswith("\\\\") or path.startswith("//")
    )


# --------------------------------------------------------------------------- #
# launcher parsing / patching
# --------------------------------------------------------------------------- #
def read_shebang(data: bytes) -> tuple[int, int, bytes] | None:
    """Return ``(start, end, line)`` of the launcher shebang, or ``None``.

    ``start`` is the index of ``#!``, ``end`` the index of the terminating
    newline, ``line`` the bytes between them.
    """
    zoom = data.find(ZIP_SIG)
    if zoom < 0:
        return None
    i = data.rfind(b"#!", 0, zoom)
    if i < 0:
        return None
    nl = data.find(b"\n", i)
    if nl < 0 or nl >= zoom + ZIP_GAP_MAX or zoom - nl > ZIP_GAP_MAX:
        return None
    return i, nl, data[i + 2:nl]


def _prefix_eq(prefix: bytes, old_b: bytes, codec: str) -> bool:
    """Case-insensitive comparison that is safe for multi-byte encodings.

    ``bytes.lower()`` is ASCII-only, so calling it on an ANSI (``mbcs``) Chinese
    path corrupts it: cp936 trail bytes cover 0x40-0xFE, which includes the ASCII
    upper-case range.  Comparing the *decoded* strings folds case per code point
    instead, which is correct for every encoding we support.
    """
    if prefix == old_b:
        return True
    if len(prefix) != len(old_b):
        return False
    try:
        return prefix.decode(codec).lower() == old_b.decode(codec).lower()
    except (LookupError, UnicodeDecodeError):
        return False


def patch_launcher(data: bytes, old_b: bytes, new_b: bytes,
                   codec: str = "utf-8") -> bytes | None:
    """Repair one launcher, or return ``None`` if it cannot be done safely.

    The returned blob always has exactly the same length as *data*.
    """
    parsed = read_shebang(data)
    if parsed is None:
        return None
    i, nl, line = parsed

    core = line.rstrip(b"\r")
    if len(core) <= len(old_b):
        return None
    tail = core[len(old_b):]
    if not _prefix_eq(core[:len(old_b)], old_b, codec):
        return None
    if b"python" not in tail.lower():
        return None

    newline = new_b + tail + line[len(core):]
    delta = len(newline) - len(line)
    start = i

    if delta > 0:
        # Shift the shebang left, consuming NUL padding.  The newline that ends
        # the line stays where it is, so the zip offset is untouched.
        pad = 0
        k = i - 1
        while k >= 0 and data[k] == 0 and pad < delta:
            k -= 1
            pad += 1
        if pad < delta:
            return None
        start = i - delta
    elif delta < 0:
        # Pad inside the line: the launcher parser ignores trailing spaces.
        newline += b" " * (-delta)

    out = data[:start] + b"#!" + newline + data[nl:]
    if len(out) != len(data):
        return None
    if len(old_b) == len(new_b):
        # Same length everywhere: also sweep any other occurrence in the file.
        out = out.replace(old_b, new_b)
    return out


# --------------------------------------------------------------------------- #
# stale path detection
# --------------------------------------------------------------------------- #
def _candidates_from_cfg(venv: str) -> list[str]:
    """The venv target recorded by ``python -m venv`` in pyvenv.cfg.

    pyvenv.cfg is written as UTF-8, so this yields the old path as a proper
    string instead of something we have to guess an encoding for.  It is the
    most trustworthy source and is consulted first.
    """
    found: list[str] = []
    cfg = os.path.join(venv, "pyvenv.cfg")
    if not os.path.isfile(cfg):
        return found
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return found
    for raw in text.splitlines():
        if not raw.lower().strip().startswith("command") or "-m venv" not in raw:
            continue
        value = raw.split("-m venv", 1)[1].strip().strip('"').strip("'")
        if value and is_absolute(value):
            found.append(value)
    return found


def _shebang_roots(venv: str) -> list[str]:
    """Plausible venv roots read out of launcher shebangs.

    Each shebang is decoded with every candidate codec and the first decoding
    that yields an absolute path wins.  This matters: decoding with the wrong
    codec produces mojibake that decodes "successfully" and would otherwise be
    reported as a second, conflicting old path.
    """
    roots: list[str] = []
    for name, path in _script_files(venv):
        if name.lower() in INTERPRETER_NAMES:
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        parsed = read_shebang(data)
        if parsed is None:
            continue
        raw = parsed[2].rstrip(b"\r")
        for codec in CODECS:
            line = decode_path(raw, codec)
            if not line:
                continue
            idx = line.lower().find("\\scripts\\")
            if idx <= 0:
                continue
            root = line[:idx]
            if is_absolute(root):
                roots.append(root)
                break
    return roots


def _stale_spellings(venv: str) -> tuple[list[str], list[str]]:
    """Spellings of the path the venv used to live at, plus conflicts.

    Returns ``(spellings, ambiguous)``.  ``spellings`` holds every spelling of
    the single stale directory that was found -- typically the 8.3-short one
    from ``pyvenv.cfg`` and the long one from the launcher shebangs -- and is
    empty when the venv looks healthy.

    Both sources are always consulted, and candidates are compared by directory
    identity rather than by string.  Comparing strings is what made this fail on
    GitHub runners, whose temp directory is ``C:\\Users\\RUNNER~1\\...``: pip
    writes the *long* spelling into the launchers and the ``venv`` module writes
    the *short* one into ``pyvenv.cfg``, so the two looked like different
    directories.  The result was a repair that silently fixed only the text
    files and left every ``.exe`` broken -- or, on a healthy venv, "repaired"
    launchers that were never broken.
    """
    venv_key = canonical(venv)
    groups: dict[str, list[str]] = {}
    for cand in list(_candidates_from_cfg(venv)) + _shebang_roots(venv):
        try:
            key = canonical(cand)
        except Exception:  # pragma: no cover - defensive
            continue
        if key == venv_key:
            continue
        bucket = groups.setdefault(key, [])
        for spelling in spellings(cand):
            if spelling not in bucket:
                bucket.append(spelling)

    if not groups:
        return [], []
    if len(groups) == 1:
        return next(iter(groups.values())), []
    return [], sorted(group[0] for group in groups.values())


def detect_stale_path(venv: str) -> tuple[str | None, list[str]]:
    """Best-effort detection of the path the venv used to live at.

    Returns ``(path, ambiguous)``.  ``path`` is ``None`` when the venv looks
    healthy; ``ambiguous`` lists conflicting candidates when there is more than
    one (e.g. the folder was moved twice) and the caller must pass ``--old``.
    """
    found, ambiguous = _stale_spellings(venv)
    if ambiguous:
        return None, ambiguous
    return (found[0] if found else None), []


# --------------------------------------------------------------------------- #
# repair driver
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.patched: list[tuple[str, int]] = []
        self.unrepairable: list[str] = []
        self.residual: list[str] = []
        self.backup: str | None = None
        self.still_broken: list[str] = []
        self.verified: list[tuple[str, bool, str]] = []

    @property
    def ok(self) -> bool:
        return not self.unrepairable and not self.still_broken


def _script_files(venv: str) -> list[tuple[str, str]]:
    """Every regular file in ``Scripts``, as ``(name, full_path)`` pairs."""
    scripts = os.path.join(venv, "Scripts")
    if not os.path.isdir(scripts):
        return []
    out = []
    for name in sorted(os.listdir(scripts)):
        path = os.path.join(scripts, name)
        if os.path.isfile(path):
            out.append((name, path))
    return out


def repair(
    venv: str,
    old: str | None = None,
    dry_run: bool = False,
    backup: bool = True,
    log=print,
) -> Report:
    venv = normalize(venv)
    report = Report()

    if not os.path.isfile(os.path.join(venv, "pyvenv.cfg")):
        raise NotAVenv(venv)

    if old is None:
        old_spellings, ambiguous = _stale_spellings(venv)
        if ambiguous:
            raise AmbiguousPath(ambiguous)
        if not old_spellings:
            log("No stale path found -- this venv looks healthy, nothing to do.")
            return report
        old = old_spellings[0]
    else:
        old = normalize(old)
        if canonical(old) == canonical(venv):
            log("Old and new path are identical -- nothing to do.")
            return report
        # The user named one spelling, but the launchers may hold another.
        old_spellings = spellings(old)

    codec = detect_codec(venv, old_spellings)

    # One (old, new) byte pair per spelling, all pointing at the same new
    # directory: a launcher holds the long spelling and pyvenv.cfg the short
    # one, so a single pair would only ever match half the files.  The
    # replacement is always the *long* spelling -- the form pip itself writes,
    # and the only one guaranteed not to invent a fresh 8.3 alias for a name
    # that happens to be long enough to have one.
    new_b = encode_path(long_form(venv), codec)
    pairs: list[tuple[bytes, bytes]] = []
    for spelling in old_spellings:
        old_b = encode_path(spelling, codec)
        if old_b != new_b and (old_b, new_b) not in pairs:
            pairs.append((old_b, new_b))
    if not pairs:
        log("Old and new path are identical -- nothing to do.")
        return report

    log("old path : %s" % old)
    log("new path : %s" % venv)
    log("encoding : %s" % codec)
    for old_b, new_b in pairs:
        log("length   : %d -> %d characters (%d -> %d bytes)"
            % (len(old), len(venv), len(old_b), len(new_b)))
    log("")

    if backup and not dry_run:
        # Remove the previous backup only after the new one is in place, so a
        # failed copy never leaves the user without a backup at all.
        live = os.path.join(venv, "Scripts")
        if os.path.isdir(live):
            staging = venv + "_Scripts_backup_new"
            if os.path.exists(staging):
                shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(live, staging)
            final = venv + "_Scripts_backup"
            if os.path.exists(final):
                shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
            report.backup = final
            log("[backup] %s" % final)

    # ---- 1. launchers and text scripts in Scripts\ ------------------------- #
    for name, path in _script_files(venv):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            log("  [skip] %-34s %s" % (name, exc))
            continue

        hits = [(o, n) for o, n in pairs if o in data]
        if not hits:
            continue

        is_launcher = ZIP_SIG in data and read_shebang(data) is not None
        if is_launcher:
            # A launcher's shebang carries exactly one spelling; use whichever
            # pair that is.
            chosen = next((pair for pair in hits
                           if patch_launcher(data, pair[0], pair[1],
                                             codec) is not None), None)
            if chosen is None:
                report.unrepairable.append(name)
                log("  [FAIL] %-34s cannot patch in place" % name)
                continue
            new = patch_launcher(data, chosen[0], chosen[1], codec)
            hits = [chosen]
        elif name.lower() in INTERPRETER_NAMES:
            # A real interpreter never contains the venv path; if we got here
            # something unusual is going on, so do not touch it.
            continue
        else:
            # Plain script (activate, activate.bat, deactivate.bat, ...).
            new = data
            for old_b, new_b in hits:
                new = new.replace(old_b, new_b)

        if not dry_run:
            tmp = path + ".venv-repair.tmp"
            with open(tmp, "wb") as fh:
                fh.write(new)
            os.replace(tmp, path)

        count = sum(data.count(old_b) for old_b, _ in hits)
        report.patched.append((name, count))
        log("  [ ok ] %-34s %d occurrence(s)" % (name, count))

    # ---- 2. pyvenv.cfg ----------------------------------------------------- #
    cfg = os.path.join(venv, "pyvenv.cfg")
    if os.path.isfile(cfg):
        with open(cfg, "rb") as fh:
            raw = fh.read()
        hits = [(o, n) for o, n in pairs if o in raw]
        if hits:
            fresh = raw
            for old_b, new_b in hits:
                fresh = fresh.replace(old_b, new_b)
            if not dry_run:
                with open(cfg, "wb") as fh:
                    fh.write(fresh)
            count = sum(raw.count(old_b) for old_b, _ in hits)
            report.patched.append(("pyvenv.cfg", count))
            log("  [ ok ] %-34s %d occurrence(s)" % ("pyvenv.cfg", count))

    log("")
    log("patched %d file(s), %d unrepairable"
        % (len(report.patched), len(report.unrepairable)))

    # ---- 3. post-check ----------------------------------------------------- #
    # Skipped on a dry run: nothing was written, so every launcher would of
    # course still look broken and the report would be meaningless.
    if not dry_run:
        stale_keys = {canonical(spelling) for spelling in old_spellings}
        for name, path in _script_files(venv):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            parsed = read_shebang(data)
            if parsed is None:
                continue
            line = decode_path(parsed[2].rstrip(b"\r"), codec) or ""
            low = line.lower()
            idx = low.find("\\scripts\\")
            if idx <= 0:
                continue
            root = line[:idx]
            # Compare by directory identity: a shebang may spell the *new* path
            # with the 8.3 short name even when we wrote the long one.
            if canonical(root) in stale_keys:
                report.still_broken.append(name)
        if os.path.isfile(cfg):
            with open(cfg, "rb") as fh:
                blob = fh.read()
            if any(old_b in blob for old_b, _ in pairs):
                report.residual.append("pyvenv.cfg")

    if report.unrepairable:
        log("")
        log("Not repairable in place (longer path, not enough NUL padding):")
        for name in report.unrepairable:
            log("  - %s" % name)
        log("")
        log("Recreate the environment instead -- see README section 'Fallback'.")
    if report.still_broken:
        log("")
        log("STILL BROKEN after repair:")
        for name in report.still_broken:
            log("  - %s" % name)
    if report.residual:
        log("")
        log("Residual stale bytes (informational): %s" % ", ".join(report.residual))

    return report


def _probe_env() -> dict:
    """Environment for running a launcher as a smoke test.

    The child's stdio encoding is pinned to UTF-8.  pip prints its own location
    -- ``pip 24.2 from <venv>\\Lib\\site-packages\\pip`` -- and on a machine whose
    ANSI code page cannot represent the venv path (cp1252 with a non-ASCII
    directory, say) Python raises ``UnicodeEncodeError`` while writing that line
    to a pipe.  That is a console limitation, not a broken launcher, and
    reporting it as a failed repair would send people off to rebuild a
    perfectly good environment.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def verify(venv: str, log=print) -> bool:
    """Smoke-test the repaired launchers by actually running them."""
    venv = normalize(venv)
    scripts = os.path.join(venv, "Scripts")
    targets = [n for n in ("pip.exe", "pip3.exe", "jupyter.exe",
                           "jupyter-lab.exe", "wheel.exe")
               if os.path.isfile(os.path.join(scripts, n))]
    if not targets:
        log("No launcher found to verify (no pip.exe in Scripts).")
        return True
    ok = True
    for name in targets:
        exe = os.path.join(scripts, name)
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True,
                                  timeout=120, env=_probe_env())
        except (OSError, subprocess.SubprocessError) as exc:
            log("  [FAIL] %-20s %s" % (name, exc))
            ok = False
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            first = proc.stdout.decode("utf-8", "replace").splitlines()[0]
            log("  [ ok ] %-20s %s" % (name, first.strip()))
        else:
            log("  [FAIL] %-20s exit=%s stderr=%s"
                % (name, proc.returncode,
                   proc.stderr.decode("utf-8", "replace").strip()[:120]))
            ok = False
    return ok


class NotAVenv(Exception):
    def __init__(self, path: str) -> None:
        super().__init__("not a venv (no pyvenv.cfg): %s" % path)
        self.path = path


class AmbiguousPath(Exception):
    def __init__(self, candidates: list[str]) -> None:
        super().__init__("ambiguous stale path")
        self.candidates = candidates


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="venv-repair",
        description="Repair a Windows venv whose folder was renamed or moved.",
        epilog="Example: venv-repair E:\\jupyter\\venv --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("venv", help="the venv directory (at its current path)")
    parser.add_argument("--old", metavar="PATH",
                        help="the path the venv used to live at "
                             "(auto-detected when omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip copying Scripts to <venv>_Scripts_backup")
    parser.add_argument("--verify", action="store_true",
                        help="after repairing, actually run pip.exe --version")
    parser.add_argument("--quiet", action="store_true", help="only report errors")
    parser.add_argument("--version", action="version",
                        version="venv-repair %s" % __version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    # A non-ASCII venv path must never crash the tool just because the console
    # code page cannot represent it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # pragma: no cover - stream may not support it
            pass

    args = build_parser().parse_args(argv)

    if os.name != "nt":
        print("warning: this tool targets Windows venvs; proceeding anyway.",
              file=sys.stderr)

    def log(*a, **k):
        if not args.quiet:
            print(*a, **k)

    try:
        report = repair(normalize(args.venv), old=args.old,
                        dry_run=args.dry_run, backup=not args.no_backup, log=log)
    except NotAVenv as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except AmbiguousPath as exc:
        print("error: several stale paths found in this venv:", file=sys.stderr)
        for cand in exc.candidates:
            print("  - %s" % cand, file=sys.stderr)
        print("Pass the right one with --old \"<path>\".", file=sys.stderr)
        return 3

    if args.dry_run:
        print("\ndry run: no file was modified.")

    if args.verify and not args.dry_run:
        print("\nverifying:")
        if not verify(args.venv, log=log):
            return 1

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
