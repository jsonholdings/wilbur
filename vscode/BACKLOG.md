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

Not built now — out of v1 scope. Needs the Wilbur CLI side built first.
