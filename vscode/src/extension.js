"use strict";
const vscode = require("vscode");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const http = require("http");
const {
  buildLaunchArgv,
  buildCheckArgv,
  buildSessionsArgv,
  shellQuote,
} = require("./sandbox");

const TERMINAL_NAME = "Wilbur";
let wilburTerminal = null;
let statusBarItem = null;
let statusBarPollHandle = null;

function getConfig() {
  const cfg = vscode.workspace.getConfiguration("wilbur");
  return {
    wilburPath: cfg.get("path", "wilbur"),
    useFlatpakSpawn: cfg.get("useFlatpakSpawn", "auto"),
    model: cfg.get("model", ""),
    approvalMode: cfg.get("approvalMode", "manual"),
    contextSize: cfg.get("contextSize", 0),
  };
}

/** Extra CLI args derived from the wilbur.model/approvalMode/contextSize settings. */
function launchExtraArgs() {
  const { model, approvalMode, contextSize } = getConfig();
  const args = [];
  if (model) {
    args.push("-m", model);
  }
  if (approvalMode === "auto") {
    args.push("--yes");
  }
  if (contextSize > 0) {
    args.push("-c", String(contextSize));
  }
  return args;
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
  const argv = buildLaunchArgv({
    wilburPath,
    useFlatpakSpawn,
    args: launchExtraArgs(),
  });

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

/**
 * Launch wilbur with extra args in a brand-new terminal (never the reused
 * singleton `wilburTerminal`, so a resume/session pick doesn't clobber the
 * terminal `sendToWilbur`/askAboutSelection/sendCurrentFile talk to).
 * @param {string} name terminal tab name
 * @param {string[]} extraArgs e.g. ["--continue"] or ["--resume", id]
 */
function launchWilburTerminal(name, extraArgs) {
  const { wilburPath, useFlatpakSpawn } = getConfig();
  if (!isWilburAvailable(wilburPath, useFlatpakSpawn)) {
    warnWilburNotFound(wilburPath);
    return undefined;
  }
  const cwd = workspaceCwd();
  const argv = buildLaunchArgv({
    wilburPath,
    useFlatpakSpawn,
    args: [...extraArgs, ...launchExtraArgs()],
  });
  const terminal = vscode.window.createTerminal({
    name,
    cwd,
    location: vscode.TerminalLocation
      ? vscode.TerminalLocation.Editor
      : undefined,
    iconPath: vscode.Uri.joinPath(
      vscode.Uri.file(__dirname),
      "..",
      "media",
      "wilbur-tab.svg",
    ),
    color: new vscode.ThemeColor("terminal.ansiRed"),
  });
  terminal.sendText(argv.map(shellQuote).join(" "), true);
  terminal.show();
  return terminal;
}

function resumeLast() {
  launchWilburTerminal(`${TERMINAL_NAME}: continue`, ["--continue"]);
}

/** Human-readable "Nm ago" / "Nh ago" / "Nd ago" from a unix-seconds timestamp. */
function relativeTime(unixSeconds) {
  if (!unixSeconds) {
    return "unknown time";
  }
  const minutes = Math.round((Date.now() - unixSeconds * 1000) / 60000);
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  return `${Math.round(hours / 24)}d ago`;
}

/** Command palette flow: pick a saved wilbur session and resume it. */
function sessionsPicker() {
  const { wilburPath, useFlatpakSpawn } = getConfig();
  if (!isWilburAvailable(wilburPath, useFlatpakSpawn)) {
    warnWilburNotFound(wilburPath);
    return;
  }
  const argv = buildSessionsArgv({ wilburPath, useFlatpakSpawn });
  let result;
  try {
    result = spawnSync(argv[0], argv.slice(1), { encoding: "utf8" });
  } catch (err) {
    vscode.window.showErrorMessage(
      `Wilbur: could not list sessions: ${err.message}`
    );
    return;
  }
  if (result.status !== 0 || !result.stdout) {
    vscode.window.showErrorMessage(
      "Wilbur: could not list sessions (is wilbur.path correct and up to date?)."
    );
    return;
  }
  let sessions;
  try {
    sessions = JSON.parse(result.stdout);
  } catch {
    vscode.window.showErrorMessage(
      "Wilbur: --sessions --json output was not valid JSON."
    );
    return;
  }
  if (!Array.isArray(sessions) || sessions.length === 0) {
    vscode.window.showInformationMessage("Wilbur: no saved sessions found.");
    return;
  }
  const items = sessions.map((s) => ({
    label: (s.objective && s.objective[0]) || "(no objective)",
    description: typeof s.turns === "number" ? `${s.turns} turns` : undefined,
    detail: `${s.cwd || "(unknown cwd)"} · ${relativeTime(s.updated)}`,
    id: s.id,
  }));
  vscode.window
    .showQuickPick(items, { placeHolder: "Resume a Wilbur session" })
    .then((choice) => {
      if (!choice) {
        return;
      }
      launchWilburTerminal(`${TERMINAL_NAME}: resume`, [
        "--resume",
        choice.id,
      ]);
    });
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

// --- wilbur's own config file (~/.config/wilbur/config.json), read/written directly ---

function wilburConfigPath() {
  return path.join(os.homedir(), ".config", "wilbur", "config.json");
}

/** Read wilbur's config with defaults; never throws if the file is missing/invalid. */
function readWilburConfig() {
  let parsed = {};
  try {
    parsed = JSON.parse(fs.readFileSync(wilburConfigPath(), "utf8"));
  } catch {
    parsed = {};
  }
  return {
    base_url: parsed.base_url || "http://localhost:11434",
    model: parsed.model || "qwen3:14b",
  };
}

/** Merge `model` into wilbur's config file on disk, creating it if needed. */
function writeWilburModel(model) {
  const cfgPath = wilburConfigPath();
  let existing = {};
  try {
    existing = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  } catch {
    existing = {};
  }
  existing.model = model;
  fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
  fs.writeFileSync(cfgPath, JSON.stringify(existing, null, 2) + "\n", "utf8");
}

/** GET a JSON URL with node's built-in http module; rejects on non-2xx/timeout/error. */
function httpGetJson(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: timeoutMs || 3000 }, (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(err);
          }
        } else {
          reject(new Error(`HTTP ${res.statusCode}`));
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.on("error", reject);
  });
}

