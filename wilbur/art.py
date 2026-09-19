"""Startup wordmark.

Block-letter WILBUR under a warm gradient, with a compact fallback for narrow
terminals and a plain-text fallback when colour is unavailable.
"""
from __future__ import annotations

# Warm terracotta ramp, dark to light, matching the accent used elsewhere.
GRADIENT = (130, 166, 173, 209, 216, 223)

WORDMARK = r"""
 __      __ ___  _     ___   _   _  ___
 \ \    / /|_ _|| |   | _ ) | | | || _ \
  \ \/\/ /  | | | |__ | _ \ | |_| ||   /
   \_/\_/  |___||____||___/  \___/ |_|_\
"""

COMPACT = r"""
 __      __ ___ _    ___
 \ \/\/ /| | | || |_ | . |  W I L B U R
  \_/\_/ |___|_||___||___|
"""

GLYPH = "✻"


try:                                  # written by scripts/png_to_art.py
    from .art_generated import ART as _GENERATED, PLAIN as _GENERATED_PLAIN
except ImportError:
    _GENERATED = _GENERATED_PLAIN = ""


def wordmark(width: int = 100, colour: bool = True) -> str:
    """Return the startup art sized for the terminal.

    A logo generated from a real image wins over the built-in wordmark, but
    only when it fits the terminal -- art wider than the window wraps into
    noise, which is worse than no art at all.
    """
    if _GENERATED:
        art = _GENERATED if colour else (_GENERATED_PLAIN or _GENERATED)
        if _visible_width(art) <= width:
            return art

    art = WORDMARK if width >= 46 else COMPACT
    lines = [line for line in art.strip("\n").splitlines() if line.strip()]
    if not colour:
        return "\n".join(lines)

    out = []
    for i, line in enumerate(lines):
        shade = GRADIENT[min(i, len(GRADIENT) - 1)]
        out.append(f"\033[38;5;{shade}m{line}\033[0m")
    return "\n".join(out)


def glyph_line(text: str, colour: bool = True) -> str:
    if not colour:
        return f"{GLYPH} {text}"
    return f"\033[38;5;209m{GLYPH}\033[0m \033[1m{text}\033[0m"


def _visible_width(art: str) -> int:
    """Widest line, ignoring ANSI escape sequences."""
    widest = 0
    for line in art.splitlines():
        visible, i, n = 0, 0, len(line)
        while i < n:
            if line[i] == "\033":
                while i < n and line[i] != "m":
                    i += 1
                i += 1
            else:
                visible += 1
                i += 1
        widest = max(widest, visible)
    return widest
