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
  buildForgetArgv,
  shellQuote,
} = require("./sandbox");

const TERMINAL_NAME = "Wilbur";
// See sendLaunchLine()'s docstring for why this exists.
const LAUNCH_DELAY_MS = 400;
let wilburTerminal = null;
/**
 * Every live Wilbur terminal, oldest first. `wilburTerminal` is the ACTIVE
 * one -- the target for sendToWilbur/askAboutSelection/sendCurrentFile --
 * and is always also a member of this list while it lives.
 *
 * Before 0.4.2 there was only the singleton, so a second concurrent session
 * was impossible by construction: `wilbur.open` returned the existing
 * terminal forever (owner, 2026-09-20: "a second session will not open
 * either"). Resume/session-pick already created standalone terminals via
 * launchWilburTerminal(), so the capability existed and was simply never
 * reachable from the open path.
 */
let wilburSessions = [];

/** Drop terminals VS Code has closed or whose process has exited. */
function pruneSessions() {
  wilburSessions = wilburSessions.filter(
    (t) => vscode.window.terminals.includes(t) && !isDeadTerminal(t)
  );
  if (wilburTerminal && !wilburSessions.includes(wilburTerminal)) {
    wilburTerminal = wilburSessions.length ? wilburSessions[wilburSessions.length - 1] : null;
  }
}

/** Register a terminal as a session and make it the active one. */
function adoptSession(terminal) {
  if (!terminal) return terminal;
  if (!wilburSessions.includes(terminal)) {
    wilburSessions.push(terminal);
  }
  wilburTerminal = terminal;
  return terminal;
}
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

/**
 * Send the wilbur launch command into a just-created terminal.
 *
 * A brand-new VS Code terminal is not exclusively ours the instant it is
 * created: other extensions (notably Python's "activate environment in new
 * terminals" feature) also write into a freshly opened terminal, and a
 * `sendText` fired in the very same tick as `createTerminal` can land ahead
 * of, or interleaved with, that other write. If wilbur's own prompt happens
 * to be the thing reading the pty when that unrelated text arrives, wilbur
 * treats it as a genuine first message and acts on it -- which is how a
 * stale `source .../.venv/bin/activate` line for an unrelated project ended
 * up as the literal first turn of a brand-new wilbur session on 2026-09-19.
 * A short delay does not make the race impossible, but it gives the shell
 * time to finish its own startup (and any other extension's injected text)
 * before wilbur's launch line -- and therefore wilbur's own prompt -- exists
 * to receive it.
 * @param {import("vscode").Terminal} terminal
 * @param {string[]} argv
 */
function sendLaunchLine(terminal, argv) {
  setTimeout(() => {
    terminal.sendText(argv.map(shellQuote).join(" "), true);
  }, LAUNCH_DELAY_MS);
}

/**
 * True when a terminal object still exists but its process has exited.
 *
 * Exiting wilbur is NOT the same as closing the terminal. When the process
 * ends, VS Code leaves the terminal open as a dead tab: `exitStatus` becomes
 * defined, but `onDidCloseTerminal` does NOT fire and the terminal is still
 * present in `vscode.window.terminals`.
 *
 * That combination is what made this extension unusable after one session
 * (owner, 2026-09-20: "you can only open one session and when you exit that
 * session you cant open a new one without closing and reopening vscode").
 * The reuse check below saw a live-looking terminal and handed back the
 * corpse, forever, because nothing cleared `wilburTerminal` until the whole
 * window was reloaded.
 * @param {import("vscode").Terminal} terminal
 */
function isDeadTerminal(terminal) {
  return !!terminal && terminal.exitStatus !== undefined;
}

