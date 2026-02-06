import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import shutil


def _have_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _python_has_fts5() -> bool:
    try:
        p = subprocess.run(
            ["python3", "-c", "import sqlite3; con=sqlite3.connect(':memory:'); con.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); con.close()"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return p.returncode == 0
    except Exception:
        return False


class TestInstallResumeV2Script(unittest.TestCase):
    def test_install_script_works_on_system_bash(self):
        install = Path(__file__).resolve().parents[1] / "bin" / "install-resumev2.sh"
        system_bash = Path("/bin/bash")

        if not system_bash.exists():
            self.skipTest("/bin/bash not present")
        if not _have_cmd("python3"):
            self.skipTest("python3 not present")
        if not _python_has_fts5():
            self.skipTest("python3 sqlite3 missing FTS5")
        if not (_have_cmd("curl") or _have_cmd("wget")):
            self.skipTest("need curl or wget to run install script")

        repo_root = str(Path(__file__).resolve().parents[1])
        repo_raw_base = f"file://{repo_root}"

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        home = Path(td.name)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["SHELL"] = "/bin/zsh"
        env["REPO_RAW_BASE"] = repo_raw_base

        p = subprocess.run([str(system_bash), str(install)], env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            self.fail(f"install script failed (exit {p.returncode}). stderr:\n{p.stderr}\nstdout:\n{p.stdout}")

        zshrc = home / ".zshrc"
        self.assertTrue(zshrc.exists(), "expected .zshrc to be created")
        txt = zshrc.read_text(encoding="utf-8", errors="replace")
        self.assertIn("# >>> codex-sessions resumev2 >>>", txt)
        self.assertIn("# <<< codex-sessions resumev2 <<<", txt)

        installed_py = home / ".codex-user" / "codex_sessions.py"
        self.assertTrue(installed_py.exists(), "expected codex_sessions.py to be installed")


if __name__ == "__main__":
    unittest.main()
