"""apply_patch: one call changes several files, all or nothing.

BACKLOG tool-multi-patch. Input is either a unified diff (`patch`) or a list
of exact-string edits (`edits`). Every hunk is validated against the files in
memory first; nothing touches disk until all of them apply. The commit phase
writes each file through a temp file + os.replace, and on any failure puts
every already-committed file back the way it was, so a refactor is never left
half-applied.

Path rules are stricter than edit_file's: a path outside the working
directory is refused outright (edit_file asks; a multi-file patch that
reaches outside the project is far more likely a wrong base path than intent).
Existing files must have been read first and be unchanged since, as for
edit_file. One approval covers the whole patch and carries every path.
"""
from __future__ import annotations

import os
import re
import stat
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diagnostics
from .base import Approval, ToolContext, ToolResult, denial_message, outside_cwd

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    """A patch that cannot apply; the message names the file and hunk."""


@dataclass
class Hunk:
    number: int
    old_start: int
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    ops: list[tuple[str, str]] = field(default_factory=list)   # (' '|'-'|'+', text)


@dataclass
class FilePatch:
    path: str
    create: bool = False
    delete: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    no_newline_at_end: bool = False


def _strip_prefix(raw: str) -> str:
    name = raw.strip().split("\t", 1)[0]
    if name.startswith(("a/", "b/")):
        name = name[2:]
    return name


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Parse a (git-style or plain) unified diff into per-file patches.

    Hunk bodies are read up to the next hunk or file header rather than by
    the header's line counts: local models often get those counts wrong while
    the body itself is right.
    """
    # A blank line or two after a pasted diff is not context.
    lines = text.rstrip("\n").splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        is_file_header = (line.startswith("--- ") and i + 1 < len(lines)
                          and lines[i + 1].startswith("+++ "))
        if is_file_header:
            old_name, new_name = line[4:].strip(), lines[i + 1][4:].strip()
            create = old_name.split("\t", 1)[0] == "/dev/null"
            delete = new_name.split("\t", 1)[0] == "/dev/null"
            path = _strip_prefix(old_name if delete else new_name)
            current = FilePatch(path=path, create=create, delete=delete)
            patches.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("diff --git ") or line.startswith(("index ", "new file mode",
                                                               "deleted file mode",
                                                               "similarity index")):
            hunk = None
            i += 1
            continue
        match = HUNK_HEADER.match(line)
        if match:
            if current is None:
                raise PatchError(f"hunk header before any '--- / +++' file header: {line!r}")
            hunk = Hunk(number=len(current.hunks) + 1, old_start=int(match.group(1)))
            current.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None and current is not None:
            if line.startswith("\\"):
                # "\ No newline at end of file" after a + line marks the new end.
                prev = lines[i - 1] if i else ""
                if prev.startswith("+") or (prev.startswith(" ") and current.create):
                    current.no_newline_at_end = True
            elif line.startswith("+"):
                hunk.new_lines.append(line[1:])
                hunk.ops.append(("+", line[1:]))
            elif line.startswith("-"):
                hunk.old_lines.append(line[1:])
                hunk.ops.append(("-", line[1:]))
            elif line.startswith(" ") or line == "":
                hunk.old_lines.append(line[1:])
                hunk.new_lines.append(line[1:])
                hunk.ops.append((" ", line[1:]))
            # anything else between hunks is commentary; ignore it
        i += 1
    if not patches:
        raise PatchError("no file headers ('--- a/path' then '+++ b/path') found in the patch")
    return patches


def _norm(line: str) -> str:
    return " ".join(line.split())


def _find_block(lines: list[str], block: list[str], hint: int) -> tuple[int, bool]:
    """Index where `block` starts in `lines`, plus whether the match was
    whitespace-tolerant. Exact matches win; a tolerant match is used only when
    no exact one exists. Either kind must be unique, except that among several
    exact matches the one at the header's line number is taken."""
    n = len(block)
    span = range(len(lines) - n + 1)
    exact = [s for s in span if lines[s:s + n] == block]
    if len(exact) == 1:
        return exact[0], False
    if len(exact) > 1:
        if hint in exact:
            return hint, False
        raise PatchError(f"context matches {len(exact)} places (lines "
                         f"{', '.join(str(s + 1) for s in exact[:5])}); add more context lines")
    wanted = [_norm(b) for b in block]
    normalised = [_norm(x) for x in lines]
    loose = [s for s in span if normalised[s:s + n] == wanted]
    if len(loose) == 1:
        return loose[0], True
    if len(loose) > 1:
        raise PatchError(f"context matches {len(loose)} places only when whitespace is "
                         "ignored; add more context lines")
    raise PatchError("context/removed lines not found in the file"
                     + (f" (first expected line: {block[0]!r})" if block else ""))


