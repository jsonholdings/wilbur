"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  isFlatpakSandbox,
  buildLaunchArgv,
  buildCheckArgv,
  buildSessionsArgv,
  buildForgetArgv,
  shellQuote,
} = require("../src/sandbox");

test("isFlatpakSandbox: true when /.flatpak-info exists", () => {
  assert.equal(isFlatpakSandbox(() => true), true);
});

test("isFlatpakSandbox: false when /.flatpak-info is absent", () => {
  assert.equal(isFlatpakSandbox(() => false), false);
});

test("isFlatpakSandbox: false (not throw) if the check itself throws", () => {
  assert.equal(
    isFlatpakSandbox(() => {
      throw new Error("EPERM");
    }),
    false
  );
});

test("buildLaunchArgv: auto + sandboxed prefixes flatpak-spawn --host", () => {
  const argv = buildLaunchArgv({
    wilburPath: "/home/user/.local/bin/wilbur",
    useFlatpakSpawn: "auto",
    detectSandbox: () => true,
  });
  assert.deepEqual(argv, [
    "flatpak-spawn",
    "--host",
    "/home/user/.local/bin/wilbur",
  ]);
});

test("buildLaunchArgv: auto + not sandboxed runs wilbur directly", () => {
  const argv = buildLaunchArgv({
    wilburPath: "/home/user/.local/bin/wilbur",
    useFlatpakSpawn: "auto",
    detectSandbox: () => false,
  });
  assert.deepEqual(argv, ["/home/user/.local/bin/wilbur"]);
});

test("buildLaunchArgv: useFlatpakSpawn=on always prefixes, even off-sandbox", () => {
  const argv = buildLaunchArgv({
    wilburPath: "wilbur",
    useFlatpakSpawn: "on",
    detectSandbox: () => false,
  });
  assert.deepEqual(argv, ["flatpak-spawn", "--host", "wilbur"]);
});

test("buildLaunchArgv: useFlatpakSpawn=off never prefixes, even on-sandbox", () => {
  const argv = buildLaunchArgv({
    wilburPath: "wilbur",
    useFlatpakSpawn: "off",
    detectSandbox: () => true,
  });
  assert.deepEqual(argv, ["wilbur"]);
});

test("buildLaunchArgv: extra args pass through after the wilbur path", () => {
  const argv = buildLaunchArgv({
    wilburPath: "wilbur",
    useFlatpakSpawn: "off",
    args: ["-C", "/some/dir"],
  });
  assert.deepEqual(argv, ["wilbur", "-C", "/some/dir"]);
});

test("buildCheckArgv: not sandboxed uses a plain sh -c command -v check", () => {
  const argv = buildCheckArgv({
    wilburPath: "wilbur",
    useFlatpakSpawn: "off",
  });
  assert.deepEqual(argv, ["sh", "-c", "command -v 'wilbur' >/dev/null 2>&1"]);
});

test("buildCheckArgv: sandboxed prefixes flatpak-spawn --host", () => {
  const argv = buildCheckArgv({
    wilburPath: "/abs/path/wilbur",
    useFlatpakSpawn: "on",
  });
  assert.deepEqual(argv, [
    "flatpak-spawn",
    "--host",
    "sh",
    "-c",
    "command -v '/abs/path/wilbur' >/dev/null 2>&1",
  ]);
});

test("buildSessionsArgv: not sandboxed returns plain wilbur --sessions --json argv", () => {
  const argv = buildSessionsArgv({
    wilburPath: "wilbur",
    useFlatpakSpawn: "off",
  });
  assert.deepEqual(argv, ["wilbur", "--sessions", "--json"]);
});

test("buildSessionsArgv: sandboxed prefixes flatpak-spawn --host", () => {
  const argv = buildSessionsArgv({
    wilburPath: "/abs/path/wilbur",
    useFlatpakSpawn: "auto",
    detectSandbox: () => true,
  });
  assert.deepEqual(argv, [
    "flatpak-spawn",
    "--host",
    "/abs/path/wilbur",
    "--sessions",
    "--json",
  ]);
});

test("buildForgetArgv: not sandboxed returns plain wilbur --forget ID argv", () => {
  const argv = buildForgetArgv({ wilburPath: "wilbur", useFlatpakSpawn: "off" }, "Projects-123");
  assert.deepEqual(argv, ["wilbur", "--forget", "Projects-123"]);
});

test("buildForgetArgv: sandboxed prefixes flatpak-spawn --host", () => {
  const argv = buildForgetArgv(
    { wilburPath: "/abs/path/wilbur", useFlatpakSpawn: "auto", detectSandbox: () => true },
    "Projects-123"
  );
  assert.deepEqual(argv, ["flatpak-spawn", "--host", "/abs/path/wilbur", "--forget", "Projects-123"]);
});

test("shellQuote: wraps and escapes single quotes", () => {
  assert.equal(shellQuote("it's fine"), "'it'\\''s fine'");
  assert.equal(shellQuote("/plain/path"), "'/plain/path'");
});
