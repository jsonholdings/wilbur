# Changelog


## 0.4.3

- Change: `Wilbur: Open` now always opens a session. It previously reused a
  healthy one, on the reasoning that a keybinding should focus what you have
  rather than stack terminals up -- but a command called "Open" that refuses
  to open is wrong however defensible the reuse looks. Use
  `Wilbur: Focus Session` to switch between live sessions instead.


## 0.4.2

- Add: multiple concurrent Wilbur sessions. `Wilbur: New Session` always opens
  an additional terminal; `Wilbur: Focus Session` picks which live session
  subsequent sends target. Previously the extension held a single terminal and
  `Wilbur: Open` returned it forever, so a second concurrent session was
  impossible by construction -- not a bug in the reuse check, a missing
  capability. `Wilbur: Open` still reuses a healthy session on purpose: a
  keybinding should focus the one you have, not spawn a pile of them.
  Closing the active session promotes the next live one so sends always have
  a target.


## 0.4.1

- Fix: Wilbur could only be opened ONCE per VS Code window. Exiting wilbur ends
  the process but leaves the terminal open as a dead tab -- `exitStatus` becomes
  defined, `onDidCloseTerminal` never fires, and the terminal stays listed in
  `window.terminals`. The reuse check saw a live-looking terminal and handed back
  the dead one on every subsequent open, so a new session required closing and
  reopening VS Code. A terminal whose process has exited is now discarded and a
  fresh one created; a healthy session is still reused rather than duplicated.
  Also clears the singleton on terminal state change, not only on tab close.

## 0.4.0

- Fix: a saved Wilbur terminal could reappear and resume its old work on its own the next
  time VS Code started, instead of waiting for an explicit "Open" or "Resume" action. Wilbur
  terminals are now marked transient, so VS Code never revives or replays them across a
  restart, and the launch command is no longer sent in the same instant the terminal is
  created, closing a race where another extension's own text could land in Wilbur's prompt
  first.
- The sessions picker (`Wilbur: Sessions`) now has a trash button on each entry to permanently
  forget a saved session, so a stale or unwanted one can be cleared without it ever being able
  to resurface via Resume/Continue.

## 0.3.2

- New Wilbur icon set: a redrawn pig mark for the Marketplace icon, the activity bar and the
  editor tab, with dedicated small-size drawings so it stays legible at 16px.

## 0.3.1

- Fix: the README header image on the Marketplace and Open VSX pointed at a non-existent repo-root path; it now uses its full URL.

## 0.3.0

- **Sessions:** Wilbur: Resume last session (`--continue`) and Wilbur:
  Sessions… (a quick pick built from `wilbur --sessions --json`, new in
  wilbur 2.9.0), both opening in a new terminal so they never clobber the
  one the other commands talk to.
- **Status bar:** shows the model wilbur will launch with and whether
  Ollama is reachable (polled every 30s against `/api/version`). Click it
  to list local Ollama models (`/api/tags`) and switch wilbur's default
  model, writing `~/.config/wilbur/config.json` directly.
- **Diagnostics:** a "Fix with Wilbur" quick-fix code action on any
  error/warning, plus Wilbur: Send problems in file, both sending file,
  line and message into the Wilbur terminal. Model filtering on the
  switch-model list is unfiltered by design — Ollama's `/api/tags` has no
  reliable tool-calling flag to filter on.
- **Settings:** `wilbur.model`, `wilbur.approvalMode` (`manual`/`auto`,
  maps to `--yes`), `wilbur.contextSize` (`-c`), applied whenever wilbur is
  launched.
- Requires wilbur 2.9.0+ for Wilbur: Sessions… (`--sessions --json`);
  earlier CLI versions still work for every other command.
- Not in this release: diff review for proposed edits, and a chat sidebar
  webview. Both are tracked in BACKLOG.md.

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