def _split(text: str) -> tuple[list[str], str, bool]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    return text.splitlines(), newline, trailing


def apply_hunks(text: str, fp: FilePatch) -> str:
    lines, newline, trailing = _split(text)
    for h in fp.hunks:
        where = f"{fp.path} hunk {h.number} (@@ -{h.old_start})"
        try:
            if not h.old_lines:
                start = 0 if fp.create else min(max(h.old_start, 0), len(lines))
            else:
                start, _ = _find_block(lines, h.old_lines, max(h.old_start - 1, 0))
        except PatchError as exc:
            raise PatchError(f"{where}: {exc}") from None
        # Context lines keep the file's own text: after a whitespace-tolerant
        # match the model's spacing must not leak into lines it did not change.
        region = iter(lines[start:start + len(h.old_lines)])
        replacement = []
        for kind, text_line in h.ops:
            if kind == "+":
                replacement.append(text_line)
            elif kind == " ":
                replacement.append(next(region))
            else:
                next(region)
        lines[start:start + len(h.old_lines)] = replacement
    if fp.create:
        trailing = not fp.no_newline_at_end
    elif fp.no_newline_at_end:
        trailing = False
    out = newline.join(lines)
    return out + newline if trailing and lines else out


def apply_edit(text: str, edit: dict[str, Any], number: int) -> str:
    path = edit.get("path", "?")
    old = edit.get("old_string", "")
    new = edit.get("new_string", "")
    where = f"{path} edit {number}"
    if old == "":
        raise PatchError(f"{where}: old_string is empty (use content to create a file)")
    if old == new:
        raise PatchError(f"{where}: old_string and new_string are identical")
    count = text.count(old)
    if count == 0:
        raise PatchError(f"{where}: old_string not found")
    if count > 1 and not edit.get("replace_all"):
        raise PatchError(f"{where}: old_string appears {count} times; add context or "
                         "pass replace_all")
    return text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)


@dataclass
class Planned:
    path: Path
    shown: str
    before: str | None       # None = did not exist
    after: str | None        # None = delete
    mode: int | None = None


