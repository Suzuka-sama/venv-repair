#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end tests for venv-repair.

Every test builds a *real* virtual environment and then moves it, because the
whole point of the tool is to cope with the exact bytes pip leaves behind --
mocking those bytes would test nothing worth testing.

Run with::

    python -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import venv_repair  # noqa: E402


def scripts_dir(venv: str) -> str:
    return os.path.join(venv, "Scripts")


def pip_exe(venv: str) -> str:
    return os.path.join(scripts_dir(venv), "pip.exe")


def pip_works(venv: str) -> bool:
    """True when the launcher actually starts the interpreter.

    The child gets ``venv_repair._probe_env()`` so the result depends on the
    repair and not on the machine's ANSI code page: pip prints its own location,
    which contains the venv path, and on a cp1252 machine that print cannot
    represent a Chinese directory name.
    """
    try:
        proc = subprocess.run([pip_exe(venv), "--version"],
                              capture_output=True, timeout=120,
                              env=venv_repair._probe_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and b"pip" in proc.stdout


def launcher_sizes(venv: str) -> dict:
    """Sizes of the ``.exe`` launchers only.

    The size guarantee applies to launchers, because their layout is
    ``[padding][shebang][embedded zip]`` and the zip must not move.  Plain text
    scripts such as ``activate`` are *expected* to change length -- one byte per
    byte of path, times the number of times the path appears.
    """
    out = {}
    for name in os.listdir(scripts_dir(venv)):
        if not name.lower().endswith(".exe"):
            continue
        path = os.path.join(scripts_dir(venv), name)
        if os.path.isfile(path):
            out[name] = os.path.getsize(path)
    return out


def read_bytes(path: str) -> bytes:
    """Read a file without leaking the handle (keeps the suite warning-free)."""
    with open(path, "rb") as fh:
        return fh.read()


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


@unittest.skipUnless(os.name == "nt", "venv-repair targets Windows launchers")
class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="venv-repair-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------- #
    def make_venv(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        proc = subprocess.run([sys.executable, "-m", "venv", path],
                              capture_output=True)
        if proc.returncode != 0:
            self.fail("could not create venv: %s"
                      % proc.stderr.decode("utf-8", "replace"))
        return path

    def moved_venv(self, old_name: str, new_name: str) -> str:
        """Create a venv at *old_name*, then rename it to *new_name*.

        This reproduces the broken state: the launchers still hold *old_name*.
        """
        old = self.make_venv(old_name)
        new = os.path.join(self.tmp, new_name)
        os.rename(old, new)
        return new

    @staticmethod
    def strip_command_line(venv: str) -> None:
        """Drop the ``command = ... -m venv ...`` line from pyvenv.cfg.

        Venvs built through the ``venv`` API programmatically do not have it, so
        this forces stale-path detection to go through the launcher shebangs.
        """
        cfg = os.path.join(venv, "pyvenv.cfg")
        with open(cfg, encoding="utf-8") as fh:
            kept = [ln for ln in fh if not ln.lower().startswith("command")]
        with open(cfg, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(kept)

    def assert_broken_before(self, venv: str) -> dict:
        self.assertFalse(pip_works(venv),
                         "expected the moved venv to be broken before repair")
        return launcher_sizes(venv)

    def assert_fixed_after(self, venv: str, sizes_before: dict) -> None:
        if not pip_works(venv):
            self.fail("pip.exe still does not run after repair"
                      + self.describe_launcher(venv))
        sizes_after = launcher_sizes(venv)
        for name, size in sizes_before.items():
            self.assertEqual(
                size, sizes_after.get(name),
                "%s changed size -- the embedded zip offset may have moved"
                % name)

    @staticmethod
    def describe_launcher(venv: str) -> str:
        """Explain *why* a launcher failed, for machines we cannot inspect.

        CI-only failures are otherwise guesswork, so report the shebang, whether
        the path it names exists, and what the launcher itself printed.
        """
        exe = pip_exe(venv)
        if not os.path.isfile(exe):
            return "\n  %s does not exist" % exe
        parsed = venv_repair.read_shebang(read_bytes(exe))
        if parsed is None:
            return "\n  %s has no recognisable shebang" % exe
        target = parsed[2].rstrip(b"\r").decode("utf-8", "replace")
        lines = ["", "  pip.exe shebang : %s" % target,
                 "  target exists   : %s" % os.path.isfile(target)]
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True,
                                  timeout=120, env=venv_repair._probe_env())
            lines.append("  launcher rc     : %s" % proc.returncode)
            lines.append("  launcher stdout : %s"
                         % proc.stdout.decode("utf-8", "replace").strip()[:200])
            # Show the end of stderr too: that is where the exception is.
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            lines.append("  launcher stderr : %s" % " | ".join(tail[-3:])[:400])
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append("  launcher raised : %s" % exc)
        return "\n".join(lines)

    # -- the three length strategies --------------------------------------- #
    def test_same_length_rename(self):
        venv = self.moved_venv("vtestA", "vtestB")
        sizes = self.assert_broken_before(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertTrue(report.ok, report.still_broken)
        self.assert_fixed_after(venv, sizes)

    def test_longer_rename_uses_nul_padding(self):
        venv = self.moved_venv("vtestA", "vtestBBBBBB")
        sizes = self.assert_broken_before(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertEqual(report.unrepairable, [],
                         "padding should have absorbed a 6 byte growth")
        self.assert_fixed_after(venv, sizes)

    def test_shorter_rename_pads_with_spaces(self):
        venv = self.moved_venv("vtestAAAAAAAA", "vtestA")
        sizes = self.assert_broken_before(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertTrue(report.ok, report.still_broken)
        self.assert_fixed_after(venv, sizes)

    # -- the differentiator: non-ASCII paths -------------------------------- #
    def test_non_ascii_path_growing(self):
        """A Chinese venv name makes the path longer in UTF-8 (3 bytes/char).

        The regression this pins down: decoding the shebang with the ANSI code
        page turns '中文目录' into mojibake, so the repair used to write the wrong
        bytes and leave the launcher broken.
        """
        venv = self.moved_venv("vtestA", "中文目录")
        sizes = self.assert_broken_before(venv)

        old, ambiguous = venv_repair.detect_stale_path(venv)
        self.assertEqual(ambiguous, [])
        self.assertEqual(venv_repair.detect_codec(venv, old), "utf-8")

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertEqual(report.unrepairable, [])
        self.assert_fixed_after(venv, sizes)

    def test_non_ascii_path_detected_from_shebang(self):
        """Old path is non-ASCII *and* pyvenv.cfg carries no ``command`` line.

        Venvs created through the ``venv`` API programmatically have no
        ``command`` line, so detection has to fall back to the launcher
        shebangs -- exactly where a wrong codec guess yields mojibake.  The new
        name is shorter, so this also covers the pad-with-spaces strategy.
        """
        venv = self.moved_venv("中文目录中文", "vtestA")
        self.strip_command_line(venv)
        sizes = self.assert_broken_before(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertEqual(report.unrepairable, [])
        self.assertTrue(report.ok, report.still_broken)
        self.assert_fixed_after(venv, sizes)

    # -- safety rails ------------------------------------------------------- #
    def test_dry_run_writes_nothing(self):
        venv = self.moved_venv("vtestA", "vtestB")
        before = launcher_sizes(venv)
        cfg_before = read_bytes(os.path.join(venv, "pyvenv.cfg"))

        report = venv_repair.repair(venv, dry_run=True, backup=False,
                                    log=lambda *a: None)

        self.assertTrue(report.patched, "dry run should still report findings")
        self.assertEqual(before, launcher_sizes(venv))
        self.assertEqual(cfg_before,
                         read_bytes(os.path.join(venv, "pyvenv.cfg")))
        self.assertFalse(pip_works(venv), "dry run must not repair anything")

    def test_healthy_venv_is_left_alone(self):
        venv = self.make_venv("vtestHealthy")
        before = launcher_sizes(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertEqual(report.patched, [])
        self.assertEqual(before, launcher_sizes(venv))
        self.assertTrue(pip_works(venv))

    def test_backup_is_created_and_restorable(self):
        venv = self.moved_venv("vtestA", "vtestB")
        broken_pip = read_bytes(pip_exe(venv))

        report = venv_repair.repair(venv, backup=True, log=lambda *a: None)

        self.assertIsNotNone(report.backup)
        self.assertTrue(os.path.isdir(report.backup))
        restored = os.path.join(report.backup, "pip.exe")
        self.assertEqual(broken_pip, read_bytes(restored))

    def test_pyvenv_cfg_is_rewritten(self):
        venv = self.moved_venv("vtestA", "vtestBBBBBB")
        cfg = os.path.join(venv, "pyvenv.cfg")
        if "vtestA" not in read_text(cfg):
            # Python 3.9's pyvenv.cfg carries no `command` line -- the venv path
            # was only recorded from a later release onwards -- so on 3.9 there
            # is nothing in this file to rewrite.  It is also why stale-path
            # detection has to fall back to the launcher shebangs.
            self.skipTest("this Python's pyvenv.cfg does not record the venv")

        venv_repair.repair(venv, backup=False, log=lambda *a: None)

        text = read_text(cfg)
        self.assertNotIn("vtestA", text)
        self.assertIn("vtestBBBBBB", text)

    # -- error handling ----------------------------------------------------- #
    def test_not_a_venv(self):
        with self.assertRaises(venv_repair.NotAVenv):
            venv_repair.repair(self.tmp, log=lambda *a: None)

    def test_verify_passes_after_repair(self):
        venv = self.moved_venv("vtestA", "vtestB")
        venv_repair.repair(venv, backup=False, log=lambda *a: None)
        self.assertTrue(venv_repair.verify(venv, log=lambda *a: None))

    def test_verify_ignores_the_console_code_page(self):
        """Verification must not depend on the machine's ANSI code page.

        pip prints its own location, which contains the venv path.  On a machine
        whose code page cannot represent that path -- cp1252 against a Chinese
        directory name, which is exactly what a US-English CI runner has -- the
        *print* raises UnicodeEncodeError.  That is a console limitation, not a
        broken launcher, so the probe pins UTF-8 for the child instead of
        reporting a false failure that sends people off to rebuild a good venv.
        """
        venv = self.make_venv("中文目录")

        os.environ["PYTHONIOENCODING"] = "cp1252"
        self.addCleanup(os.environ.pop, "PYTHONIOENCODING", None)

        self.assertTrue(venv_repair.verify(venv, log=lambda *a: None),
                        "verify() failed because of the console encoding")
        self.assertTrue(pip_works(venv))

    # -- 8.3 short names ---------------------------------------------------- #
    @staticmethod
    def short_form(path: str):
        """The 8.3 spelling of *path*, or ``None`` when the volume has none."""
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        short = buf.value if n else path
        if not short or os.path.normcase(short) == os.path.normcase(path):
            return None
        return short

    def short_addressed_dir(self, name: str):
        """A directory whose 8.3 alias differs from its long name."""
        long_dir = os.path.join(self.tmp, name)
        os.makedirs(long_dir, exist_ok=True)
        short = self.short_form(long_dir)
        if short is None:
            self.skipTest("this volume has no 8.3 short names")
        return short

    def test_short_and_long_spelling_of_one_directory(self):
        """Launchers keep the long spelling, ``pyvenv.cfg`` the 8.3-short one.

        pip resolves the interpreter path while the ``venv`` module records
        whatever it was handed, so the two disagree whenever the venv sits under
        a name that has an 8.3 alias -- which is exactly what the GitHub runners
        do, their temp directory being ``C:\\Users\\RUNNER~1\\...``.  Comparing
        those as plain strings made the repair fix only the text files and
        silently skip every ``.exe``, leaving ``pip.exe`` broken.
        """
        short = self.short_addressed_dir("venv-repair-long-name")

        old = os.path.join(short, "vtestA")
        subprocess.run([sys.executable, "-m", "venv", old], check=True,
                       capture_output=True)
        new = os.path.join(short, "vtestB")
        os.rename(old, new)

        sizes = self.assert_broken_before(new)
        report = venv_repair.repair(new, backup=False, log=lambda *a: None)

        self.assertEqual(report.unrepairable, [])
        self.assertTrue(report.ok, report.still_broken)
        self.assert_fixed_after(new, sizes)

    def test_healthy_venv_built_through_the_short_spelling(self):
        """The mirror image: a healthy venv must not look stale.

        With the spelling mismatch, ``pyvenv.cfg`` matched the venv and the
        launchers did not, so the tool fell through to the shebangs and
        "repaired" launchers that had never been broken.
        """
        short = self.short_addressed_dir("venv-repair-long-name-2")

        venv = os.path.join(short, "vtestHealthy")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True,
                       capture_output=True)
        before = launcher_sizes(venv)

        report = venv_repair.repair(venv, backup=False, log=lambda *a: None)

        self.assertEqual(report.patched, [],
                         "a healthy venv must be left alone")
        self.assertEqual(before, launcher_sizes(venv))
        self.assertTrue(pip_works(venv))

    def test_non_ascii_rename_under_a_short_named_parent(self):
        """Both oddities at once, which is what the CI runner actually hits.

        A non-ASCII target makes the path longer in UTF-8, so the shebang has to
        grow into the NUL padding, and the 8.3 parent means the old spelling in
        the launcher differs from the one in ``pyvenv.cfg``.  Together they are
        the combination that made the first CI run fail on every Python version.
        """
        short = self.short_addressed_dir("venv-repair-long-name-3")

        old = os.path.join(short, "vtestA")
        subprocess.run([sys.executable, "-m", "venv", old], check=True,
                       capture_output=True)
        new = os.path.join(short, "中文目录")
        os.rename(old, new)
        # Python 3.9 does not record the venv path in pyvenv.cfg at all, so only
        # assert on its contents where there was something to rewrite.
        cfg_named_the_venv = "vtestA" in read_text(os.path.join(new, "pyvenv.cfg"))

        sizes = self.assert_broken_before(new)
        report = venv_repair.repair(new, backup=False, log=lambda *a: None)

        self.assertEqual(report.unrepairable, [])
        self.assertTrue(report.ok, report.still_broken)
        self.assert_fixed_after(new, sizes)
        # The rewritten config must name the directory itself, never an 8.3
        # alias Windows merely happens to have generated for it.
        if cfg_named_the_venv:
            self.assertIn("中文目录", read_text(os.path.join(new, "pyvenv.cfg")))


class LauncherUnitTests(unittest.TestCase):
    """Byte-level tests for the launcher patcher, with no virtualenv involved.

    These cover the refusal path, which is awkward to reach with a real venv: the
    NUL padding in front of the shebang measures ~660 bytes on CPython 3.9-3.13,
    so a path would have to grow by more than that -- and blow past MAX_PATH --
    before the tool has to say no.
    """

    TAIL = b"\\Scripts\\python.exe"
    OLD = b"E:\\old\\venv"                 # 11 bytes
    NEW = b"E:\\a\\much\\longer\\venv"     # 22 bytes, +11

    @classmethod
    def blob(cls, old=OLD, padding=80, crlf=False, payload=b"PK\x03\x04"):
        line = b"#!" + old + cls.TAIL + (b"\r" if crlf else b"")
        return b"\x00" * padding + line + b"\n" + payload + b"PAYLOAD"

    @classmethod
    def shebang_of(cls, blob):
        return venv_repair.read_shebang(blob)[2]

    def test_read_shebang_finds_the_line_just_before_the_zip(self):
        self.assertEqual(self.shebang_of(self.blob()), self.OLD + self.TAIL)

    def test_read_shebang_tolerates_the_trailing_cr(self):
        """pip.exe has an extra CRLF after the shebang; must not be mistaken."""
        self.assertEqual(self.shebang_of(self.blob(crlf=True)),
                         self.OLD + self.TAIL + b"\r")

    def test_grow_into_padding_keeps_length(self):
        blob = self.blob(padding=80)
        out = venv_repair.patch_launcher(blob, self.OLD, self.NEW, "utf-8")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), len(blob), "file size must not change")
        self.assertEqual(self.shebang_of(out), self.NEW + self.TAIL)

    def test_shrink_pads_with_spaces_and_keeps_length(self):
        old = self.NEW
        blob = self.blob(old=old, padding=80)
        out = venv_repair.patch_launcher(blob, old, self.OLD, "utf-8")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), len(blob), "file size must not change")
        self.assertEqual(self.shebang_of(out).rstrip(b" "), self.OLD + self.TAIL)

    def test_refuses_when_padding_cannot_absorb_the_growth(self):
        blob = self.blob(padding=4)        # only four spare bytes, needs 11
        self.assertIsNone(
            venv_repair.patch_launcher(blob, self.OLD, self.NEW, "utf-8"))

    def test_refuses_a_file_that_is_not_a_launcher(self):
        blob = self.blob(payload=b"definitely-not-a-zip")
        self.assertIsNone(
            venv_repair.patch_launcher(blob, self.OLD, self.NEW, "utf-8"))

    def test_refuses_a_shebang_that_is_not_a_python_interpreter(self):
        blob = b"\x00" * 80 + b"#!" + self.OLD + b"\\bin\\sh" + b"\n" + \
            b"PK\x03\x04PAYLOAD"
        self.assertIsNone(
            venv_repair.patch_launcher(blob, self.OLD, self.NEW, "utf-8"))

    def test_prefix_eq_is_safe_for_multibyte_paths(self):
        """A differing drive-letter case must still match, safely.

        ``bytes.lower()`` is ASCII-only, and a double-byte code page's trail
        bytes cover 0x40-0xFE -- including the ASCII upper-case range -- so
        lowering the raw bytes can corrupt a multi-byte character.
        ``_prefix_eq`` decodes first.

        cp936 is requested explicitly rather than using ``mbcs``: ``mbcs`` is
        whatever code page the machine happens to run, and on a US-English CI
        runner that is cp1252, which has no double-byte characters at all.
        """
        codec = "cp936"
        sample = None
        for cp in range(0x4E00, 0xA000):
            try:
                enc = chr(cp).encode(codec)
            except (UnicodeEncodeError, LookupError):
                continue
            if len(enc) == 2 and 0x41 <= enc[1] <= 0x5A:
                sample = chr(cp)
                break
        self.assertIsNotNone(sample, "no cp936 char with an ASCII trail byte")

        old = ("E:\\" + sample + "\\venv").encode(codec)
        prefix = ("e:\\" + sample + "\\venv").encode(codec)
        self.assertNotEqual(prefix, old)
        self.assertTrue(venv_repair._prefix_eq(prefix, old, codec),
                        "_prefix_eq must compare decoded text, not bytes.lower()")


@unittest.skipUnless(os.name == "nt", "venv-repair targets Windows launchers")
class CliTests(unittest.TestCase):
    """The CLI contract: exit codes and flags."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="venv-repair-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable,
             os.path.join(os.path.dirname(os.path.dirname(
                 os.path.abspath(__file__))), "venv_repair.py"), *args],
            capture_output=True, timeout=300)

    def test_exit_code_2_for_non_venv(self):
        proc = self.run_cli(self.tmp)
        self.assertEqual(proc.returncode, 2)

    def test_exit_code_0_and_dry_run_wording(self):
        old = os.path.join(self.tmp, "vtestA")
        subprocess.run([sys.executable, "-m", "venv", old],
                       check=True, capture_output=True)
        new = os.path.join(self.tmp, "vtestB")
        os.rename(old, new)

        proc = self.run_cli(new, "--no-backup", "--dry-run")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))

        proc = self.run_cli(new, "--no-backup", "--verify")
        self.assertEqual(proc.returncode, 0,
                         proc.stdout.decode("utf-8", "replace") +
                         proc.stderr.decode("utf-8", "replace"))

    def test_version_flag(self):
        proc = self.run_cli("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"venv-repair", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