/** Refresh the status bar item's text/color from Ollama's reachability + wilbur's model. */
function updateStatusBar() {
  if (!statusBarItem) {
    return;
  }
  const { base_url, model } = readWilburConfig();
  httpGetJson(`${base_url}/api/version`)
    .then(() => {
      statusBarItem.text = `$(circle-large-filled) ${model} · Ollama ok`;
      statusBarItem.backgroundColor = undefined;
      statusBarItem.tooltip = `Ollama reachable at ${base_url}. Click to switch model.`;
    })
    .catch(() => {
      statusBarItem.text = `$(warning) ${model} · Ollama unreachable`;
      statusBarItem.backgroundColor = new vscode.ThemeColor(
        "statusBarItem.warningBackground"
      );
      statusBarItem.tooltip = `Could not reach Ollama at ${base_url}. Click to switch model.`;
    });
}

/**
 * List local Ollama models and, on pick, write the choice into wilbur's own
 * config file as the default model. Note: /api/tags has no reliable
 * "tool-capable" flag, so this lists every local model unfiltered rather
 * than overclaiming a filter it can't actually apply.
 */
function switchModel() {
  const { base_url } = readWilburConfig();
  httpGetJson(`${base_url}/api/tags`)
    .then((data) => {
      const models = ((data && data.models) || [])
        .map((m) => m && m.name)
        .filter(Boolean);
      if (models.length === 0) {
        vscode.window.showWarningMessage("Wilbur: no local Ollama models found.");
        return;
      }
      vscode.window
        .showQuickPick(models, {
          placeHolder: "Select a model for Wilbur (all local models; not filtered by tool-calling support)",
        })
        .then((choice) => {
          if (!choice) {
            return;
          }
          try {
            writeWilburModel(choice);
          } catch (err) {
            vscode.window.showErrorMessage(
              `Wilbur: could not write ${wilburConfigPath()}: ${err.message}`
            );
            return;
          }
          updateStatusBar();
          const running =
            wilburTerminal && vscode.window.terminals.includes(wilburTerminal);
          vscode.window.showInformationMessage(
            running
              ? `Wilbur: default model set to ${choice}. The running Wilbur terminal keeps its current model; this applies next launch.`
              : `Wilbur: default model set to ${choice}.`
          );
        });
    })
    .catch(() => {
      vscode.window.showErrorMessage(
        `Wilbur: could not reach Ollama at ${base_url} to list models.`
      );
    });
}

