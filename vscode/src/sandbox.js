"use strict";
/**
 * Sandbox / launch-command detection, kept separate from extension.js so it
 * can be unit-tested with node's built-in test runner without the `vscode`
 * module being resolvable.
 */
const fs = require("fs");

/**
 * @param {() => boolean} [existsFn] override for testing
 * @returns {boolean} true if this process is running inside a Flatpak sandbox
 */
function isFlatpakSandbox(existsFn) {
  const exists = existsFn || (() => fs.existsSync("/.flatpak-info"));
  try {
    return exists();
  } catch {
    return false;
  }
}

/**
 * Build the argv (as an array: [command, ...args]) used to launch wilbur.
 *
 * @param {object} opts
 * @param {string} opts.wilburPath path to the wilbur executable
 * @param {"auto"|"on"|"off"} opts.useFlatpakSpawn setting value
 * @param {string[]} [opts.args] extra args to pass to wilbur
 * @param {() => boolean} [opts.detectSandbox] override for testing
 * @returns {string[]}
 */
function buildLaunchArgv(opts) {
  const { wilburPath, useFlatpakSpawn, args = [], detectSandbox } = opts;
  const needsSpawn =
    useFlatpakSpawn === "on" ||
    (useFlatpakSpawn === "auto" && isFlatpakSandbox(detectSandbox));

  if (needsSpawn) {
    return ["flatpak-spawn", "--host", wilburPath, ...args];
  }
  return [wilburPath, ...args];
}

/** Quote a single shell argument for a POSIX shell (used when sending text into a terminal). */
function shellQuote(arg) {
  return `'${String(arg).replace(/'/g, `'\\''`)}'`;
}

/**
 * Build the argv used to check whether `wilburPath` resolves to something
 * runnable (an absolute path that exists, or a bare name found on PATH),
 * honoring the same flatpak-spawn detection as buildLaunchArgv so the check
 * runs in the same namespace the real launch would.
 *
 * @param {object} opts same shape as buildLaunchArgv's opts
 * @returns {string[]}
 */
function buildCheckArgv(opts) {
  const { wilburPath, useFlatpakSpawn, detectSandbox } = opts;
  const needsSpawn =
    useFlatpakSpawn === "on" ||
    (useFlatpakSpawn === "auto" && isFlatpakSandbox(detectSandbox));
  const inner = `command -v ${shellQuote(wilburPath)} >/dev/null 2>&1`;
  if (needsSpawn) {
    return ["flatpak-spawn", "--host", "sh", "-c", inner];
  }
  return ["sh", "-c", inner];
}

/**
 * Build the argv used to list wilbur's saved sessions as JSON
 * (`wilbur --sessions --json`), honoring the same flatpak-spawn detection as
 * buildLaunchArgv/buildCheckArgv so it runs in the same namespace the real
 * launch would. Unlike buildCheckArgv this is a plain argv array (no shell
 * wrapping), so it can be passed straight to spawnSync.
 *
 * @param {object} opts same shape as buildLaunchArgv's opts (args is ignored)
 * @returns {string[]}
 */
function buildSessionsArgv(opts) {
  return buildLaunchArgv({ ...opts, args: ["--sessions", "--json"] });
}

/**
 * Build the argv used to delete a saved session (`wilbur --forget ID`), so a
 * stale/queued session can never be picked up again by `--continue` or shown
 * again by `--sessions`. Same shape as buildSessionsArgv.
 *
 * @param {object} opts same shape as buildLaunchArgv's opts (args is ignored)
 * @param {string} id the session id to forget
 * @returns {string[]}
 */
function buildForgetArgv(opts, id) {
  return buildLaunchArgv({ ...opts, args: ["--forget", id] });
}

module.exports = {
  isFlatpakSandbox,
  buildLaunchArgv,
  buildCheckArgv,
  buildSessionsArgv,
  buildForgetArgv,
  shellQuote,
};