/** Get (or create) the Wilbur terminal, launching wilbur in it if it's new. */
function getOrCreateTerminal() {
  // A terminal whose process has exited must be dropped, not reused --
  // sending a launch line to it does nothing and the user sees a dead tab.
  pruneSessions();
  if (isDeadTerminal(wilburTerminal)) {
    try {
      wilburTerminal.dispose();
    } catch (_) {
      // already gone; nothing to clean up
    }
    wilburTerminal = null;
  }

  if (
    wilburTerminal &&
    vscode.window.terminals.includes(wilburTerminal) &&
    !isDeadTerminal(wilburTerminal)
  ) {
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
    // Never let VS Code revive this terminal (or replay anything queued for
    // it) across a window reload/crash. A previous run's leftover session
    // must not resurface on its own the next time the owner opens Wilbur --
    // that is the exact bug reported 2026-09-19: an old "twilio lookup"
    // session's shell activation line landed as the *first turn* of a brand
    // new wilbur process after reopening VS Code.
    isTransient: true,
  });

  adoptSession(wilburTerminal);
  sendLaunchLine(wilburTerminal, argv);
  return { terminal: wilburTerminal, isNew: true };
}

/**
 * Open an ADDITIONAL Wilbur session, always in a fresh terminal.
 *
 * `wilbur.open` deliberately reuses a healthy session -- invoking it from a
 * keybinding should focus the one you have, not spawn a pile of them. This
 * is the explicit "I want another one" path, and it is what makes concurrent
 * sessions possible at all.
 */
function newWilburSession() {
  pruneSessions();
  const n = wilburSessions.length + 1;
  const terminal = launchWilburTerminal(`${TERMINAL_NAME} ${n}`, []);
  if (!terminal) {
    return undefined;
  }
  adoptSession(terminal);
  terminal.show();
  return terminal;
}

/** Pick which live session subsequent sends target, and focus it. */
function focusWilburSession() {
  pruneSessions();
  if (wilburSessions.length === 0) {
    vscode.window.showInformationMessage("Wilbur: no sessions open.");
    return Promise.resolve(undefined);
  }
  if (wilburSessions.length === 1) {
    wilburSessions[0].show();
    return Promise.resolve(wilburSessions[0]);
  }
  const items = wilburSessions.map((t, i) => ({
    label: t.name || `${TERMINAL_NAME} ${i + 1}`,
    description: t === wilburTerminal ? "active -- receives sends" : "",
    _terminal: t,
  }));
  return Promise.resolve(
    vscode.window.showQuickPick(items, {
      placeHolder: "Focus a Wilbur session (it becomes the target for sends)",
    })
  ).then((choice) => {
    if (!choice) return undefined;
    adoptSession(choice._terminal);
    choice._terminal.show();
    return choice._terminal;
  });
}

function openWilbur() {
  // OPEN ALWAYS OPENS. Until 0.4.3 this reused a healthy session, on the
  // reasoning that a keybinding should focus what you have rather than
  // stack terminals up. That was my preference, not the owner's: he
  // reported three times that only one tab would ever open (2026-09-20,
  // "only one wilbur terminal tab opens at a time"). A command called
  // "Open" that silently refuses to open anything is wrong, however
  // defensible the reuse looked. Focusing an existing session is what
  // `wilbur.focusSession` is for.
  pruneSessions();
  if (wilburSessions.length === 0) {
    const { terminal } = getOrCreateTerminal();
    if (!terminal) {
      return undefined;
    }
    terminal.show();
    return terminal;
  }
  return newWilburSession();
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
    // See getOrCreateTerminal()'s comment: never let VS Code revive a
    // resume/continue terminal (or replay anything queued for it) on its own.
    isTransient: true,
  });
  sendLaunchLine(terminal, argv);
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
/** Fetch saved sessions as `wilbur --sessions --json` sees them, or undefined + a shown error. */
function listSavedSessions() {
  const { wilburPath, useFlatpakSpawn } = getConfig();
  if (!isWilburAvailable(wilburPath, useFlatpakSpawn)) {
    warnWilburNotFound(wilburPath);
    return undefined;
  }
  const argv = buildSessionsArgv({ wilburPath, useFlatpakSpawn });
  let result;
  try {
    result = spawnSync(argv[0], argv.slice(1), { encoding: "utf8" });
  } catch (err) {
    vscode.window.showErrorMessage(
      `Wilbur: could not list sessions: ${err.message}`
    );
    return undefined;
  }
  if (result.status !== 0 || !result.stdout) {
    vscode.window.showErrorMessage(
      "Wilbur: could not list sessions (is wilbur.path correct and up to date?)."
    );
    return undefined;
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    vscode.window.showErrorMessage(
      "Wilbur: --sessions --json output was not valid JSON."
    );
    return undefined;
  }
}