def _atomic_write(path: Path, content: str, mode: int | None) -> None:
    """Temp file in the same directory, fsync, then os.replace onto the target.
    A new file is opened 0666 so the process umask applies, as for write_file
    (mkstemp would leave it 0600; os.umask is process-wide, not thread-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.patch-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, stat.S_IMODE(mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit(plan: list[Planned]) -> None:
    """Write every planned change; on any failure restore what was written."""
    done: list[Planned] = []
    try:
        for item in plan:
            if item.after is None:
                item.path.unlink()
            else:
                _atomic_write(item.path, item.after, item.mode)
            done.append(item)
    except BaseException:
        for item in reversed(done):
            try:
                if item.before is None:
                    item.path.unlink(missing_ok=True)
                else:
                    _atomic_write(item.path, item.before, item.mode)
            except OSError:
                pass
        raise


class ApplyPatch:
    name = "apply_patch"
    read_only = False
    description = (
        "Change several files in one all-or-nothing call. Give either patch (a unified diff "
        "with '--- a/path' / '+++ b/path' headers and @@ hunks; /dev/null creates or deletes a "
        "file) or edits (a list of {path, old_string, new_string[, replace_all]}, "
        "{path, content} to create, or {path, delete: true}). Every hunk is checked first; if "
        "any fails, no file changes. Read existing files first."
    )
    schema = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "Unified diff across one or more files"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                        "content": {"type": "string"},
                        "delete": {"type": "boolean"},
                    },
                    "required": ["path"],
                },
            },
        },
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            plan = self._plan(args, ctx)
        except PatchError as exc:
            return ToolResult(f"Patch not applied; no file was changed. {exc}", is_error=True)
        summary = ", ".join(
            f"{'create' if p.before is None else 'delete' if p.after is None else 'edit'} {p.shown}"
            for p in plan)
        outcome = ctx.approve("apply_patch", f"patch {len(plan)} file(s): {summary}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("patch", outcome), is_error=True)
        try:
            commit(plan)
        except OSError as exc:
            return ToolResult(f"Patch not applied; writing failed ({exc}) and every file was "
                              "restored to its state before the call.", is_error=True)
        touched = []
        for item in plan:
            if item.after is None:
                ctx.read_files.pop(str(item.path), None)
            else:
                ctx.read_files[str(item.path)] = item.path.stat().st_mtime
                touched.append(item.path)
        message = diagnostics.annotate(f"Applied patch to {len(plan)} file(s): {summary}.",
                                       touched, str(ctx.cwd), ctx.config)
        return ToolResult(message)

    # ------------------------------------------------------------------ #

    def _target(self, raw: str, ctx: ToolContext) -> Path:
        if not raw:
            raise PatchError("an entry has no path")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(ctx.cwd) / p
        if outside_cwd(p, ctx.cwd):
            raise PatchError(f"{raw}: outside the working directory {ctx.cwd}; apply_patch "
                             "only changes files inside the project")
        return p

    def _existing(self, path: Path, raw: str, ctx: ToolContext) -> tuple[str, int]:
        if not path.is_file():
            raise PatchError(f"{raw}: file does not exist")
        if str(path) not in ctx.read_files:
            raise PatchError(f"{raw}: read it before patching it")
        try:
            if path.stat().st_mtime != ctx.read_files[str(path)]:
                raise PatchError(f"{raw}: changed on disk since you read it; re-read it")
            return path.read_text(errors="replace"), path.stat().st_mode
        except OSError as exc:
            raise PatchError(f"{raw}: cannot read ({exc})") from None

    def _plan(self, args: dict[str, Any], ctx: ToolContext) -> list[Planned]:
        patch, edits = args.get("patch"), args.get("edits")
        if bool(patch) == bool(edits):
            raise PatchError("give exactly one of `patch` (unified diff) or `edits` (list)")
        by_path: dict[Path, Planned] = {}
        order: list[Path] = []

        def state(raw: str) -> tuple[Path, Planned | None]:
            path = self._target(raw, ctx)
            return path, by_path.get(path)

        def put(item: Planned) -> None:
            if item.path not in by_path:
                order.append(item.path)
            by_path[item.path] = item

        if patch:
            for fp in parse_unified_diff(patch):
                path, prior = state(fp.path)
                if fp.create:
                    if prior is not None or path.exists():
                        raise PatchError(f"{fp.path}: create hunk but the file already exists")
                    put(Planned(path, fp.path, None, apply_hunks("", fp)))
                    continue
                if prior is not None:
                    if prior.after is None:
                        raise PatchError(f"{fp.path}: patched after being deleted")
                    before, current, mode = prior.before, prior.after, prior.mode
                else:
                    current, mode = self._existing(path, fp.path, ctx)
                    before = current
                if fp.delete:
                    if fp.hunks:
                        apply_hunks(current, fp)   # validates the removed lines match
                    put(Planned(path, fp.path, before, None, mode))
                    continue
                if not fp.hunks:
                    raise PatchError(f"{fp.path}: file header with no @@ hunks")
                put(Planned(path, fp.path, before, apply_hunks(current, fp), mode))
        else:
            if not isinstance(edits, list):
                raise PatchError("`edits` must be a list")
            for number, edit in enumerate(edits, 1):
                if not isinstance(edit, dict):
                    raise PatchError(f"edit {number} is not an object")
                raw = str(edit.get("path", ""))
                path, prior = state(raw)
                if "content" in edit and "old_string" not in edit:
                    if prior is not None or path.exists():
                        raise PatchError(f"{raw} edit {number}: content creates a file, but "
                                         "it already exists; use old_string/new_string")
                    put(Planned(path, raw, None, str(edit["content"])))
                    continue
                if prior is not None:
                    if prior.after is None:
                        raise PatchError(f"{raw} edit {number}: edited after being deleted")
                    before, current, mode = prior.before, prior.after, prior.mode
                else:
                    current, mode = self._existing(path, raw, ctx)
                    before = current
                if edit.get("delete"):
                    put(Planned(path, raw, before, None, mode))
                    continue
                put(Planned(path, raw, before, apply_edit(current, edit, number), mode))
        plan = [by_path[p] for p in order]
        return [p for p in plan if not (p.before is None and p.after is None)]
