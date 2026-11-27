"""U8 (2.65.0): the diff fast path keeps a 2,000-line rewrite under 10 ms; on
2.64.0 a brace-heavy rewrite took 48 s (autojunk=False is quadratic on
repeated lines)."""
from __future__ import annotations

import random
import time

from wilbur import render


def _code(tag: str, n: int = 2000) -> str:
    return "\n".join(["", "    }", f"    {tag}{i}();"][i % 3] for i in range(n))


def _apply(a, b, ops):
    out = []
    for op, i1, i2, j1, j2 in ops:
        out.extend(a[i1:i2] if op == "equal" else b[j1:j2])
    return out


def _best_ms(old, new, reps=5):
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        render.diff_view("a.c", old, new)
        best = min(best, (time.perf_counter() - t) * 1000)
    return best


def test_repeated_line_rewrite_is_fast():
    t = time.perf_counter()
    render.diff_view("a.c", _code("a"), _code("b"))
    assert time.perf_counter() - t < 0.5
    assert _best_ms(_code("a"), _code("b")) < 10


def test_unique_line_rewrite_meets_the_10ms_budget():
    old = "\n".join(f"old line {i}" for i in range(2000))
    new = "\n".join(f"new line {i}" for i in range(2000))
    assert _best_ms(old, new) < 10


def test_opcodes_are_a_valid_edit_script():
    rng = random.Random(7)
    for _ in range(200):
        a = [rng.choice(["", "}", "x", "y", "z"]) for _ in range(rng.randint(0, 30))]
        b = [rng.choice(["", "}", "x", "y", "w"]) for _ in range(rng.randint(0, 30))]
        ops = render._diff_opcodes(a, b)
        assert _apply(a, b, ops) == b
        for op, i1, i2, j1, j2 in ops:
            if op == "equal":
                assert a[i1:i2] == b[j1:j2]


def test_one_line_change_counts_and_view_control():
    old = "\n".join(f"line {i}" for i in range(100))
    new = old.replace("line 50", "line fifty")
    assert render.diff_counts(old, new) == (1, 1)
    out = render.diff_view("a.py", old, new)
    assert "line fifty" in out and "line 10\n" not in out


def test_past_the_input_cap_the_middle_is_one_replace(monkeypatch):
    monkeypatch.setattr(render, "DIFF_INPUT_CAP", 3)
    ops = render._diff_opcodes(["h", "a", "b", "c", "d", "t"], ["h", "w", "x", "y", "z", "t"])
    assert ops == [("equal", 0, 1, 0, 1), ("replace", 1, 5, 1, 5), ("equal", 5, 6, 5, 6)]
