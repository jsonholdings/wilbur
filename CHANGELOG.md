# Changelog

Earlier history lives in `git log`; this file starts at 2.4.0.


## 2.37.3
- gpu-lease re-vendored at jh-gpu-lease 0.4.3: a SIGTERM/SIGHUP to a process holding an ADMITTED lease now completes the lease at once instead of leaving gq to expire it (only where the signal is still at its default action; `wilbur loop` keeps its own stop handler).

## 2.37.2
- `contrib/wilbur-watch/watch.sh` (private build): the health watchdog reads `wilbur status --check` instead of guessing from session-file age and `pgrep`, logs each run row, notifies once per transition into ALERT, and exits 0/1/2 (a missing wilbur binary is UNKNOWN). Sample unit `contrib/systemd/wilbur-watch.service`. Also ships VS Code extension 0.5.0 (a "Wilbur" terminal profile with the pig tab icon) and agent-team 0.3.0 docs/config.

## 2.37.1
- Publish fix: `tests/test_tab_identity.py` used a `/home/...` sample path, which the public-mirror scrub check rejects, so 2.37.0 reached the private origin but not the public mirror. The fixture now uses `/srv/proj`; no behaviour change.

## 2.37.0
- The terminal tab identifies Wilbur for the whole run: at start it reads `🐖 Wilbur · <cwd name> · <model>` (OSC 2 title plus OSC 1 icon name, the pig as the portable icon); while a turn runs it reads `🐖 Wilbur: working (n)` and on stop `🐖 Wilbur: stopped (<reason>) HH:MM`, so "Wilbur" is always in the tab. The previous title is pushed to the xterm title stack at start and restored on exit, error paths included (terminals without a title stack keep the last title). No escape sequences are written when stdout is not a terminal or TERM is empty/dumb.

