# Wilbur for VS Code

<p align="center"><img src="media/icon.png" width="96" alt="Wilbur"></p>

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
code --install-extension wilbur-vscode-0.2.0.vsix
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

<!-- Screenshot slot: activity bar + terminal panel with Wilbur running. -->

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `wilbur.path` | `wilbur` | Path to, or name on `PATH` of, the `wilbur` executable, resolved on the host. If it can't be found, Wilbur shows an error with a link to Settings. |
| `wilbur.useFlatpakSpawn` | `auto` | `auto` prefixes `flatpak-spawn --host` only when VS Code itself is running in a Flatpak sandbox (detected via `/.flatpak-info`); `on`/`off` force it. |

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
