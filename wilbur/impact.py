"""Test-impact selection from a static import graph (pure Python, `ast`).

Given the files an edit touched, return the test files that import them,
directly or transitively, so `run_tests` can run those first and report a
failure in seconds instead of after the whole suite. Static analysis only:
nothing is imported or executed, so a broken module cannot crash selection.

Resolution is deliberately conservative. A module is known by its dotted path
from the project root (and from `src/` when that layout is used), and a bare
`import helper` also matches a sibling `helper.py` (tests that put their own
directory on sys.path). An import that resolves to nothing in the project is
ignored: stdlib and third-party modules never select tests.
"""
from __future__ import annotations

import ast
import subprocess
from collections import deque
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox",
             ".mypy_cache", ".pytest_cache", "build", "dist", "site-packages"}
MAX_FILES = 5000  # a repo bigger than this falls back to the full suite


def is_test_file(path: Path) -> bool:
    return path.suffix == ".py" and (path.name.startswith("test_")
                                     or path.name.endswith("_test.py"))


def _python_files(root: Path) -> list[Path] | None:
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix == ".py":
                found.append(entry)
                if len(found) > MAX_FILES:
                    return None
    return found


def _module_names(path: Path, root: Path) -> list[str]:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = [".".join(parts)] if parts else []
    if len(parts) > 1 and parts[0] == "src":
        names.append(".".join(parts[1:]))
    return names


def _imported_names(path: Path, root: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return set()
    own = _module_names(path, root)
    package = own[0].split(".")[:-1] if own else []
    if path.name == "__init__.py" and own:
        package = own[0].split(".")
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base:
                names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def build_graph(root: Path) -> dict[Path, set[Path]] | None:
    """file -> project files it imports. None when the repo is too big."""
    files = _python_files(root)
    if files is None:
        return None
    file_set = set(files)
    by_module: dict[str, Path] = {}
    for f in files:
        for name in _module_names(f, root):
            by_module.setdefault(name, f)
    graph: dict[Path, set[Path]] = {}
    for f in files:
        targets: set[Path] = set()
        for name in _imported_names(f, root):
            hit = by_module.get(name)
            if hit is None and "." not in name:
                sibling = f.parent / f"{name}.py"
                hit = sibling if sibling in file_set else None
            if hit is not None and hit != f:
                targets.add(hit)
            # `import a.b.c` also runs a/__init__ and a/b/__init__.
            parts = name.split(".")
            for i in range(1, len(parts)):
                pkg = by_module.get(".".join(parts[:i]))
                if pkg is not None and pkg != f:
                    targets.add(pkg)
        graph[f] = targets
    return graph


def impacted_tests(root: Path, changed: list[Path]) -> list[Path] | None:
    """Test files that cover `changed`, sorted. None = could not compute."""
    root = root.resolve()
    graph = build_graph(root)
    if graph is None:
        return None
    reverse: dict[Path, set[Path]] = {}
    for src, targets in graph.items():
        for t in targets:
            reverse.setdefault(t, set()).add(src)
    seeds = {c.resolve() for c in changed if c.suffix == ".py"}
    seen = set(seeds)
    queue = deque(seeds)
    while queue:
        node = queue.popleft()
        for importer in reverse.get(node, ()):
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return sorted(p for p in seen if p in graph and is_test_file(p))


def changed_files(root: Path) -> list[Path]:
    """Uncommitted changes (tracked and untracked) under `root`, via git.
    Empty when `root` is not a git checkout or git is missing."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "."],
                              cwd=str(root), capture_output=True, text=True, timeout=15)
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=str(root), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or top.returncode != 0:
        return []
    base = Path(top.stdout.strip())
    out: list[Path] = []
    for line in proc.stdout.splitlines():
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        path = base / entry.strip('"')
        if path.exists():
            out.append(path)
    return out
