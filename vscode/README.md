# Wilbur for VS Code

<p align="center"><img src="https://raw.githubusercontent.com/jsonholdings/wilbur/main/vscode/media/icon.png" width="96" alt="Wilbur"></p>

**Wilbur** — a local coding agent for the terminal, tools, subagents, a plan
it keeps, driven entirely by models on your own GPU. This extension runs
Wilbur in a VS Code terminal panel, the way Claude Code's own VS Code
integration works, without sending anything to a cloud model.

GitHub: https://github.com/jsonholdings/wilbur

## Requirements

- [Ollama](https://ollama.com) running locally, with a tool-capable model pulled.
- The `wilbur` CLI installed and on your `PATH` (see the main repo's README
  for the installer). If VS Code can't find it, set `wilbur.path`.

## Install

From the Marketplace: search **Wilbur** and click Install, or from Open VSX
if you're on a Marketplace-compatible fork (VSCodium, etc).

From a `.vsix` (built from source):

```
cd vscode
npx @vscode/vsce package
code --install-extension wilbur-vscode-0.3.0.vsix
```

(On a Flatpak VS Code install, prefix both commands with
`flatpak-spawn --host`.) Reload the window afterwards
(`Developer: Reload Window`).

## Use

- **Wilbur: Open** (command palette, or the Wilbur icon in the activity bar) —
  opens a terminal running `wilbur` with its cwd set to the workspace root.
- **Wilbur: Ask about selection** (right-click a selection, or `ctrl+alt+w`) —
  sends the file path, line range and selected text into the Wilbur terminal
  as a prompt, starting Wilbur first if it isn't already running.
- **Wilbur: Send current file** — sends the active file's path into the
  Wilbur terminal.
- **Wilbur: Resume last session** — launches `wilbur --continue` in a new
  terminal, resuming the latest saved session for the workspace's cwd.
- **Wilbur: Sessions…** — lists wilbur's saved sessions (via
  `wilbur --sessions --json`) in a quick pick, showing each one's objective,
  working directory and how long ago it was updated, then resumes the one
  you pick with `wilbur --resume <id>` in a new terminal.
- **Wilbur: Send problems in file** — sends every diagnostic (error/warning)
  in the active file to Wilbur as one message.
- **Fix with Wilbur** — a quick-fix code action offered on any diagnostic
  (error/warning) that sends its file, line and message to Wilbur.
- **Status bar model indicator** — shows the model wilbur will launch with
  and whether Ollama is reachable (polled every 30s). Click it to list local
  Ollama models and switch wilbur's default model
  (writes `~/.config/wilbur/config.json` directly; applies to the next
  launch, not a running terminal).

<!-- Screenshot slot: activity bar + terminal panel with Wilbur running. -->

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `wilbur.path` | `wilbur` | Path to, or name on `PATH` of, the `wilbur` executable, resolved on the host. If it can't be found, Wilbur shows an error with a link to Settings. |
| `wilbur.useFlatpakSpawn` | `auto` | `auto` prefixes `flatpak-spawn --host` only when VS Code itself is running in a Flatpak sandbox (detected via `/.flatpak-info`); `on`/`off` force it. |
| `wilbur.model` | `""` | Passed to wilbur via `-m` on launch when set. Empty uses wilbur's own default. The status bar's model switcher edits wilbur's config file directly instead of this setting. |
| `wilbur.approvalMode` | `manual` | `auto` passes `--yes` to wilbur, auto-approving its proposed actions. |
| `wilbur.contextSize` | `0` | Passed to wilbur via `-c` when greater than 0. `0` means unset. |

## Why `flatpak-spawn --host`

If your VS Code install is a Flatpak (e.g. `com.visualstudio.code`), the
extension host runs inside that sandbox. `wilbur` (and `node`/`npm` used to
build this extension) live on the host, not in the sandbox, so launching
`wilbur` directly from inside VS Code would fail with "command not found".
`flatpak-spawn --host <cmd>` runs `<cmd>` in the real host namespace instead.
This is auto-detected and only applies on Flatpak installs.

## Development

```
npm test
```

Unit tests cover the sandbox-detection/launch-argv logic in `src/sandbox.js`
without requiring the `vscode` module.

## Status

v1: terminal-based (open, ask-about-selection, send-current-file). Diff
accept/reject for Wilbur's proposed edits is planned — see `BACKLOG.md`.

## License

Apache-2.0. See `LICENSE`.