## 2.36.0
- `run.json` now carries `metrics`, the JSON Holdings shared agent-run record (`jh-agent-metrics` 0.1.0, schema 1), so the Home Base Agents board reads Wilbur and Claude runs with one schema and one token math: tokens (cache fields UNKNOWN, since ollama has no prompt cache), tokens/min and fresh/min over the last 5 minutes from a short cumulative sample ring (`metrics_samples`, also written), context (the last call's prompt size), calls, subagents and state. `usage` gains `context` (null before the first model call). The public build has no JH registry: the import is optional and run.json then has no `metrics` key. Private build pins the package in `requirements-gpu-queue.txt`; `tests/test_agent_metrics_pin.py` (not mirrored) fails on a version mismatch. A fault in the metrics path never costs the run.json write.

## 2.35.2
- Tests: the suite can no longer write into the owner's real `~/.local/share/wilbur/sessions` or `~/.config/wilbur`. `tests/conftest.py` now points `WILBUR_CONFIG`, `WILBUR_SESSIONS`, `WILBUR_HISTORY` and `WILBUR_USER_SKILLS_DIR` at a throwaway directory before `wilbur.config` is imported, so a thread that outlives its test, or a subprocess a test starts, falls back to that directory instead of the real one (a `test_idle_exit_fires_after_a_h0-*.progress.md` had leaked into the real sessions dir). A session-wide guard fails the run if any `test_*` file appears in the real sessions dir; `tests/test_home_isolation.py` covers the redirect and proves the guard can fire.

## 2.35.1
- Publishing: `tests/test_jh_source.py` is no longer copied to the public mirror. It imports `voice/`, which the mirror does not carry, so the mirror's test run failed at collection and 2.33.2–2.35.0 never reached the public repo.

## 2.35.0

Bounded message check. A line typed while Wilbur works was read only when the whole
turn ended (up to 60 tool rounds, each a model call of up to 10 min, plus `/loop`
turns and long `sleep` commands), so a message could sit unread for hours. Now it is
read within `message_check_max_s` (default 60) at every safe point: the agent injects
plain lines into the conversation between tool rounds, a slash command ends the turn
at once so the REPL runs it, `run_bash` cuts a `sleep`/wait command short when a message
is pending, `/loop` hands back to the prompt between turns, and a watcher thread keeps
run.json's `unread_message_since` current. `wilbur status --check` exits 1 and reports
the age when a message is unread for more than twice the bound. A model call already in
progress (up to `request_timeout`) or a build/test command still runs to its end first. End of input (piped stdin) is not a
message and never pauses a turn. A line read mid-turn still stops keep-going, like a
line left queued.


## 2.34.0

Outside readers can see elapsed time, tokens and subagents. `run.json` now carries `started_at` (the run's first state write) and `usage` (cumulative `prompt_tokens`, `completion_tokens`, `total_tokens`, model `calls` and `seconds`), refreshed after every model call without resetting `since`. The subagent registry is mirrored to `<session>.subagents.json` (id, label, kind, state, started, ended, round, current, last_tool), written atomically on every transition and removed with `run.json` on a clean exit; `wilbur status --json` includes each run's `subagents`. The dead `parallel_tools` config switch is removed (no code read it; an old config file that still sets it is ignored). `run.json` also records `surface` (`cli` or `vscode`; VS Code extension 0.4.5 sets `WILBUR_SURFACE=vscode` on its terminals) so the Agents board can label Wilbur CLI vs Wilbur VS Code.


## 2.33.2

The voice transcribe asset now resolves from `$JH_SOURCE_DIR`, else the clean origin
mirror (`jsonholdings-origin-mirror`), instead of the shared JSON Holdings checkout,
which lags origin and holds other sessions' edits. `$WILBUR_TRANSCRIBE_ASSET` still
overrides it. `scripts/voice_bench.py` exits 2 (UNKNOWN) when the asset is missing.


## 2.33.1

The idle-exit wait has its own method (`Repl._next_line_within`), and its tests wait for the stop instead of a fixed 3 s, so they pass on the slower public-mirror gate.


## 2.33.0

A stopped run can no longer look alive. Each run writes `<session>.run.json` atomically on every state change (working, waiting for approval, waiting for input, stopped with a reason) and removes it on a clean exit; a file whose process is gone reads as `dead`. A stop rings the bell once, prints a `STOPPED — <reason>` banner and sets the terminal title (config `stop_bell`, `set_title`). New `wilbur status [--check] [--json]` reads those files with no model: `--check` exits 1 for a hung or dead run and 2 when it cannot look. Two empty model replies in a row now stop keep-going with reason `empty_reply` instead of nudging on. Optional `idle_exit_minutes` saves and exits a stopped run that had a stated horizon.


## 2.32.2

`tests/test_image_tool.py` skips its two gpu-queue backend tests when the private `jh_gpu_queue_client` package is absent, so the public mirror's suite passes.


## 2.32.1

VS Code extension 0.4.4: its test suite passes without the wilbur CLI on PATH, so the public CI is green again. `scripts/publish.sh` runs the gate, pushes the source, syncs and pushes the public mirror and waits for its CI; `--check` reports drift.


## 2.32.0

`FEATURES.pdf` in the repo root, built from the code by `docs/build_feature_reference.py` through the shared JSON Holdings feature-reference generator: command-line flags, interactive commands, model tools, config switches, scripts, and what is built but unproven. Rebuild or check it with `tb python3 docs/build_feature_reference.py [--check]`.


## 2.31.0

Generated test matrix. `scripts/test_matrix.py` writes `TEST-MATRIX.md`, listing every tool and boolean config switch with the test files that exercise it (all on the fake backend); `--check`, run by the gate, fails when the file is stale or a row has no test. New tests cover the `verify_done_claims` and `progress_log` switches. `parallel_tools` is flagged as a switch no code reads.


## 2.30.0

Remote GPU host support. New README section "Use a GPU on another machine" (SSH tunnel, `--no-models --no-gpu` install, secret placement, the port-11434 clash). `contrib/remote-gpu/` adds a systemd `--user` tunnel unit and `wilbur-remote-check --check`, which reports tunnel, Ollama, gpu-queue and secret as OK, FAIL or UNKNOWN. When Ollama is reachable but no gpu-queue secret is usable, Wilbur warns once at startup that GPU work is bypassing gpu-queue; grant behaviour is unchanged.


## 2.29.0

Opt-in grammar-constrained tool calls. Set `"constrained_tool_calls": true` and each Ollama request that offers tools carries a `format` JSON schema (a `content` string plus `tool_calls` whose names are limited to the offered tools), so the model cannot emit a malformed call. A reply that ignores `format` falls back to the existing recovery path, which stays until it is measured on `huihui_ai/qwen2.5-coder-abliterate:32b`.


## 2.28.0

Opt-in `generate_image` tool. Off by default; set `"image_generation": true`. It submits an `image_generate` job to gpu-queue (never the GPU directly) and saves the returned PNG inside the project after approval. `image_model` defaults to `qwen-image-2512` (Apache-2.0); `qwen-image-2.1` is allowed and carries a research-only licence warning in the approval prompt and result. gpu-queue has no `image_generate` kind yet, so today the tool returns a clear "no kind registered" error; the job contract is in `wilbur/tools/image.py`.


## 2.27.0

Opt-in critic pass. Set `"critic_pass": true` in `~/.config/wilbur/config.json` and each successful `write_file`/`edit_file` gets one extra model call that reviews the change; a `PROBLEM:` verdict is appended to the tool result so the agent re-reads and fixes it. Off by default (about one extra call per write); a failed critic call never fails the write.


## 2.26.1

A timed-out subagent is now told to stop. The `task` tool sets the subagent's cancel event when its `subagent_timeout_s` expires, so the abandoned subagent exits at its next round or tool-call checkpoint instead of looping through `max_turns` in the background.


## 2.26.0

One-shot runs are saved. `wilbur -p` now writes a session (always a new id, never the directory's latest, so a script cannot inherit a stale plan), saved even when the run fails, so `--continue` resumes it.


## 2.25.12

Fixed the last timing flake. `test_repeated_identical_done_claims_still_trip_the_stuck_detector`
ran five real pytest subprocesses inside the 10 s thread-join cap of the keep-going
harness; at load 8 that took up to 8 s. The test now refutes each claim with a
stubbed `verify.run` (0.35 s under the same load); the real subprocess path stays
covered by the other done-claim tests.


## 2.25.11

Deterministic test suite. The ptk pump tests stop the pump with Ctrl-D and join it
instead of closing the pipe under a running prompt, which left the pump alive and
broke a later test. The sign-off tests wait for the main loop to block for its next
command, so `/exit` can no longer reach the queue before the sign-off decision.
Every stdin, pty, subagent and team-role thread a test starts is now stopped and
joined in teardown, and a leaked thread fails the test (`pytest.ini`). No code changes.

## 2.25.10

Ten practice-loop test files: catalog search, fit report, GPU VRAM parsing, file
resolution, the ListFiles, Glob, Grep and Bash tools (edge cases), shell program tokens and
UI rendering. No code changes.

## 2.25.9

New practice-loop tests: `tests/test_state_roundtrip.py`,
`tests/test_doneclaim_helpers.py`, `tests/test_skills_tool.py` and two more cases in
`tests/test_context_orphans.py`. No code changes.

## 2.25.8

`scripts/sync_to_mirror.py --apply` now rebuilds the mirror from the allow-list plus
the mirror-only files, so a file outside the allow-list (such as a local `build/lib/`)
is deleted instead of carried forward. New tests: `tests/test_transient_errors.py`
(practice loop) and `tests/test_sync_mirror_prunes.py`.

## 2.25.7

The sudo guard's loop variable is renamed from `token` to `word`: the mirror
scrub reads `token = <value>` as a secret-shaped assignment. Behaviour unchanged.

## 2.25.6

The sudo guard's env-assignment check no longer uses a `NAME=` regex, which the
public mirror's scrub flagged as secret-shaped. Behaviour unchanged.

## 2.25.5

- **Docs:** one-shot runs are not saved to session history (stated in the docs).
- **Tests:** new coverage for progress, tools base, config round-trip,
  models info and lenient JSON parsing.

## 2.25.4

- **`run_bash` refuses privilege elevation without running it.** Any command
  whose program position is `sudo`, `su`, `doas`, `pkexec`, `run0` or
  `sudoedit` (also after `&&`, `;`, `|`, `$(`, a backtick, `env`/`command`/
  `timeout`-style wrappers, an env assignment, or a full path) returns an
  error telling the model root is unavailable and not to retry. The loop model
  had been running `sudo /usr/bin/true` in every task, and each attempt raised
  a host security alert. The baked system rules gain the same line.
- **`install.sh --check` no longer probes sudo.** The actual source of the
  root-mail alerts: `sudo -n true` in the privilege probe logs "a password is
  required" for `/usr/bin/true`, and the test suite (run in every loop task)
  calls `install.sh --check`. Check mode installs nothing and now skips the
  probe; `WILBUR_NO_SUDO=1` skips it for a real install too.
- `wilbur/HANDOFF-INBOX.md` moved to the repo root so a mirror sync from the
  main checkout is not blocked by an untracked file.

## 2.25.3

Tests only. Merges nine unit-test modules from Wilbur's practice loop:
`test_art`, `test_balanced_json`, `test_catalog_names`, `test_contrib_queues`,
`test_loop_helpers`, `test_near_miss_hint`, `test_registry_walk`,
`test_todo_tool`, `test_ui_helpers` (550 tests total). No source changes.

## 2.25.2

Fixes from the review of PR #2 (REVIEW-wilbur-pr1-fixes.md, grade B-,
MERGE-WITH-FIXES). Supersedes PR #2 and PR #1: this release contains both.
Still a diagnostic, not a fix, for the reported `test_doneclaim.py` failures
and 150 s runtime, which remain unreproduced off the host (see
FIXES-wilbur-pr1-fixes.md).

- **The leak guard restores what leaked.** `_no_leaked_state` puts every
  leaked piece of state back (env, cwd, `sys.stdin`, SIGINT, the patched
  module globals, the fake backend's shared queues) before failing the test,
  so a leak no longer cascades into the next test.
- **Thread report.** The guard emits a `LeakedThreadWarning` naming every
  thread a test left running (stdin pumps, agent turns). It does not fail the
  test; the warnings summary is what a host rerun should read.
- **Fake-backend scripts are fully validated on load.** A non-string
  `content` or `thinking`, or an invalid `exhausted` (e.g. `null`), is a
  `FakeBackendError` at load time instead of a crash partway through a run.
- **No gpu-queue under the fake backend.** A REPL on `backend: fake` never
  loads the gpu-priority client, so a scripted turn cannot wait behind a GPU
  render. README documents that `/model`, `/search`, `/pull` and `/context`
  still talk to Ollama.
- **Tests.** tests/test_leak_guard.py now covers all 8 guarded keys and their
  restore, and uses `pytester` to show the guard ERRORs a leaking test at
  teardown, that the next test then passes, that monkeypatch is undone before
  the guard checks, and that a left-over thread is warned about. 5 more
  tests in tests/test_fake_backend.py. Full suite (without the private-build
  test_gpu_priority.py): 485 passed in file order and in 4 random orders
  (seeds 101/202/303/404).

## 2.25.1

Follow-up to 2.25.0 (PR #1). Review report: `tests/test_doneclaim.py` failed
3 tests in the full suite but passed alone, and the suite went from about
55 s to 150 s. This adds a diagnostic for that report; it does not fix it,
since the failures were never reproduced.

- **Leak guard.** New autouse fixture `_no_leaked_state` (tests/conftest.py)
  snapshots process-wide state before each test and fails the test that
  leaves it changed: env vars, cwd, `sys.stdin`, the SIGINT handler,
  `wilbur.agent.OllamaClient`, `wilbur.config.CONFIG_PATH`,
  `wilbur.verify.detect` and the fake backend's shared script queues. A leak
  is reported on the test that caused it, not on whichever test runs next.
- **Fixture ordering.** `_fresh_fake_backend` now requests the guard, so it
  clears `wilbur.fake_backend._shared` before the guard checks it. The guard
  found that 4 tests in test_fake_backend.py otherwise still had a queue
  registered when the guard checked.
- **test_repl_runs_on_the_fake_backend_without_ollama** closes the stdin pipe
  it opens instead of leaving the fd open.
- **Tests.** `tests/test_leak_guard.py` (3) shows the guard names each kind
  of leak. Full suite (without the private-build test_gpu_priority.py):
  477 passed in file order (32 s) and in 3 random orders (pytest-randomly,
  seeds 101/202/303/404; 29-32 s), against 30 s for main. The reported
  test_doneclaim failures and the 150 s runtime did not reproduce in a
  clean environment; no fake-backend test sleeps or waits for a timeout
  when it passes, so no timeout was shortened.

## 2.25.0

A deterministic fake model backend, so the whole suite (and a scripted run of
the real CLI) needs no GPU, no Ollama and no network.

- **`wilbur/fake_backend.py`.** `FakeClient` implements `OllamaClient.chat()`'s
  interface and returns scripted replies in order, recording every request.
  It moved here from `tests/conftest.py` (where it was test-only, with two
  more copies in `tests/test_agent.py` and `tests/test_incremental_save.py`
  and an import from `tests.conftest` in `bench/runner.py`); all now use the
  one in the package. A script entry written as text goes through the same
  `recover_tool_calls()` step as real model output.
- **Selected by configuration, never silently.** New settings `backend`
  (`"ollama"` default, or `"fake"`) and `fake_script` (a JSON list of turns),
  and matching `--backend` / `--fake-script` flags. The CLI prints
  `FAKE BACKEND` to stderr on every run that uses it; an unknown backend, or
  `fake` with no script, exits 2; an unreachable Ollama never falls back to
  the fake. `Config.save()` never writes the backend from a running session
  (only a hand edit of config.json changes it). Under `fake` the CLI and REPL skip the Ollama/VRAM checks.
  `Agent` builds its client through `make_client()`, and a parent agent, its
  subagents and team roles share one script queue.
- **Tests.** `tests/test_fake_backend.py` (25): the client, script loading and
  validation, selection rules, and full runs through `Agent`, a `task`
  subagent, `Repl.run` and a `wilbur -p` subprocess pointed at a dead port.
  New fixture `fake_backend_config` selects the backend by configuration. No
  test was skipped for lack of a model before this change, so none needed
  converting.

## 2.24.1

Never accept "done" without evidence.

- **Done-claim verifier** (new `wilbur/doneclaim.py`). When a turn ends with a
  completion claim, or with no tool call, while todo items are still open,
  Wilbur runs the project's own check (the same detection as the post-write
  check: `make test`, `npm test`, pytest, ...) and checks that every file named
  in an open item exists. If the check passes and the model claimed
  completion, the open items are ticked and the run stops as COMPLETE with the
  evidence shown. If it fails, the next nudge is specific: the failing command,
  the tail of its output, the exact open items, and "don't claim completion
  without running the check" — not the generic "continue".
- **Claims guard.** A reply that says the tests pass when no test command ran
  in that turn (neither the model's `run_bash` nor the harness's own check) is
  flagged "unverified claim" in the transcript and triggers the verifier.
- A repeated identical done-claim still counts as no progress for the stuck
  detector; after the verifier passes, the run stops instead of spinning.
- New setting `verify_done_claims` (default on).

## 2.24.0

Long runs: "if I tell him to work all night on something, he should do it."

- **The run horizon replaces the keep-going caps.** By default Wilbur now runs
  until the task's todo plan is complete, not for a fixed 10 continues / 30
  minutes. State a horizon in plain English and he honours it: "all night" /
  "overnight" (bounded by a 12-hour safety maximum, `run_safety_max_minutes`),
  "for 3 hours" / "for 90 minutes", or "until the tests pass". The old numeric
  caps (`auto_continue_max`, `auto_continue_max_minutes`) still apply when set
  > 0, but default to 0 (off). New module `wilbur/horizon.py`.
- **The round cap is no longer a stop.** When a message reaches `max_turns`
  tool rounds, Wilbur writes a checkpoint (what's done / left / next), the
  context is compacted if near the window limit, and the run carries on
  automatically instead of ending the turn.
- **A stuck detector is the safety net**, not a continue count: the run stops
  after `stuck_limit` (default 5) consecutive turns with no meaningful progress
  — no files changed, no new verification, the same error or tool call repeated
  — and reports what was tried.
- **Durability for unattended runs.** In a git repo Wilbur works on a
  `wilbur/run-<session>` branch and commits tested checkpoints as it goes
  (`checkpoint_commit_every`, default 10; never pushes). A progress log for the
  morning is written to `~/.local/share/wilbur/sessions/<session>.progress.md`.
  A run resumes with `--continue`.
- **Stall (b) fixed:** a thinking-capable local model that returned a message
  with everything in `thinking` and an empty `content` (and no tool call) made
  the turn end with no reply at all. The agent loop now recovers it in place —
  it nudges once for a concrete action or answer, and falls back to surfacing
  the thinking rather than going silent.

## 2.23.1

- Keep going now fires where the owner really had to step in, measured offline on two saved
  long sessions (counts only, no message text kept). Most mid-task stops were an empty reply
  right after tool work. 2.23.0 treated any empty reply as finished; it now continues when the
  turn called tools ("went quiet mid-task"), and a second empty reply after the nudge still
  counts as a stall.
- A reply that only asks leave to carry on with the request ("Shall I continue with the next
  item?", "Want me to proceed?") now continues. A real choice ("A or B?", "which ..."), an
  offer of new scope, or a question after all items are done still stops.

## 2.23.0

- Keeps going: a plain multi-item prompt no longer stops after the first item. When a turn
  ends with the model's todo plan still open, or a "Next, I'll ..." with no tool call, the REPL
  sends a neutral "continue with the next item" nudge and shows `↻ continuing (n/N)`. It stops
  on all items done, a question to the user, a refused or unanswered approval, `Ctrl-C`, a line
  the user typed, a continue with no tool call, or the cap (`auto_continue_max` 10,
  `auto_continue_max_minutes` 30). On by default; `/keepgoing off` or `"auto_continue": false`
  turns it off. Approvals are never granted by the nudge.

## 2.22.5

- gpu-lease pinned to jh-gpu-lease 0.4.1: a loop lease that is queued or held when gq restarts
  (a gq deploy) reconnects with backoff and keeps its place instead of failing with
  "connection refused".

## 2.22.4

- Team mode: the `reviewer_model` VRAM fallback note is no longer dropped. It is kept on the
  result (`reviewer_model_note`), written to the loop results row and log, and shown in the REPL.
- Team mode: every reviewer verdict is recorded in order (`verdicts`, e.g. DENY -> APPROVE) on
  the result, the loop results row and the loop's roles log line.
- `/team auto`: the sizing heuristic was re-tuned on 18 labelled prompts from this repo's BACKLOG
  and git history (11/18 -> 18/18 on that set). Abbreviations such as "e.g." no longer count as
  file names; "refactor this function" stays single-agent; multi-component wording ("end to end",
  "wire X into Y", "and the tests", "across all ...") routes to the team.


## 2.22.3

- Team mode: the tester must produce a real red. If its tests already pass against the
  unchanged code, it is re-prompted once ("your tests must fail against the current code");
  if they still pass, the task stops as `tester_no_red` instead of reaching the coder.
- Team mode: if the tester writes no test file, it is re-prompted once; if it still writes
  none, the task stops as `tester_no_tests`.
- Every tool call the tester makes (role, tool, short argument summary, ok/error) is recorded
  in `TeamResult.tester_calls`, the loop's results row (`tester_calls`) and the loop log, along
  with a one-line role-timings and red/green summary.

## 2.22.2

- Team mode: a role that times out (or a loop SIGTERM) now stops calling the model. Before,
  the timed-out role's thread was abandoned but kept running its agent loop, so it could keep
  using the GPU after loop mode had released its gpu-queue lease. Each role gets its own stop
  event, set on timeout or on the caller's cancel; a cancelled team returns status `cancelled`.
- Team mode in loop mode gates on the queue item's `test_commands` when it has them (before,
  team tasks ignored them and only used the detected project check), and the results row
  records `roles` (seconds per role, in order), `revisions`, `red_green` and `check`.
- Team mode's tester is confined to test files: any non-test file it writes is reverted before
  the red gate (`tester_reverted` in the result). Found by the first live team run
  (2026-10-02, qwen3.8 27b under a gq lease): the tester implemented the whole feature with its
  tests, so the red gate already passed and the coder had nothing to do. The tester is also
  prompted for tests only.
- Red-before-green is recorded: `red->green`, `no-red` (the tester's tests already passed
  before the coder ran; the reviewer is told) or `no-check`.
- The owner's portable global rules are in Wilbur's system prompt: exact targets only, stage
  by path, `&&` not `;`, no secret values, no push/deploy/mail unless asked, label results
  verified/unknown/assumed, bump versions with every surface.

## 2.22.1

- gpu-lease pinned by hash manifest (jh-gpu-lease 0.3.0), no checkout path.

## 2.22.0

- gpu-queue requests use v2 signatures (gq >= 1.11.2: HMAC over ts + method + path + body)
  with `X-Gq-Client: wilbur`: loop mode's lease is gpu-lease 0.3.0 and the priority client
  signs with `sign_request_v2` from jh-gpu-queue-client 1.1.0 (pin bumped).

## 2.21.2

- Loop mode's gpu-queue lease is gpu-lease 0.2.0: every non-submit POST carries a nonce, so a
  cancel or heartbeat no longer collides with another client's empty-body POST in gq's replay
  guard (401 "replayed request"); a lease that is never admitted is released with the retried,
  idempotent `release_unadmitted`; SIGTERM while queued exits cleanly and cancels the lease.
- The vendored copy is checked against the origin-tracking monorepo checkout
  (`business/jsonholdings-origin-mirror`), not the shared working checkout, and pinned to 0.2.0.
  A control test proves the staleness check reports a modified copy.


## 2.21.1

- `wilbur loop --once` exits 2 (UNKNOWN) and says so when a pending task did not run because
  the gpu-queue lease failed (refused, unreachable, not admitted). It used to exit 0 with
  "0 task(s) recorded", which read as an empty queue. An empty queue still exits 0.
- A task that ran but whose lease release failed (gpu-queue went away mid-task) is now recorded
  with a `lease_note`, instead of being lost and run again.
- Tests: a submitted lease is cancelled on every failure path before admission; when the cancel
  itself fails, the lease never heartbeats and gpu-queue's lease TTL expires it.
- `contrib/loop-queue.seed.json`: 12 small, safe tasks for this repo with deterministic checks.
  The sample unit points at this repo and the seed queue; README "Loop mode" has the install,
  start, watch, stop and branch-review commands.

## 2.21.0

- Loop mode. `/loop <goal>` keeps working turn after turn until the model says `GOAL COMPLETE`
  and the project's own check passes, capped by `loop_max_turns` (20) and `loop_max_minutes`
  (60); two turns with no tool call stop it as stalled; Ctrl-C stops it and keeps the
  conversation. Before this, any reply without a tool call ended the work and Wilbur waited.
- `wilbur loop --queue FILE`: headless task queue (`wilbur/queue/1` or routine-runner
  `queue/1`). Each task runs in a `wilbur/<slug>` worktree branch (team mode where sized), is
  committed there, and is recorded in `FILE.results.jsonl`; nothing is merged or pushed, and a
  moved checked-out branch is recorded as `violation`. Each task holds a gpu-queue
  `external_lease` with heartbeat (vendored `gpu-lease` 0.1.0), unloads its model, and
  releases before the next; deferral and queue outages back off instead of failing the task.
- Team mode: `TeamOrchestrator.run(..., merge=False)` stops at an approved, gate-verified
  branch (status `approved`).
- Sample `contrib/systemd/wilbur-loop.service` and `contrib/loop-queue.example.json`
  (not installed).

- Voice (private build): Wilbur no longer invents figures. The system prompt forbids stating an
  amount, fee, price, balance, date, phone number, percentage or statistic about Jay's own bills or
  accounts that he did not give, and asks him to say he doesn't have it and where to find it.
- Voice: a figure guard runs on every sentence before text-to-speech. A sentence stating a money
  or percent figure that is neither in what Jay said this session nor one arithmetic step from
  those numbers is spoken as "I don't have that figure." (once per run), kept out of the history,
  logged to stderr, and counted as `ungrounded_figures` in the timing log.
- Eval: eight fabrication cases (late fee, balance, phone number, due date, gas price, a late-pay
  statistic, next bill, and a grounded rent subtraction) grade with a numbers-grounding rubric:
  every number in the answer must be in the prompt or follow from it by one arithmetic step. The
  real 2026-09-30 "$35 / $25 late fee" answer fails it. `--max-wait`, resume and `--system-file`
  / `--no-guard` for before/after runs.

## 2.19.0

- Voice (private build): answers the question asked, with the figure. The system prompt now asks
  for the answer first with no thinking-aloud opener, for concrete numbers with units (typical
  temperatures in degrees Fahrenheit, times, amounts), says plainly that a call has no live
  weather, calendar, bank or bill data, and orders bills that are due by due date and by what
  missing each costs rather than a general "highest interest first" rule.
- Voice: spoken filler ("Let me think...", "Hmm,", "Well,", "Okay, so") and any `<think>` block are
  stripped from the start of every reply before text-to-speech, holding back only the first 40
  characters. `think` was already off; the filler was plain content, not leaked reasoning.
- `scripts/voice_quality_eval.py` grades every answer against pass/fail rubrics
  (`voice/quality_rubric.py`), adds four regression cases (16 prompts), exits 1 on any failure, and
  `--grade <file>` re-grades an earlier results file without the GPU. The rubrics flag exactly the
  three weak 2026-09-29 answers. Wall times stay shared-load numbers.


## 2.18.2

- Private build only: the voice latency bench runs again in the whisperx venv it documents. Since
  2.18.1 it failed at import (`No module named 'jh_gpu_queue_client'`); it now falls back to the
  pinned client installed in `.venv-voice`. No change to the service or the public build.


## 2.18.1

- Private build only (the GPU priority client is not part of the public build): secret lookup
  and request signing now come from a pinned shared package instead of Wilbur's own copy; a
  test fails when the installed package differs from the pin. A group- or world-readable
  secret file is now refused (treated as no secret) rather than used. No change to the
  public build or to wait/override behaviour.


## 2.18.0

- Voice: the first reply of a call is no longer cold. On session connect (WebSocket open; a phone
  adapter calls `VoiceSession.start_prewarm()` at call start) Wilbur requests gpu-queue priority and
  loads whisper and the Ollama model in parallel while the owner speaks the first sentence. Nothing
  loads without a grant; the warm uses the chat's exact `keep_alive` and `num_ctx`.
- Voice: if the first utterance ends before that load finishes, a pre-rendered "One moment." clip
  plays at once (Piper, rendered at build time by `scripts/render_voice_clips.py` into
  `voice/assets/one_moment.wav`; no GPU or TTS call at call time), then the answer follows.
- Voice: a connect with no utterance for `WILBUR_VOICE_CONNECT_IDLE_S` (default 60) unloads and
  releases. A close during an in-flight prewarm waits for the load to land, then unloads, so a
  mid-load model is never left resident. The 2.17.2 hang-up path is unchanged.
- Spoken GPU waits with no ETA now say what is running and for how long ("a LatentWorks render has
  been running 3 minutes; no estimate yet"), from GQ's `elapsed_s` (or `started_at` on GQ 1.5).
- gpu_priority: a 503 or refused connection (a gpu-queue restart) is retried (0.5 s, 1.5 s;
  `WILBUR_GQ_RETRY_DELAYS`) before the queue is treated as absent.

## 2.17.2

- Voice: a hang-up now always unloads Wilbur's models and releases the gpu-queue priority. Found
  on the real GPU: aiohttp cancels the socket handler when the peer drops the connection, which
  abandoned `aclose()` mid-unload, so qwen3:14b and the whisper worker stayed resident and the
  heartbeat thread kept wait mode on, holding every queued job. The close now runs as its own
  shielded task. Regression test: `test_hangup_unloads_then_releases_even_on_abrupt_disconnect`.

## 2.17.1

- Voice: Wilbur's own models are unloaded when the gpu-queue priority is released (hang-up and
  the 120 s idle TTL). `VoiceSession.aclose()` unloads the Ollama voice model by exact name
  (`keep_alive: 0`, only if `/api/ps` lists it, then waits until it is gone) and stops the GPU
  whisper worker process, THEN releases the priority, so a render that re-leases the GPU no longer
  finds ~10 GB still resident for 30 min. While the priority is held the models stay warm.
  The CPU fallback recogniser and other apps' models are never touched; an unload failure still
  releases. `/healthz` now reports `version`.

## 2.17.0

- Voice defaults tuned from a clean-room GPU bench (docs/VOICE-BENCH-2026-09-29.md): STT
  `faster-whisper-large-v3-turbo` (was large-v3), LLM `qwen3:14b` (was the 27b), Ollama
  `num_ctx` 8192 (new `WILBUR_VOICE_NUM_CTX`; Ollama's 32k default put the 27b at 20 GB and
  whisper then failed with CUDA out of memory), and eager first-clause splitting
  (`WILBUR_VOICE_SPLIT=eager|default`). Measured first-audio p50 1051 ms -> 727 ms.
- `scripts/voice_bench.py`: the per-stage latency bench (private build).

## 2.16.0

- GPU priority: a 401/403 from gpu-queue is no longer treated as "no queue". The turn is parked,
  the error is logged, and the user is told "GPU queue rejected Wilbur's signature". 404/503 still
  fall back to OSS behaviour.
- Voice: optional `/ca.crt` route (set `WILBUR_VOICE_CA_CERT`) serves the local CA's public
  certificate so a phone can install it; refuses to serve anything containing a private key.
- `scripts/wilbur-voice-phone-peer.sh`: owner-run root script that adds the phone as a wg0 peer
  (via the shared workstation-config add-peer script) and prints its QR code.

## 2.15.0

- Terminal Wilbur can wait on an optional, private-build-only GPU scheduler before a turn and
  offer `/override` (with a typed confirmation). The public build does not ship that module and
  behaves exactly as 2.14.0.

## 2.14.0

- The startup box now shows the running version (`Wilbur v2.14.0`). Wilbur is installed editable,
  so a process keeps whatever the source tree held when it STARTED -- a fresh `wilbur --version`
  can report a newer build than the session you are sitting in. Printing it on load makes the
  version you are actually running visible without a second command.
- The idle sign-off is now `TASK COMPLETE -- CAN YOU SLOP THE TROUGH?`. Same behaviour as before:
  it comes from the REPL, not the model, so it stays an honest "waiting on you" signal, and it is
  still configurable via `idle_signoff` / `idle_signoff_text`.

## 2.13.0

- `--forget ID` deletes a saved session so `--continue`/`--resume` can never pick it up again --
  the documented way to clear a stale or unwanted session (also reachable from the VS Code
  extension's sessions picker, via a trash button on each entry).

## 2.12.0

- Wilbur improvement programme, checkpoint 2: a `run_tests` tool that runs the project's
  own test command (pytest via JUnit XML, or `npm test`/`make test` with best-effort
  regex parsing) and returns pass/fail counts plus file:line detail per failure instead
  of raw test-runner output the model has to re-parse itself.
- A skills system: `SKILL.md` folders (a short frontmatter name/description the model
  sees up front, a body loaded only on demand) discovered from a project's
  `.wilbur/skills/`, the user's `~/.config/wilbur/skills/`, and three built-in starter
  skills (`writing-tests`, `bench-tasks`, `changelog-entries`), exposed via `list_skills`
  and `load_skill`.
- `bench/tasks/07_test_diagnosis` added to the task benchmark; validated the same way as
  the existing six tasks (see `bench/VALIDATION.md`).

## 2.11.0

- Wilbur improvement programme, checkpoint 1: a task benchmark harness (`bench/`) for
  measuring agent quality before/after a change against fixed coding tasks with real
  pass/fail tests, run headlessly and recorded to JSONL (pass rate, rounds, wall time,
  tokens/s). Tested only against the fake model backend so far; the live run needs a
  Wilbur GPU window (see `bench/README.md`).
- Tool-call recovery now repairs common local-model JSON mistakes before giving up: a
  trailing comma before `}`/`]`, and single-quoted strings in place of double (only when
  the text has no double quotes at all, so a legitimately mixed string is never
  mangled). Applies to bare/tagged/fenced calls and to a string-encoded `arguments`
  field alike.
- `OllamaClient` retries a transient failure (connection reset, timeout, 5xx) with
  capped exponential backoff (default 3 retries); a 4xx or any other error is never
  retried.

## 2.10.0

- Idle sign-off: when a turn ends with nothing left in flight (no subagents running, nothing queued, no round
  limit hit), Wilbur prints `TASK COMPLETE — CAN YOU SLOP THE TROUGH?`. It comes from the REPL, not the
  model, so it is an honest "waiting on you" signal. Configure with `idle_signoff` / `idle_signoff_text`.

## 2.9.0

- `--sessions --json` emits the session listing as machine-readable JSON
  (id, cwd, updated, objective, turns), for the VS Code extension's
  session picker instead of parsing the human-readable table.

## 2.8.0

- The terminal look matches Claude Code's: prompt_toolkit replaces the
  hand-rolled fd-level stdin pump on a real tty. Output scrolls above a
  pinned input line with a bottom toolbar (spinner/status while a turn
  runs, then model, tokens and running subagents). History persists under
  `~/.config/wilbur/history`; Alt-Enter (Escape then Enter) inserts a
  newline for multi-line input; `/`-commands tab-complete. Off a tty
  (pipes, `WILBUR_NO_PTK=1`, or prompt_toolkit not installed) the old pump
  is used unchanged. Typing during a turn still queues a command, approval
  prompts still route through the same prompt, Ctrl-C still cancels a turn
  or subagents, Ctrl-D still exits.
- First-run model picker: if the configured model isn't installed, Wilbur
  lists the installed tool-capable models with a VRAM fit mark for the
  current GPU, lets you pick one, and saves it. If nothing tool-capable is
  installed, it suggests an `ollama pull` sized to detected free VRAM. If
  Ollama isn't reachable, it prints how to start it.
- The shipped default model is now `qwen3:14b`, a mainstream tool-capable
  model that a public release should work with out of the box.
- License, notice and brand files added (`LICENSE`, `NOTICE`, `BRAND.md`)
  ahead of the first public release.
- VS Code extension bumped to 0.2.0: `wilbur.path` defaults to `wilbur`,
  the `repository`/gallery fields point at the public
  `github.com/jsonholdings/wilbur` repo, a gallery banner was added, and
  the terminal tab now shows a full-colour Wilbur icon instead of the
  generic terminal icon.
- Each tool result shows its own elapsed time. `/help` is grouped into Model,
  Turn, Session and Other sections instead of one flat list.
- The GPU fit check now reads one card by index (`gpu_vram_mb(index)` /
  `gpu_free_mb(index)`, backed by a new `all_gpu_vram_mb()`) instead of always
  reading `nvidia-smi`'s first row regardless of which card Ollama would
  place a model on. `Config.gpu_index` (default 0) names the target card,
  ready for a second one.
- The system prompt carries a short set of always-on operating rules
  (verify and label claims, name explicit targets for destructive commands,
  stage only changed files, test before committing) alongside the identity
  lock, independent of persona.

## VS Code extension (`vscode/`)

A VS Code extension (own version, 0.2.0) that runs Wilbur in a terminal
panel — the shape of Claude Code's own VS Code integration. Commands:
`Wilbur: Open`, `Wilbur: Ask about selection` (`ctrl+alt+w`), `Wilbur: Send
current file`. See `vscode/README.md` and `vscode/CHANGELOG.md`. Does not
change the CLI's own version.

## 2.5.1

- Fix: text typed during a turn vanished as it was typed. The input pump
  echoed each key and then paused the spinner, and pausing clears the line.
  It now pauses once, before the first echoed key. Regression test drives a
  real pty with the spinner active.

## 2.5.0

- Typing is visible while a turn is running: the stdin pump reads at the fd
  level and switches a real terminal to cbreak mode so keystrokes echo as
  they're typed instead of being held by the kernel until Enter. Verified
  against a real pty, not just a pipe.
- Subagents get their own tool-round budget (`subagent_max_turns`) instead of
  inheriting the parent's `max_turns`.
- `/agents` lists in-flight and recently finished subagents (role, status,
  elapsed time, current tool). The spinner shows a one-line "(N subagents
  running)" status while any are live.
- Ctrl-C now reaches a running subagent: a cooperative cancel flag is checked
  between tool rounds and before each tool call, so an interrupted subagent
  stops instead of running unattended in the background.

## 2.4.0

- Hitting the tool-round limit no longer throws the turn's work away. Wilbur
  makes one final no-tools model call asking what was done, what is left, and
  the next concrete step, and shows that instead of the bare "stopped after
  N rounds" notice. The transcript is untouched by that call.
- `/continue` (or typing "continue") resumes a task with a fresh round budget
  and the same context.
- `/turns [n]` shows or sets the tool-round limit (`max_turns`) at runtime.
- The system prompt now tells the model to split a long task into small,
  numbered, independently-testable chunks and verify each one before moving
  on.
