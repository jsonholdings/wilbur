"""Run this project's test suite and return a compact, structured result.

Runs pytest (preferred), npm test, or make test depending on what markers
exist in ctx.cwd, parses the result into pass/fail counts and per-failure
file:line detail where the runner supports it (pytest via JUnit XML), and
clamps the output the same way Bash does so one run cannot fill the window.
"""
from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import contained
from .. import impact
from .base import Approval, ToolContext, ToolResult, clamp, denial_message


def _detect_pytest(root: Path, target: Path) -> bool:
    if (root / "pytest.ini").exists():
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(errors="replace")
        except OSError:
            text = ""
        if "[tool.pytest.ini_options]" in text:
            return True
    tests_dir = target if target.is_dir() else root
    for candidate in (tests_dir, tests_dir / "tests"):
        if candidate.is_dir():
            for pattern in ("test_*.py", "*_test.py"):
                if any(candidate.rglob(pattern)):
                    return True
    return False


def _detect_npm(root: Path) -> bool:
    pkg = root / "package.json"
    if not pkg.exists():
        return False
    try:
        import json
        data = json.loads(pkg.read_text(errors="replace"))
    except (OSError, ValueError):
        return False
    return bool((data.get("scripts") or {}).get("test"))


def _detect_make(root: Path) -> bool:
    makefile = root / "Makefile"
    if not makefile.exists():
        return False
    try:
        text = makefile.read_text(errors="replace")
    except OSError:
        return False
    return bool(re.search(r"^test\s*:", text, re.MULTILINE))


def _first_line(text: str) -> str:
    text = (text or "").strip()
    return text.splitlines()[0] if text else ""


def _location_from_message(text: str) -> tuple[str, str]:
    """Best-effort file:line extraction from a JUnit failure/error message
    when the testcase element itself carries none."""
    match = re.search(r'([\w./\\-]+\.py):(\d+)', text or "")
    if match:
        return match.group(1), match.group(2)
    return "", ""


def _parse_junit(xml_path: Path) -> dict:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))

    total = passed = failed = errors = skipped = 0
    failures: list[dict] = []

    for suite in suites:
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))

    passed = total - failed - errors - skipped

    for testcase in root.iter("testcase"):
        node = testcase.find("failure")
        if node is None:
            node = testcase.find("error")
        if node is None:
            continue
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        file_attr = testcase.get("file", "")
        line_attr = testcase.get("line", "")
        message = node.get("message", "") or _first_line(node.text or "")
        if not file_attr or not line_attr:
            guess_file, guess_line = _location_from_message((node.text or "") + " " + message)
            file_attr = file_attr or guess_file
            line_attr = line_attr or guess_line
        failures.append({
            "classname": classname,
            "name": name,
            "file": file_attr,
            "line": line_attr,
            "message": _first_line(message),
        })

    return {
        "total": total, "passed": passed, "failed": failed,
        "errors": errors, "skipped": skipped, "failures": failures,
    }


def _format_pytest_summary(parsed: dict, exit_code: int) -> str:
    lines = [
        f"tests: {parsed['total']} total, {parsed['passed']} passed, "
        f"{parsed['failed']} failed, {parsed['errors']} errors, "
        f"{parsed['skipped']} skipped (pytest)",
        f"exit {exit_code}",
    ]
    if parsed["failures"]:
        lines.append("")
        lines.append("FAILURES:")
        for f in parsed["failures"]:
            loc = f"{f['file']}:{f['line']}" if f["file"] else "(unknown location)"
            test_id = f"{f['classname']}::{f['name']}" if f["classname"] else f["name"]
            lines.append(f"- {loc} {test_id}")
            if f["message"]:
                lines.append(f"  {f['message']}")
    return "\n".join(lines)


def _format_custom_summary(framework: str, body: str, exit_code: int, workdir: str = "") -> str:
    passing = 0
    failing = 0
    for pat in (r'(\d+)\s+passing', r'(\d+)\s+tests?\s+passed'):
        m = re.search(pat, body)
        if m:
            passing = int(m.group(1))
            break
    for pat in (r'(\d+)\s+failing', r'(\d+)\s+tests?\s+failed'):
        m = re.search(pat, body)
        if m:
            failing = int(m.group(1))
            break
    if exit_code != 0 and passing == 0 and failing == 0:
        # 2.52.1: "0 passed, 0 failed" read as a clean run when the command never ran a test
        # (wrong cwd -> relative ./venv resolved elsewhere -> "No module named pytest").
        header = (f"tests: DID NOT RUN ({framework}) -- the command exited {exit_code} "
                  f"without reporting any test counts; ran in {workdir or '(cwd)'}. "
                  f"Relative paths resolve from there: pass `cwd` or absolute paths")
    else:
        header = (f"tests: {passing} passed, {failing} failed ({framework}) -- "
                  f"per-test file:line detail is not available for this runner")
    return header + f"\nexit {exit_code}\n\n" + body


