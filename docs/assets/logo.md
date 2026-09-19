# Logo assets

`logo-source-light.svg`, `logo-source-dark.svg` and `logo-source-tinted.svg` are the
ONLY hand-maintained files here — the owner-approved Wilbur mark (brand-wave1-v4,
48px-and-up "eared pig" drawing; see `BRAND.md` at the repo root for the colour
table). Everything else in this directory — `icon.svg`, `logo-light.svg`,
`logo-dark.svg`, `social-preview.svg`, `social-preview.png`, `avatar.svg` and
`avatar.png` — is generated from them by `scripts/build_logo_assets.py` and must
never be hand-edited.

Unlike squire's mark (a single outline shield colour-swapped by a fixed table),
Wilbur's dark and tinted variants are each their own hand-tuned drawing per
`jsonholdings/infrastructure/design-tokens/BRAND-SYSTEM.md` — darkened value, not a
computed inversion — so there are three source files instead of one.

## To change the logo, replace the source files

1. Replace `logo-source-light.svg` / `-dark.svg` / `-tinted.svg` with the new mark
   (each `viewBox="0 0 1024 1024"`, colours baked in).
2. Run:
   ```sh
   python3 scripts/build_logo_assets.py
   ```
3. Commit the regenerated files under `docs/assets/`.

## Avatar

`avatar.png` is a 500x500 square: the mark filling most of the frame with a ~6%
safe-area margin on a `#fbfaf7` paper background. The margin is smaller than
squire's 15% because Wilbur's mark already carries its own filled superellipse tile
and background gradient — it does not need as much surrounding paper as an outline
mark does.

It is for the GitHub org profile picture or wherever else a square Wilbur mark is
needed — a repository has no avatar of its own; what shows on a repo's card is the
social preview image built above (`social-preview.png`). Uploading a chosen avatar
is always a manual UI step (GitHub org Settings -> Profile picture) — this script
only produces the file, never uploads it.

## Social preview

GitHub's repository social-preview image (shown on link unfurls and the repo's own
card) can only be SET through the web UI, not the API. `social-preview.png` is the
file to upload; see `SETUP.md` in the review folder for the exact click-path.

## README embed

<!-- Paste into README.md. Renders logo-dark.svg under a dark OS/browser theme,
     logo-light.svg otherwise. GitHub's own README rendering honors prefers-color-scheme
     inside <picture> for images. -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
  <img alt="wilbur" src="docs/assets/logo-light.svg" width="272" height="51">
</picture>
