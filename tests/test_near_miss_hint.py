"""_near_miss_hint: the 'why did the exact match fail' message.

Pins the current output of each branch so a wording change is a visible
diff, and proves the no-close-match path cannot crash on degenerate input.
Pure string logic: no network, no GPU, no files.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.tools.files import _near_miss_hint


def test_whitespace_only_difference_produces_indentation_hint():
    text = "def f():\n    return 1\n"
    old = "def f():\n  return 1\n"
    assert _near_miss_hint(text, old) == (
        " The text is present but the indentation differs. "
        "Re-read the file and copy the leading whitespace exactly.")


def test_first_line_present_points_mismatch_further_down():
    text = "alpha\nbeta\n"
    old = "alpha\ngamma\n"
    assert _near_miss_hint(text, old) == (
        " The first line ('alpha') is present, so the mismatch is "
        "further down. Re-read that region and copy it verbatim.")


def test_no_close_match_does_not_crash_and_pins_fallback():
    text = "alpha\nbeta\n"
    old = "completely different text\n"
    assert _near_miss_hint(text, old) == (
        " No similar text found either -- re-read the file.")


def test_empty_old_string_does_not_crash():
    # old.splitlines() is [] and stripped is "", so both branches are
    # skipped and the fallback is returned without an IndexError.
    assert _near_miss_hint("alpha\nbeta\n", "") == (
        " No similar text found either -- re-read the file.")


def test_whitespace_only_old_string_does_not_crash():
    # old.splitlines() is ["   ", "\t"], so stripped is " " (truthy) and
    # " " is in the stripped haystack -- the indentation branch is taken.
    # The point is that it does not crash on a whitespace-only old_string.
    assert _near_miss_hint("alpha\nbeta\n", "   \n\t") == (
        " The text is present but the indentation differs. "
        "Re-read the file and copy the leading whitespace exactly.")
