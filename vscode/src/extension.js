"use strict";
const vscode = require("vscode");
const { spawnSync } = require("child_process");
const { buildLaunchArgv, buildCheckArgv, shellQuote } = require("./sandbox");

const TERMINAL_NAME = "Wilbur";
let wilburTerminal = null;

function getConfig() {
  const cfg = vscode.workspace.getConfiguration("wilbur");
  return {
    wilburPath: cfg.get("path", "wilbur"),
    useFlatpakSpawn: cfg.get("useFlatpakSpawn", "auto"),
  };
}

/**
 * Check whether `wilburPath` resolves to a runnable executable, in the same
 * namespace it would actually be launched in.
 * @returns {boolean}
 */
function isWilburAvailable(wilburPath, useFlatpakSpawn) {
  const argv = buildCheckArgv({ wilburPath, useFlatpakSpawn });
  try {
    const result = spawnSync(argv[0], argv.slice(1));
    return result.status === 0;
  } catch {
    return false;
  }
}

function warnWilburNotFound(wilburPath) {
  vscode.window
    .showErrorMessage(
      `Wilbur: '${wilburPath}' was not found. Install the wilbur CLI (see ` +
        "https://github.com/jsonholdings/wilbur) so it is on your PATH, or " +
        "set wilbur.path to its absolute location.",
      "Open Settings"
    )
    .then((choice) => {
      if (choice === "Open Settings") {
        vscode.commands.executeCommand(
          "workbench.action.openSettings",
          "wilbur.path"
        );
      }
    });
}

function workspaceCwd() {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;
}

/** Get (or create) the Wilbur terminal, launching wilbur in it if it's new. */
function getOrCreateTerminal() {
  if (wilburTerminal && vscode.window.terminals.includes(wilburTerminal)) {
    return { terminal: wilburTerminal, isNew: false };
  }

  const { wilburPath, useFlatpakSpawn } = getConfig();
  if (!isWilburAvailable(wilburPath, useFlatpakSpawn)) {
    warnWilburNotFound(wilburPath);
    return { terminal: null, isNew: false };
  }
  const cwd = workspaceCwd();
  const argv = buildLaunchArgv({ wilburPath, useFlatpakSpawn });

  wilburTerminal = vscode.window.createTerminal({
    name: TERMINAL_NAME,
    cwd,
    location: vscode.TerminalLocation
      ? vscode.TerminalLocation.Editor
      : undefined,
    // A full-colour Uri icon (unlike a ThemeIcon) is not theme-tinted, so
    // the pig keeps its terracotta colours in both light and dark themes.
    iconPath: vscode.Uri.joinPath(
      vscode.Uri.file(__dirname),
      "..",
      "media",
      "wilbur-tab.svg",
    ),
    color: new vscode.ThemeColor("terminal.ansiRed"),
  });

  wilburTerminal.sendText(argv.map(shellQuote).join(" "), true);
  return { terminal: wilburTerminal, isNew: true };
}

function openWilbur() {
  const { terminal } = getOrCreateTerminal();
  if (!terminal) {
    return undefined;
  }
  terminal.show();
  return terminal;
}

/** Send a line of text into the (already-running) Wilbur REPL. */
function sendToWilbur(text) {
  const { terminal, isNew } = getOrCreateTerminal();
  if (!terminal) {
    return;
  }
  terminal.show();
  // Give a freshly spawned wilbur process a moment to start its REPL before
  // we push a prompt at it.
  const send = () => terminal.sendText(text, true);
  if (isNew) {
    setTimeout(send, 1500);
  } else {
    send();
  }
}

function askAboutSelection() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("Wilbur: no active editor.");
    return;
  }
  const selection = editor.selection;
  if (selection.isEmpty) {
    vscode.window.showWarningMessage("Wilbur: no text selected.");
    return;
  }
  const text = editor.document.getText(selection);
  const filePath = editor.document.uri.fsPath;
  const startLine = selection.start.line + 1;
  const endLine = selection.end.line + 1;
  const rangeLabel =
    startLine === endLine ? `line ${startLine}` : `lines ${startLine}-${endLine}`;

  const prompt =
    `About ${filePath} (${rangeLabel}):\n\n` + "```\n" + text + "\n```\n\n";
  sendToWilbur(prompt);
}

function sendCurrentFile() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("Wilbur: no active editor.");
    return;
  }
  const filePath = editor.document.uri.fsPath;
  sendToWilbur(`Please look at ${filePath}`);
}

class WilburTreeProvider {
  getTreeItem(element) {
    return element;
  }
  getChildren() {
    const item = new vscode.TreeItem(
      "Open Wilbur",
      vscode.TreeItemCollapsibleState.None
    );
    item.command = { command: "wilbur.open", title: "Open Wilbur" };
    item.iconPath = new vscode.ThemeIcon("hubot");
    return [item];
  }
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("wilbur.open", openWilbur),
    vscode.commands.registerCommand(
      "wilbur.askAboutSelection",
      askAboutSelection
    ),
    vscode.commands.registerCommand("wilbur.sendCurrentFile", sendCurrentFile),
    vscode.window.registerTreeDataProvider(
      "wilburActivityView",
      new WilburTreeProvider()
    ),
    vscode.window.onDidCloseTerminal((t) => {
      if (t === wilburTerminal) {
        wilburTerminal = null;
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
