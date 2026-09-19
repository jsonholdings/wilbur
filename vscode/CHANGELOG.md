# Changelog

## 0.2.0

- Public-release prep: Apache-2.0 `LICENSE`/`NOTICE`, Marketplace metadata
  (`categories`, `keywords`, `galleryBanner` in the brand terracotta
  `#a8431c`), `repository`/`homepage`/`bugs` pointing at the public repo,
  removed `private: true`.
- `wilbur.path` now defaults to `wilbur` (resolved on `PATH`) instead of a
  machine-specific absolute path. A missing executable now shows a clear
  error with a link to Settings instead of failing silently.
- README rewritten for the Marketplace listing (requirements, install from
  Marketplace/Open VSX, settings table).
- The Wilbur terminal tab now shows the full-colour pig (media/wilbur-tab.svg,
  the icon.svg pig without its rounded-square background) instead of the
  generic terminal icon, with `terminal.ansiRed` as the closest built-in
  accent to the brand terracotta -- a colour Uri icon isn't theme-tinted, so
  it keeps its colours in both light and dark themes.

## 0.1.1

- Custom icon: a terracotta Wilbur pig with the ✻ spark for the extension list (media/icon.png, source media/icon.svg),
  and a matching monochrome activity-bar mark that follows the theme colour (the old one hard-coded dark eyes).

## 0.1.0

- Wilbur: Open — runs `wilbur` in a VS Code terminal (editor area), cwd = workspace root.
- Wilbur: Ask about selection (`ctrl+alt+w`, right-click menu) — sends file path, line range and selected text into the Wilbur terminal.
- Wilbur: Send current file.
- Activity bar entry with an "Open Wilbur" item.
- Settings: `wilbur.path`, `wilbur.useFlatpakSpawn` (auto/on/off).
- Flatpak-sandbox detection (`/.flatpak-info`) so Wilbur launches via `flatpak-spawn --host` only when needed.
