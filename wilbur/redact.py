"""Redact secrets from a copy of session data before it is written to disk.

The live messages are never touched: callers get a deep copy back.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

MARKER = "<REDACTED>"

# Secret-looking values by shape. Group 1 (when present) is kept; the rest
# of the match is replaced by the marker.
_PATTERNS = [
    re.compile(r"(?i)(\b(?:password|passwd|pwd|passphrase|psk|secret|token|api[_-]?key|access[_-]?key)\b[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+"),
    re.compile(r"(?i)(\bAuthorization:\s*(?:Bearer|Basic|token)\s+)[^\s\"']+"),
    # nmcli-style `password <x>` only on an nmcli line, so prose such as
    # "password reset" is not redacted (2.60.0 verifier).
    re.compile(r"(?i)(\bnmcli\b[^\n]*?\b(?:wifi-sec\.psk|password)\s+[\"']?)(?!<REDACTED>)[^\s\"'=:]+"),
    re.compile(r"()\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"()\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"()\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"()\bAKIA[0-9A-Z]{16}\b"),
]


def _secret_files() -> list[str]:
    files: list[str] = []
    env = os.environ.get("WILBUR_SECRET_FILES", "")
    files += [f for f in env.split(os.pathsep) if f]
    try:
        from .config import Config
        files += list(Config.load().secret_files or [])
    except Exception:  # noqa: BLE001 -- a bad config must never block a save
        pass
    return files


def secret_values() -> list[str]:
    values: set[str] = set()
    for name in _secret_files():
        try:
            text = Path(name).expanduser().read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if len(line) >= 4:
                values.add(line)
                # "KEY=value" / "key: value" lines: the value alone is secret too
                m = re.match(r"^[^=:\s]+\s*[=:]\s*(.+)$", line)
                if m and len(m.group(1).strip().strip("\"'")) >= 4:
                    values.add(m.group(1).strip().strip("\"'"))
    return sorted(values, key=len, reverse=True)


def redact_text(text: str, values: list[str]) -> str:
    for v in values:
        text = text.replace(v, MARKER)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + MARKER if m.group(0)[len(m.group(1)):] != MARKER else m.group(0), text)
    return text


def _walk(obj: Any, values: list[str]) -> Any:
    if isinstance(obj, str):
        return redact_text(obj, values)
    if isinstance(obj, dict):
        return {k: _walk(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, values) for v in obj]
    return obj


def redact(obj: Any) -> Any:
    """Return a redacted deep copy of `obj`; `obj` itself is unchanged."""
    return _walk(copy.deepcopy(obj), secret_values())
