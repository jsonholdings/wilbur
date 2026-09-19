"""Run the project's own check after a write, and hand the model the result.

A frontier model mostly remembers to verify its work. A 3B-active model mostly
does not -- it edits a file, says the change is made, and moves on. That is not
a reasoning failure that a better prompt fixes reliably; it is a habit the
harness can simply perform instead.

So: after a write lands, the harness runs the project's own check and feeds the
output back as a tool result. The model does not have to remember, and cannot
claim a change works when the command says otherwise.

Deliberately conservative:
- Only runs a command the project itself defines. Never invents one.
- Never runs anything on a repository with no detectable check.
- Bounded by a short timeout; a hanging test suite must not hang the agent.
- Reports UNKNOWN rather than OK when it could not run. A check that cannot run
  is not a passing check.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 180


@dataclass
class Check:
    label: str
    cmd: list[str]


def detect(cwd: str) -> Check | None:
    """Find the project's own check command, or None.

    Order matters: a repo with both a Makefile `test` target and a pytest suite
    almost always wants the Makefile, because that is what its author wired up.
    """
    root = Path(cwd)

    makefile = root / "Makefile"
    if makefile.is_file() and shutil.which("make"):
        try:
            text = makefile.read_text(errors="replace")
        except OSError:
            text = ""
        for target in ("check", "test"):
            if any(line.startswith(f"{target}:") for line in text.splitlines()):
                return Check(f"make {target}", ["make", target])

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        for script in ("test", "check", "lint"):
            if script in scripts and shutil.which("npm"):
                # --silent keeps npm's banner out of a bounded tool result.
                return Check(f"npm run {script}", ["npm", "run", "--silent", script])

    if (root / "pyproject.toml").is_file() or (root / "tests").is_dir():
        venv_py = root / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.is_file() else (shutil.which("python3") or "")
        if py and (root / "tests").is_dir():
            # -x: the first failure is the one worth reading, and a full run of a
            # broken suite is mostly noise in a 64K window.
            return Check("pytest", [py, "-m", "pytest", "tests", "-q", "-x"])

    if (root / "Cargo.toml").is_file() and shutil.which("cargo"):
        return Check("cargo test", ["cargo", "test", "--quiet"])

    if (root / "go.mod").is_file() and shutil.which("go"):
        return Check("go test", ["go", "test", "./..."])

    return None


def run(check: Check, cwd: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool | None, str]:
    """(passed, output). `passed` is None when the check could not run.

    None is not False. A check that could not execute tells you nothing about
    the code, and reporting it as a failure would train the model to "fix"
    things that were never broken.
    """
    env = dict(os.environ, NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(check.cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"{check.label}: timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return None, f"{check.label}: could not run ({type(exc).__name__}: {exc})"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def summarise(check: Check, passed: bool | None, output: str, limit: int = 2000) -> str:
    """A short, honest line plus the tail of the output when it failed."""
    if passed is None:
        return f"VERIFICATION UNKNOWN -- {output}"
    if passed:
        tail = output.strip().splitlines()[-1:] or [""]
        return f"VERIFICATION PASSED -- {check.label}: {tail[0][:160]}"
    # The end of a failing run is where the assertion is; the head is setup noise.
    body = output[-limit:] if len(output) > limit else output
    return f"VERIFICATION FAILED -- {check.label}\n{body}"
