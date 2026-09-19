"use strict";
/**
 * Regression coverage for the 2026-09-19 bug: opening Wilbur in VS Code must
 * never, by itself, spawn a process, run a shell command, or resume a past
 * session's work. Only an explicit command invocation (the owner clicking
 * "Wilbur: Open", "Wilbur: Resume last session", etc.) may do any of that.
 *
 * `extension.js` requires the real `vscode` module, which only exists inside
 * the extension host. This test stubs it via Module._load so extension.js's
 * real activation/command code runs against a fake, observable API surface.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const path = require("node:path");

const EXTENSION_PATH = path.join(__dirname, "..", "src", "extension.js");

/** A minimal fake `vscode` module: just enough surface for extension.js to load and run. */
function makeFakeVscode() {
  const registeredCommands = new Map();
  const createdTerminals = [];
  const configValues = {
    "wilbur.path": "wilbur",
    "wilbur.useFlatpakSpawn": "off", // avoid touching /.flatpak-info in CI
    "wilbur.model": "",
    "wilbur.approvalMode": "manual",
    "wilbur.contextSize": 0,
  };

  function fakeTerminal() {
    const calls = { sendText: [], show: 0 };
    return {
      name: "fake",
      sendText(text, addNewLine) {
        calls.sendText.push({ text, addNewLine });
      },
      show() {
        calls.show += 1;
      },
      dispose() {},
      _calls: calls,
    };
  }

  const vscode = {
    StatusBarAlignment: { Right: 1 },
    TerminalLocation: { Editor: 1 },
    CodeActionKind: { QuickFix: "quickfix" },
    TreeItemCollapsibleState: { None: 0 },
    ThemeColor: class ThemeColor {
      constructor(id) { this.id = id; }
    },
    ThemeIcon: class ThemeIcon {
      constructor(id) { this.id = id; }
    },
    CodeAction: class CodeAction {
      constructor(title, kind) { this.title = title; this.kind = kind; }
    },
    TreeItem: class TreeItem {
      constructor(label, state) { this.label = label; this.state = state; }
    },
    Uri: {
      file: (p) => ({ fsPath: p, path: p }),
      joinPath: (base, ...segs) => ({ fsPath: [base.fsPath, ...segs].join("/") }),
    },
    workspace: {
      workspaceFolders: [{ uri: { fsPath: "/fake/workspace" } }],
      getConfiguration: () => ({
        get: (key, def) => {
          const full = `wilbur.${key}`;
          return full in configValues ? configValues[full] : def;
        },
      }),
      asRelativePath: (uri) => (uri && uri.fsPath) || String(uri),
    },
    languages: {
      registerCodeActionsProvider: () => ({ dispose() {} }),
      getDiagnostics: () => [],
    },
    commands: {
      registerCommand: (name, fn) => {
        registeredCommands.set(name, fn);
        return { dispose() {} };
      },
      executeCommand: () => {},
    },
    window: {
      createStatusBarItem: () => ({ show() {}, dispose() {} }),
      createTerminal: (opts) => {
        const t = fakeTerminal();
        t._opts = opts;
        createdTerminals.push(t);
        return t;
      },
      terminals: [],
      onDidCloseTerminal: () => ({ dispose() {} }),
      registerTreeDataProvider: () => ({ dispose() {} }),
      showErrorMessage: () => Promise.resolve(undefined),
      showWarningMessage: () => Promise.resolve(undefined),
      showInformationMessage: () => Promise.resolve(undefined),
      showQuickPick: () => Promise.resolve(undefined),
      createQuickPick: () => ({
        items: [],
        show() {},
        hide() {},
        dispose() {},
        onDidTriggerItemButton() {},
        onDidAccept() {},
        onDidHide() {},
      }),
      activeTextEditor: undefined,
    },
  };

  return { vscode, registeredCommands, createdTerminals };
}

// extension.js's status-bar poll calls the real `http` module to reach
// Ollama. In a test that has no bearing on the activation bug and, worse,
// can leave a real socket open past the test run (this workstation actually
// runs Ollama on :11434) and hang node --test waiting for it to close. Stub
// it so `activate()` never touches the network.
function fakeHttpModule() {
  return {
    get(_url, _opts, cb) {
      const req = {
        on(event, handler) {
          if (event === "error") {
            setImmediate(() => handler(new Error("stubbed: no network in tests")));
          }
          return req;
        },
        destroy() {},
      };
      return req;
    },
  };
}

/** Load extension.js fresh against a fake vscode module. Returns { extension, fake }. */
function loadExtensionWithFakeVscode() {
  const fake = makeFakeVscode();
  const originalLoad = Module._load;
  Module._load = function (request, ...rest) {
    if (request === "vscode") {
      return fake.vscode;
    }
    if (request === "http") {
      return fakeHttpModule();
    }
    return originalLoad.call(this, request, ...rest);
  };
  delete require.cache[require.resolve(EXTENSION_PATH)];
  delete require.cache[require.resolve("../src/sandbox.js")];
  let extension;
  try {
    extension = require(EXTENSION_PATH);
  } finally {
    Module._load = originalLoad;
  }
  return { extension, fake };
}

// Every isWilburAvailable() check in extension.js shells out via spawnSync to
// `command -v wilbur`. In this sandbox that's harmless and deterministic
// (either found or not), so we don't stub child_process -- we only assert on
// vscode-visible side effects (terminals created, text sent), which is what
// the bug was actually about.

/** activate() starts a 30s status-bar poll interval; every test must clear it or node --test hangs. */
function activateAndTrack(t, extension) {
  const context = { subscriptions: [] };
  extension.activate(context);
  t.after(() => extension.deactivate());
  return context;
}

test("activate(): registers commands but performs no work (no terminal, no process)", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  assert.equal(fake.createdTerminals.length, 0, "activate() must not create a terminal");
  assert.ok(fake.registeredCommands.has("wilbur.open"), "wilbur.open must be registered");
  assert.ok(fake.registeredCommands.has("wilbur.resumeLast"), "wilbur.resumeLast must be registered");
});

test("negative control: activate() twice still creates zero terminals", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  // Two activations in a row (each must clear its own interval, or the first
  // leaks and this test hangs node --test on exit).
  extension.activate({ subscriptions: [] });
  extension.deactivate();
  activateAndTrack(t, extension);
  assert.equal(fake.createdTerminals.length, 0);
});

test("explicit wilbur.open creates a transient terminal and delays the launch line", (t, done) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  const openFn = fake.registeredCommands.get("wilbur.open");
  assert.ok(openFn, "wilbur.open must be registered");
  openFn();

  assert.equal(fake.createdTerminals.length, 1, "explicit open creates exactly one terminal");
  const term = fake.createdTerminals[0];
  assert.equal(term._opts.isTransient, true,
    "the terminal must be isTransient so VS Code never revives/replays it across a reload");
  assert.equal(term._calls.sendText.length, 0,
    "the launch line must not be sent synchronously -- it races other extensions' terminal writes");

  setTimeout(() => {
    assert.equal(term._calls.sendText.length, 1, "the launch line is sent after the guard delay");
    done();
  }, 500);
});

test("wilbur.resumeLast is an explicit command, not something activate() calls on its own", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);
  // activate() alone must not have resumed anything.
  assert.equal(fake.createdTerminals.length, 0);

  const resumeFn = fake.registeredCommands.get("wilbur.resumeLast");
  resumeFn();
  assert.equal(fake.createdTerminals.length, 1, "resumeLast only launches once explicitly invoked");
  assert.equal(fake.createdTerminals[0]._opts.isTransient, true);
});
