#!/usr/bin/env python3
"""Build Wilbur's GitHub-presence logo assets from the hand-drawn mark sources.

`docs/assets/logo-source-{light,dark,tinted}.svg` are the ONLY hand-maintained files
here (they are the owner-approved brand-wave1-v4 Wilbur mark, unchanged). Everything
else in docs/assets/ — icon.svg, logo-light.svg, logo-dark.svg, social-preview.svg,
social-preview.png, avatar.svg and avatar.png — is generated from them by this script
and must never be hand-edited.

Mirrors the shape of squire's docs/assets/build_logo_assets.py (same output list,
same README <picture> convention) so the two projects read as siblings from one
brand family, per jsonholdings/infrastructure/design-tokens/BRAND-SYSTEM.md.

Run:
    python3 scripts/build_logo_assets.py
Then render social-preview.png / avatar.png separately (see NOTES in docs/assets/)
since this script only emits vector + the light-source SVGs; PNG rendering needs a
real browser (chrome-headless-shell), not available in every environment this
script runs in.
"""
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

INK_LIGHT = "#3a2320"   # matches the mark's own eye ink -- no 4th colour introduced
INK_DARK = "#f2f0ea"    # neutral light ink for dark backgrounds, same role as squire's

MONO_FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
             "'Liberation Mono', monospace")
SANS_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def read_source(variant: str) -> str:
    return (ASSETS / f"logo-source-{variant}.svg").read_text()


def inner_svg(svg_text: str) -> str:
    """Return the mark's inner markup (defs + drawing), viewBox 0 0 1024 1024,
    with <title> stripped -- callers supply their own <title>/aria-label."""
    body = re.sub(r"<\?xml.*?\?>\s*", "", svg_text)
    body = re.sub(r"<svg[^>]*>", "", body, count=1)
    body = body.rsplit("</svg>", 1)[0]
    body = re.sub(r"<title>.*?</title>\s*", "", body, flags=re.S)
    return body.strip()


def build_icon():
    """icon.svg: the light mark at a GitHub-README-friendly 64x64 display size.
    Pure vector re-display (viewBox stays 0 0 1024 1024, width/height drop to 64)
    -- no coordinate scaling needed, no quality loss at any size a README embeds it."""
    inner = inner_svg(read_source("light"))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="64" height="64" role="img" aria-label="wilbur icon">
  <title>wilbur</title>
  {inner}
</svg>
'''
    (ASSETS / "icon.svg").write_text(svg)


def build_logo(variant: str, ink: str):
    """logo-{variant}.svg: icon + wordmark lockup, same viewBox aspect (340x64,
    displayed 272x51) and layout rhythm as squire's logo-{variant}.svg."""
    inner = inner_svg(read_source(variant))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 340 64" width="340" height="64" role="img" aria-label="wilbur logo">
  <title>wilbur</title>
  <g transform="scale(0.0625)">
    {inner}
  </g>
  <text x="76" y="42" font-family="{MONO_FONT}" font-size="30" font-weight="600" letter-spacing="0.5" fill="{ink}">wilbur</text>
</svg>
'''
    (ASSETS / f"logo-{variant}.svg").write_text(svg)


def build_avatar():
    """avatar.svg: 500x500, the mark filling most of the frame with a small safe-area
    margin. Unlike squire's avatar (an outline mark needing a paper background to sit
    on), Wilbur's mark already carries its own filled tile + background gradient, so
    the padding here is a ~6% icon safe-area, not squire's 15% figure-on-paper margin."""
    inner = inner_svg(read_source("light"))
    pad = 500 * 0.06
    size = 500 - 2 * pad
    scale = size / 1024
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500" width="500" height="500">
  <title>wilbur avatar</title>
  <rect width="500" height="500" fill="#fbfaf7"/>
  <g transform="translate({pad:.2f},{pad:.2f}) scale({scale:.6f})">
    {inner}
  </g>
</svg>
'''
    (ASSETS / "avatar.svg").write_text(svg)


def build_social_preview():
    """social-preview.svg: GitHub's 1280x640 repo social card. Mark + wordmark left,
    tagline and footer below -- safe area kept inside a centred ~1200x600 box since
    GitHub crops the outer edge in some surfaces (link-preview thumbnails)."""
    inner = inner_svg(read_source("light"))
    mark_scale = 220 / 1024
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" width="1280" height="640">
  <title>wilbur -- a local coding agent for the terminal</title>
  <rect x="0" y="0" width="1280" height="640" fill="#fbfaf7"/>
  <rect x="0" y="0" width="1280" height="640" fill="none" stroke="#e3ded4" stroke-width="4"/>

  <g transform="translate(120,210) scale({mark_scale:.6f})">
    {inner}
  </g>

  <text x="380" y="300" font-family="{MONO_FONT}" font-size="88" font-weight="600" letter-spacing="1" fill="#3a2320">wilbur</text>

  <text x="382" y="356" font-family="{SANS_FONT}" font-size="27" fill="#4a4f57">A local coding agent for the terminal, driven entirely</text>
  <text x="382" y="392" font-family="{SANS_FONT}" font-size="27" fill="#4a4f57">by models on your own GPU.</text>

  <line x1="382" y1="432" x2="940" y2="432" stroke="#e3ded4" stroke-width="2"/>
  <text x="382" y="472" font-family="{MONO_FONT}" font-size="22" fill="#676d75">A JSON Holdings project</text>
</svg>
'''
    (ASSETS / "social-preview.svg").write_text(svg)


def main():
    build_icon()
    build_logo("light", INK_LIGHT)
    build_logo("dark", INK_DARK)
    build_avatar()
    build_social_preview()
    print("Wrote icon.svg, logo-light.svg, logo-dark.svg, avatar.svg, social-preview.svg")
    print("PNG renders (avatar.png, social-preview.png) are NOT built by this script --")
    print("render them from the .svg with chrome-headless-shell and inspect visually.")


if __name__ == "__main__":
    main()