// --- diagnostics -> "Fix with Wilbur" ---

class WilburCodeActionProvider {
  provideCodeActions(document, _range, context) {
    return context.diagnostics.map((diagnostic) => {
      const action = new vscode.CodeAction(
        `Fix with Wilbur: ${diagnostic.message}`,
        vscode.CodeActionKind.QuickFix
      );
      action.command = {
        command: "wilbur._fixDiagnostic",
        title: "Fix with Wilbur",
        arguments: [document, diagnostic],
      };
      return action;
    });
  }
}

function fixDiagnostic(document, diagnostic) {
  const relPath = vscode.workspace.asRelativePath(document.uri);
  const line = diagnostic.range.start.line;
  let codeLine = "";
  try {
    codeLine = document.lineAt(line).text;
  } catch {
    codeLine = "";
  }
  const message =
    `Fix this problem in ${relPath}:${line + 1}: ${diagnostic.message}\n\n` +
    "```\n" +
    codeLine +
    "\n```\n";
  sendToWilbur(message);
}

function sendProblems() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("Wilbur: no active editor.");
    return;
  }
  const diagnostics = vscode.languages.getDiagnostics(editor.document.uri);
  if (!diagnostics || diagnostics.length === 0) {
    vscode.window.showWarningMessage("Wilbur: no problems in the current file.");
    return;
  }
  const relPath = vscode.workspace.asRelativePath(editor.document.uri);
  const lines = diagnostics.map(
    (d) => `line ${d.range.start.line + 1}: ${d.message}`
  );
  sendToWilbur(`Problems in ${relPath}:\n\n${lines.join("\n")}\n`);
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
  statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBarItem.command = "wilbur.switchModel";
  statusBarItem.show();
  updateStatusBar();
  statusBarPollHandle = setInterval(updateStatusBar, 30000);

  context.subscriptions.push(
    vscode.commands.registerCommand("wilbur.open", openWilbur),
    vscode.commands.registerCommand(
      "wilbur.askAboutSelection",
      askAboutSelection
    ),
    vscode.commands.registerCommand("wilbur.sendCurrentFile", sendCurrentFile),
    vscode.commands.registerCommand("wilbur.resumeLast", resumeLast),
    vscode.commands.registerCommand("wilbur.sessions", sessionsPicker),
    vscode.commands.registerCommand("wilbur.switchModel", switchModel),
    vscode.commands.registerCommand("wilbur.sendProblems", sendProblems),
    vscode.commands.registerCommand("wilbur._fixDiagnostic", fixDiagnostic),
    vscode.languages.registerCodeActionsProvider(
      "*",
      new WilburCodeActionProvider(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }
    ),
    vscode.window.registerTreeDataProvider(
      "wilburActivityView",
      new WilburTreeProvider()
    ),
    vscode.window.onDidCloseTerminal((t) => {
      if (t === wilburTerminal) {
        wilburTerminal = null;
      }
    }),
    statusBarItem,
    { dispose: () => clearInterval(statusBarPollHandle) }
  );
}

function deactivate() {
  if (statusBarPollHandle) {
    clearInterval(statusBarPollHandle);
    statusBarPollHandle = null;
  }
}

module.exports = { activate, deactivate };