const FORGET_BUTTON = {
  iconPath: new vscode.ThemeIcon("trash"),
  tooltip: "Forget this session (delete it; --continue/--resume can never pick it up again)",
};

function sessionToItem(s) {
  return {
    label: (s.objective && s.objective[0]) || "(no objective)",
    description: typeof s.turns === "number" ? `${s.turns} turns` : undefined,
    detail: `${s.cwd || "(unknown cwd)"} · ${relativeTime(s.updated)}`,
    id: s.id,
    buttons: [FORGET_BUTTON],
  };
}

/**
 * Command palette flow: pick a saved wilbur session and resume it, or
 * forget (permanently delete) one via its trash button -- the documented way
 * to clear a stale/queued session so it can never resurface on its own. This
 * never runs, and never deletes anything, without the owner explicitly
 * opening this picker and clicking an item or its trash button.
 */
function sessionsPicker() {
  const sessions = listSavedSessions();
  if (sessions === undefined) {
    return;
  }
  if (!Array.isArray(sessions) || sessions.length === 0) {
    vscode.window.showInformationMessage("Wilbur: no saved sessions found.");
    return;
  }

  const quickPick = vscode.window.createQuickPick();
  quickPick.placeholder = "Resume a Wilbur session, or click the trash icon to forget one";
  quickPick.items = sessions.map(sessionToItem);

  quickPick.onDidTriggerItemButton((event) => {
    const { wilburPath, useFlatpakSpawn } = getConfig();
    const argv = buildForgetArgv({ wilburPath, useFlatpakSpawn }, event.item.id);
    let result;
    try {
      result = spawnSync(argv[0], argv.slice(1), { encoding: "utf8" });
    } catch (err) {
      vscode.window.showErrorMessage(`Wilbur: could not forget session: ${err.message}`);
      return;
    }
    if (result.status !== 0) {
      vscode.window.showErrorMessage(
        `Wilbur: could not forget session ${event.item.id}.`
      );
      return;
    }
    vscode.window.showInformationMessage(`Wilbur: forgot session ${event.item.id}.`);
    quickPick.items = quickPick.items.filter((i) => i.id !== event.item.id);
  });

  quickPick.onDidAccept(() => {
    const choice = quickPick.selectedItems[0];
    quickPick.hide();
    if (!choice) {
      return;
    }
    launchWilburTerminal(`${TERMINAL_NAME}: resume`, ["--resume", choice.id]);
  });
  quickPick.onDidHide(() => quickPick.dispose());
  quickPick.show();
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
    vscode.commands.registerCommand("wilbur.newSession", newWilburSession),
    vscode.commands.registerCommand("wilbur.focusSession", focusWilburSession),
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
      wilburSessions = wilburSessions.filter((s) => s !== t);
      if (!wilburTerminal && wilburSessions.length) {
        // Closing the active session promotes the next live one rather than
        // leaving sends with nowhere to go.
        wilburTerminal = wilburSessions[wilburSessions.length - 1];
      }
    }),
    // onDidCloseTerminal only fires when the TAB closes. A wilbur process
    // that exits on its own leaves the tab open, so without this the
    // singleton stayed pointed at a dead terminal until the window reloaded.
    vscode.window.onDidChangeTerminalState
      ? vscode.window.onDidChangeTerminalState((t) => {
          if (t === wilburTerminal && isDeadTerminal(t)) {
            wilburTerminal = null;
          }
        })
      : { dispose: () => {} },
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
