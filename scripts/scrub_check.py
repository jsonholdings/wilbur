#!/usr/bin/env python3
"""Fail if anything internal would ship: hostnames, home paths, venture names, secret-shaped strings.

Run before every commit and in CI. `--release` also requires a LICENSE file.
Exit 0 = clean, 1 = findings.

Site-specific terms are NOT hardcoded here: a scrub that hardcodes its own denylist and
exempts itself from its own scan is exactly the kind of leak this exists to catch.
Internal-only terms load from ~/.config/wilbur/scrub-denylist.txt or
$WILBUR_SCRUB_DENYLIST if present; this script itself is scanned like any other file
(no self-exemption).
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", "__pycache__"}

# /home/user/... is the project's own anonymized placeholder (used in test fixtures for
# a generic non-owner path) -- excluded so real leaks aren't lost in a sea of expected
# fixture matches; every other /home/<name> still fires.
BASE_PATTERNS = {
    "home path": r"/home/(?!user\b)[a-z]",
    # Unquoted on purpose (feedback_secret_scan_needs_unquoted_pattern.md): catches a
    # bare key-colon-value line, not only a quoted literal.
    "secret-shaped": r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|access[_-]?token|token)\b"
                      r"\s*[:=]\s*['\"]?(?!<|\$\{|\[REDACTED\]|\*{2,}|\.\.\.|`)[^\s'\"`]{8,}",
}


def load_denylist():
    path = os.environ.get("WILBUR_SCRUB_DENYLIST", os.path.expanduser("~/.config/wilbur/scrub-denylist.txt"))
    p = Path(path)
    if not p.exists():
        return None
    terms = [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not terms:
        return None
    return r"(?i)" + "|".join(re.escape(t) for t in terms)


def main():
    patterns = dict(BASE_PATTERNS)
    denylist = load_denylist()
    if denylist:
        patterns["denylisted term"] = denylist

    findings = []
    for f in ROOT.rglob("*"):
        if not f.is_file() or any(part in SKIP_DIRS for part in f.parts):
            continue
        text = f.read_text(errors="ignore")
        for label, pat in patterns.items():
            for m in re.finditer(pat, text):
                line = text.count("\n", 0, m.start()) + 1
                findings.append(f"{f.relative_to(ROOT)}:{line}: {label}")
    if "--release" in sys.argv and not (ROOT / "LICENSE").exists():
        findings.append("LICENSE missing (owner must choose MIT or Apache-2.0)")
    print("\n".join(findings) if findings else "scrub: clean")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
