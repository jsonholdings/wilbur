"""code_intel: symbols, definition and references without reading whole files
(BACKLOG `tool-code-intel`, Python first).

Reading a whole file to find one function is the main source of extra rounds
(M2: 7.9 rounds against the 6.5 target). This tool answers the three common
questions from the standard-library `ast` module, so it needs no language
server, no third-party package and no network:

- symbols(path): an outline of classes, functions and methods with line spans;
- definition(name): where a class/function/method or module-level name is
  defined, across the project's .py files;
- references(name): lines that use the name (identifiers and attributes, not
  strings or comments).

A file that does not parse is named as skipped, never silently dropped. The
walk reuses search.bounded_glob, so SKIP_DIRS, other mounts and the time
budget apply exactly as for glob. C++ (clangd) and GDScript come later.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolResult
from .search import bounded_glob

MAX_RESULTS = 60
MAX_FILE_BYTES = 2_000_000


def _parse(path: Path) -> ast.Module | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return ast.parse(path.read_text(errors="replace"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None


def outline(tree: ast.Module) -> list[tuple[int, int, str, str]]:
    """(start, end, kind, qualified name) for every class and function, in
    source order; methods are qualified with their class."""
    rows: list[tuple[int, int, str, str]] = []

    def visit(nodes: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                rows.append((node.lineno, node.end_lineno or node.lineno, "class",
                             prefix + node.name))
                visit(node.body, prefix + node.name + ".", True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "def"
                if isinstance(node, ast.AsyncFunctionDef):
                    kind = "async " + kind
                rows.append((node.lineno, node.end_lineno or node.lineno, kind,
                             prefix + node.name))
                visit(node.body, prefix + node.name + ".", False)

    visit(tree.body, "", False)
    return rows


def _module_assignments(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    found.append((node.lineno, sub.id))
    return found


def definitions(tree: ast.Module, name: str) -> list[tuple[int, str]]:
    """(line, description) for each place `name` is defined. `name` may be
    bare (`run`) or qualified (`Agent.run`)."""
    hits = [(start, f"{kind} {qual}") for start, _end, kind, qual in outline(tree)
            if qual == name or qual.endswith("." + name)]
    if "." not in name:
        hits += [(line, f"assignment {target}") for line, target in _module_assignments(tree)
                 if target == name]
    return sorted(hits)


def references(tree: ast.Module, name: str) -> list[int]:
    """Lines where the bare name is used as an identifier, attribute, import,
    definition or keyword argument."""
    bare = name.rsplit(".", 1)[-1]
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == bare:
            lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == bare:
            lines.add(node.end_lineno or node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == bare:
            lines.add(node.lineno)
        elif isinstance(node, ast.alias) and bare in (node.name.rsplit(".", 1)[-1], node.asname):
            lines.add(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == bare:
            lines.add(node.lineno)
        elif isinstance(node, ast.arg) and node.arg == bare:
            lines.add(node.lineno)
    return sorted(lines)


class CodeIntel:
    name = "code_intel"
    read_only = True
    description = (
        "Python code navigation without reading whole files. action=symbols with path: outline "
        "of classes/functions with line spans. action=definition or references with name "
        "(e.g. 'run' or 'Agent.run'), optional path to limit the search: file:line hits. "
        "Then read_file only the lines you need."
    )
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["symbols", "definition", "references"]},
            "name": {"type": "string", "description": "Symbol name for definition/references"},
            "path": {"type": "string", "description": "File (symbols) or file/dir to search"},
        },
        "required": ["action"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        raw = args.get("path") or ""
        base = Path(raw).expanduser() if raw else Path(ctx.cwd)
        if not base.is_absolute():
            base = Path(ctx.cwd) / base
        if not base.exists():
            return ToolResult(f"No such file or directory: {raw}", is_error=True)
        if action == "symbols":
            return self._symbols(base, raw)
        name = (args.get("name") or "").strip()
        if action not in ("definition", "references"):
            return ToolResult("action must be symbols, definition or references", is_error=True)
        if not name:
            return ToolResult(f"{action} needs a name", is_error=True)
        return self._search(action, name, base, Path(ctx.cwd))

    def _symbols(self, path: Path, raw: str) -> ToolResult:
        if not path.is_file():
            return ToolResult("symbols needs a file path", is_error=True)
        if path.suffix != ".py":
            return ToolResult(f"{raw}: code_intel supports Python (.py) files only for now",
                              is_error=True)
        tree = _parse(path)
        if tree is None:
            return ToolResult(f"{raw}: skipped, the file does not parse as Python", is_error=True)
        rows = outline(tree)
        if not rows:
            return ToolResult(f"{raw}: no classes or functions")
        depth = {qual: qual.count(".") for _s, _e, _k, qual in rows}
        lines = [f"{'  ' * depth[qual]}{start}-{end} {kind} {qual.rsplit('.', 1)[-1]}"
                 for start, end, kind, qual in rows]
        return ToolResult("\n".join(lines))

    def _search(self, action: str, name: str, base: Path, cwd: Path) -> ToolResult:
        if base.is_file():
            files, cut = [base], ""
        else:
            files, cut = bounded_glob(base, "**/*.py")
        out: list[str] = []
        skipped: list[str] = []
        for path in sorted(files):
            tree = _parse(path)
            try:
                shown = str(path.relative_to(cwd))
            except ValueError:
                shown = str(path)
            if tree is None:
                skipped.append(shown)
                continue
            if action == "definition":
                out += [f"{shown}:{line}: {what}" for line, what in definitions(tree, name)]
            else:
                source = path.read_text(errors="replace").splitlines()
                out += [f"{shown}:{line}: {source[line - 1].strip()[:160]}"
                        for line in references(tree, name) if line <= len(source)]
        total = len(out)
        text = "\n".join(out[:MAX_RESULTS]) if out else f"No {action} found for {name!r}."
        if total > MAX_RESULTS:
            text += f"\n... {total - MAX_RESULTS} more; pass path to narrow the search"
        if skipped:
            text += f"\nskipped (does not parse): {', '.join(skipped[:10])}"
        if cut:
            text += f"\n(search cut short: {cut})"
        return ToolResult(text)