class RunTests:
    name = "run_tests"
    read_only = False
    description = (
        "Run this project's test suite (auto-detects pytest/npm/make, or pass "
        "an explicit command) and return a compact pass/fail summary with "
        "file:line for each failure when available. After several edits, "
        "run it once for the batch, not after each edit."
    )
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "Explicit test command override"},
            "path": {"type": "string",
                     "description": "Subdirectory or target to test, default cwd"},
            "cwd": {"type": "string",
                    "description": "Directory to run the command in (absolute, or relative to the "
                                   "session cwd); default the session cwd. A `cd` in an earlier "
                                   "run_bash does not carry over"},
            "timeout": {"type": "integer", "description": "Seconds, default 120"},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session_root = Path(ctx.cwd)
        root = session_root
        if args.get("cwd"):
            root = Path(str(args["cwd"])).expanduser()
            if not root.is_absolute():
                root = session_root / root
            if not root.is_dir():
                return ToolResult(f"cwd {root} is not a directory.", is_error=True)
        target = Path(args["path"]).expanduser() if args.get("path") else root
        if not target.is_absolute():
            target = root / target

        command = (args.get("command") or "").strip()
        timeout = int(args.get("timeout", 120))
        framework = "custom"
        junit_path: Path | None = None

        if not command:
            if _detect_pytest(root, target):
                framework = "pytest"
                tmp_dir = tempfile.mkdtemp(prefix="wilbur-junit-")
                junit_path = Path(tmp_dir) / "junit.xml"
                rel_target = ""
                if args.get("path"):
                    rel_target = str(target)
                command = f"python3 -m pytest -q {rel_target} --junitxml={junit_path}".strip()
            elif _detect_npm(root):
                framework = "npm"
                command = "npm test"
            elif _detect_make(root):
                framework = "make"
                command = "make test"
            else:
                return ToolResult(
                    "No test command detected (no pytest.ini/pyproject "
                    "[tool.pytest.ini_options]/tests dir, no package.json "
                    "\"test\" script, no Makefile test: target). Pass "
                    "`command` explicitly.",
                    is_error=True,
                )

        first_files: list[Path] = []
        if (framework == "pytest" and not args.get("path")
                and getattr(ctx.config, "test_impact", True)):
            # `changed` is accepted but kept out of the schema: git's uncommitted
            # files are the default, and every schema token is paid each round
            # (tests/test_compact_round_overflow.py sits on that budget).
            first_files = _impact_selection(root, args.get("changed"))

        label = command[:70]
        shown = command
        if first_files:
            shown = f"{_pytest_files_command(first_files, root)} && {command}"
        outcome = ctx.approve("run_tests", f"{label}\n    $ {shown}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("test run", outcome), is_error=True)

        memory_max = getattr(ctx.config, "tool_memory_max", contained.DEFAULT_MEMORY_MAX)
        impact_note = ""
        if first_files:
            first = _run_first(first_files, root, timeout, memory_max)
            if isinstance(first, ToolResult):
                return first
            first_text, first_ok = first
            if not first_ok:
                text = (f"impact: {len(first_files)} test file(s) covering your changes FAILED "
                        f"(full suite not run yet; fix these, then run_tests again)\n{first_text}")
                return ToolResult(clamp(text.strip(), ctx.config.max_tool_output_chars,
                                        ctx.config.max_tool_output_lines), is_error=True)
            impact_note = (f"impact: {len(first_files)} test file(s) covering your changes "
                           f"passed first; full suite follows\n")
        try:
            proc = contained.shell(command, str(root), timeout, memory_max)
        except contained.Timeout:
            return ToolResult(f"Test run timed out after {timeout}s. {contained.timeout_hint(timeout)}",
                              is_error=True)

        body = (proc.stdout or "") + (proc.stderr or "")
        body += contained.oom_note(proc.returncode, memory_max, body, proc.oom_killed)

        if framework == "pytest" and junit_path is not None:
            try:
                parsed = _parse_junit(junit_path)
                text = _format_pytest_summary(parsed, proc.returncode)
            except (ET.ParseError, OSError):
                text = (f"tests: unable to parse JUnit report (pytest)\n"
                        f"exit {proc.returncode}\n\n{body.strip()}")
            finally:
                try:
                    junit_path.unlink(missing_ok=True)
                    junit_path.parent.rmdir()
                except OSError:
                    pass
        else:
            text = _format_custom_summary(framework, body.strip(), proc.returncode, str(root))

        text = clamp((impact_note + text).strip(), ctx.config.max_tool_output_chars,
                     ctx.config.max_tool_output_lines)
        return ToolResult(text, is_error=proc.returncode != 0)


def _impact_selection(root: Path, changed_arg: Any) -> list[Path]:
    """Test files covering the changed files; [] means run only the full suite."""
    if isinstance(changed_arg, list) and changed_arg:
        changed = [Path(str(c)) if Path(str(c)).is_absolute() else root / str(c)
                   for c in changed_arg]
    else:
        changed = impact.changed_files(root)
    if not changed:
        return []
    return impact.impacted_tests(root, changed) or []


def _pytest_files_command(files: list[Path], root: Path, junit: Path | None = None) -> str:
    rels = []
    for f in files:
        try:
            rels.append(str(f.relative_to(root.resolve())))
        except ValueError:
            rels.append(str(f))
    command = "python3 -m pytest -q " + " ".join(rels)
    return f"{command} --junitxml={junit}" if junit else command


def _run_first(files: list[Path], root: Path, timeout: int,
               memory_max: str) -> tuple[str, bool] | ToolResult:
    tmp_dir = tempfile.mkdtemp(prefix="wilbur-junit-")
    junit = Path(tmp_dir) / "junit.xml"
    try:
        try:
            proc = contained.shell(_pytest_files_command(files, root, junit), str(root),
                                   timeout, memory_max)
        except contained.Timeout:
            return ToolResult(f"Impacted tests timed out after {timeout}s. "
                              f"{contained.timeout_hint(timeout)}", is_error=True)
        try:
            text = _format_pytest_summary(_parse_junit(junit), proc.returncode)
        except (ET.ParseError, OSError):
            body = (proc.stdout or "") + (proc.stderr or "")
            text = f"tests: unable to parse JUnit report (pytest)\nexit {proc.returncode}\n\n{body.strip()}"
        return text, proc.returncode == 0
    finally:
        try:
            junit.unlink(missing_ok=True)
            junit.parent.rmdir()
        except OSError:
            pass
