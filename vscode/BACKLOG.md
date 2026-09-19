# Backlog

## Diff accept/reject for Wilbur's edits (v2)

Design: Wilbur's terminal REPL only prints text today, so there is no
structured edit event for the extension to react to. v2 needs the Wilbur CLI
itself to emit a machine-readable proposed-edit record —
e.g. a JSON line on a side channel (a unix socket or a `.wilbur/pending-edit.json`
file written per turn) carrying `{file, oldText, newText, range}` — instead of
only printing a diff to the terminal.

The extension would watch for that record, build an in-memory "modified"
document, and call `vscode.commands.executeCommand("vscode.diff", originalUri,
modifiedUri, title)` to show VS Code's native diff view, with accept/reject
codicons in the diff editor's title bar wired to commands that either apply
the edit (`vscode.workspace.applyEdit`) or discard it and tell Wilbur so via
the same side channel.

Not built now — out of v1/0.3.0 scope. Needs the Wilbur CLI side built first
(an opt-in `--edit-protocol` flag or env var that, before applying an
edit_file/write_file, writes the proposed edit to a per-session inbox dir
and waits for accept/reject; the CLI behaves exactly as before without the
flag).

## Chat sidebar webview (v2)

A sidebar view (contributes.views) with a webview panel: an input box that
sends lines into the Wilbur terminal and shows its status, with a strict
CSP and no remote resources. Not built now — out of 0.3.0 scope. The
status bar, sessions and diagnostics commands cover the near-term use
cases; revisit once those have real usage.
