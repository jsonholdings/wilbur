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

/**
 * Regression coverage for the 2026-09-20 bug the owner reported:
 * "you can only open one session and when you exit that session you cant
 * open a new one without closing and reopening vscode".
 *
 * Exiting wilbur is NOT closing the terminal. When the process ends, VS Code
 * leaves the terminal open as a dead tab: `exitStatus` becomes defined,
 * `onDidCloseTerminal` does NOT fire, and the terminal remains in
 * `window.terminals`. The reuse check therefore saw a live-looking terminal
 * and returned the corpse on every subsequent open, forever, because nothing
 * cleared the singleton short of a window reload.
 *
 * The fix must survive the realistic sequence, which is what this asserts:
 * open -> process exits (tab stays) -> open again must yield a NEW terminal.
 */
test("a second open after the wilbur process exits creates a NEW terminal", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  const openFn = fake.registeredCommands.get("wilbur.open");
  assert.ok(openFn, "wilbur.open must be registered");

  openFn();
  assert.equal(fake.createdTerminals.length, 1, "first open creates a terminal");
  const first = fake.createdTerminals[0];

  // Simulate the real failure mode: the process exits, but the tab lives on
  // and VS Code still lists it. onDidCloseTerminal deliberately does NOT fire.
  first.exitStatus = { code: 0 };
  fake.vscode.window.terminals = [first];

  openFn();
  assert.equal(
    fake.createdTerminals.length,
    2,
    "a dead terminal must be discarded, not reused -- this is the bug that " +
      "forced a full VS Code restart between sessions"
  );
  assert.notEqual(fake.createdTerminals[1], first, "the second open must be a fresh terminal");
});

test("a live session is kept, and a second open joins it rather than replacing it", (t) => {
  // Originally asserted reuse (one terminal). Reversed in 0.4.3: the owner
  // wants Open to open. What still matters is that the FIRST session is not
  // closed or clobbered when a second one starts -- both stay live.
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  const openFn = fake.registeredCommands.get("wilbur.open");
  openFn();
  const first = fake.createdTerminals[0];
  fake.vscode.window.terminals = [first]; // still running, no exitStatus

  openFn();

  assert.equal(fake.createdTerminals.length, 2, "the second open creates its own terminal");
  assert.equal(
    first._calls.dispose ? first._calls.dispose.length : 0,
    0,
    "the existing live session must NOT be disposed when another opens"
  );
});

/**
 * Multi-session coverage (owner, 2026-09-20: "a second session will not open
 * either" / "can we add sessions and others?").
 *
 * Before 0.4.2 the extension held a single terminal and `wilbur.open`
 * returned it forever, so concurrent sessions were impossible BY
 * CONSTRUCTION -- not a bug in the reuse check, a missing capability.
 * `wilbur.open` still reuses a healthy session on purpose (a keybinding
 * should focus what you have, not spawn a pile); `wilbur.newSession` is the
 * explicit "another one" path.
 */
test("wilbur.newSession opens an ADDITIONAL terminal alongside a live one", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  fake.registeredCommands.get("wilbur.open")();
  assert.equal(fake.createdTerminals.length, 1);
  const first = fake.createdTerminals[0];
  fake.vscode.window.terminals = [first]; // still alive

  const newSession = fake.registeredCommands.get("wilbur.newSession");
  assert.ok(newSession, "wilbur.newSession must be registered");
  newSession();

  assert.equal(
    fake.createdTerminals.length,
    2,
    "newSession must create a second terminal even though the first is healthy"
  );
  assert.notEqual(fake.createdTerminals[1], first);
});

test("wilbur.open opens ANOTHER session when one is already live", (t) => {
  // Reversed in 0.4.3. The 0.4.2 version of this test asserted that repeated
  // opens reused the existing terminal -- my design call, and the owner
  // overruled it after reporting three times that only one tab would ever
  // open. A command called "Open" must open. Focusing an existing session is
  // what wilbur.focusSession is for.
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);

  const open = fake.registeredCommands.get("wilbur.open");
  open();
  const first = fake.createdTerminals[0];
  fake.vscode.window.terminals = [first]; // still alive

  open();
  fake.vscode.window.terminals = [...fake.createdTerminals];
  open();

  assert.equal(
    fake.createdTerminals.length,
    3,
    "each Open must yield its own terminal while the previous ones are alive"
  );
});

test("wilbur.focusSession is registered and tolerates having no sessions", (t) => {
  const { extension, fake } = loadExtensionWithFakeVscode();
  activateAndTrack(t, extension);
  const focus = fake.registeredCommands.get("wilbur.focusSession");
  assert.ok(focus, "wilbur.focusSession must be registered");
  return Promise.resolve(focus()).then((r) => {
    assert.equal(r, undefined, "no sessions open must not throw");
  });
});
