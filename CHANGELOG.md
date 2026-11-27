# Changelog

Earlier history lives in `git log`; this file starts at 2.4.0.


## 2.64.0
- Tool-call parse errors fixed at the source (R1, `tool_call_mode`, default `auto`): when Ollama cannot parse the tool call the model wrote ("XML syntax error ... element <function> closed by </parameter>"), the same turn is asked again once in constrained mode: the `format` schema with the tool names, and no `tools` in the payload, so the server's XML tool-call parser never runs; Wilbur's own `parse_constrained_reply` reads the reply. If that also fails, the 2.62.0 note retry is the last resort. `tool_call_mode: "native"` keeps the 2.62.0 policy; any other value fails config validation. The `tool_parse_retry` event now carries `mode` (`constrained` or `note`). Not yet measured live: the rate on native vs constrained calls is owed to the M1 bench.
- Bench rows carry `tool_parse_retries` (count of `tool_parse_retry` events); the B0 baseline (`docs/BENCH-B0-2026-11-26.md`) could not see retried parse errors without it.
- Tests: `tests/test_tool_call_mode.py` (a fake backend that raises the XML error once: the retry is constrained and carries no note; a second failure falls back to the note; native mode never constrains; the forced constrained payload carries `format` and no `tools`; controls: a clean turn is never constrained and a default payload keeps `tools` with no `format`; the bench runner counts parse retries).


## 2.63.0
- Compaction keeps unacted reads (R2, `compact_keep_unacted`, default on; BACKLOG keep-working-regression-20261026): compaction never summarises the newest round's tool results, and `read_file` results the model has not acted on yet (no later edit/write of that path, not read again since) are carried through verbatim instead of going to the prose summary, up to half of `num_ctx` together with the recent slice. Past that they become a file index (path, line span, "re-read with offset/limit"), so a re-read is targeted. Reads of files already edited are summarised as before. 2.62.0's round hold-back stays. Set `compact_keep_unacted: false` for the 2.62.0 policy.
- Mirror hygiene (R0): `docs/PROGRAMMES-PLAN.md` (internal planning) and `changes/` are excluded from the public mirror; `docs/PARITY-ANALYSIS.md` and `docs/LIVE-CHECKS-*.md` stay public. `changes/README.md` holds the CHANGELOG fragment rule for the release train.
- Tests: `tests/test_compact_keep_unacted.py` (a policy-model reproduction of live run A at num_ctx 32768: with the switch on every module is delivered at most twice; the control with it off re-reads some 3 times), `tests/test_sync_mirror_private_docs.py` (with a control that PARITY-ANALYSIS.md is still mirrored).


## 2.62.0
- Compaction thrash (BACKLOG keep-working-regression-20261026, live W3 2026-11-26): a round of parallel reads whose results would push the prompt past the compaction budget no longer overflows it. Reads after the first in a round that would cross the budget are held back with a "[Not shown: ...]" result telling the model to act on what it has and request them again; the first read of a round always runs, and write/command results are always kept. On 2.61.0 twelve parallel reads (~56.8k tokens at num_ctx 32768) were compacted away before the model saw them and it re-read all twelve: 7 compactions, 96 reads, 0 edits in 359 s.
- Tool-call parse errors: when Ollama cannot parse the tool call the model wrote (`ollama: XML syntax error ...`, `error parsing tool call ...`), the model call is retried up to 2 times with a harness note instead of ending the run (live W3: a 5-item task list stopped after item 4 on this error). Other server errors still raise as before.
- Tests: `tests/test_compact_round_overflow.py`, `tests/test_tool_parse_retry.py` (each fails on 2.61.0, with controls).


## 2.61.0
- Publish refusals: `scripts/publish.sh` refuses before touching origin or the mirror when tracked files have uncommitted changes, or when HEAD's `wilbur/__init__.py` version differs from the CHANGELOG top (a release commit that was refused or never made); `--preflight` runs only these checks (BACKLOG publish-on-refused-commit).
- Publish gate output goes to a log file; on failure the FAILED/ERROR lines and the summary are printed instead of a silent exit 1 (BACKLOG publish-gate-hides-failures).
- Test-suite guard: the real-home check records writes made by the test process (an audit hook) instead of comparing mtimes, so the owner's live Wilbur writing its history mid-suite no longer fails the last test's teardown (BACKLOG flaky-test-worktree-teardown). `tests/conftest.py`.
- Contained commands: the no-systemd RLIMIT_AS fallback caps virtual memory at 4x `tool_memory_max` (Go/JVM reserve far more than they touch; ASan cannot run under RLIMIT_AS, documented); the memory-cap hint uses the scope's own systemd `Result=oom-kill` (scopes are no longer `--collect`ed; read and reset by name), so a command exiting on its own SIGTERM gets no hint, and the fallback hints only on SIGKILL or an uncaught `MemoryError`; `stop()` survives a missing `systemctl` (BACKLOG contained-commands-followups). `wilbur/tools/contained.py`.
- GPU holder marks: written only while granted (a waiting voice server no longer blocks a terminal's release), mark/scan serialised with a file lock, and each mark records the process start time so a reused pid is not a live holder (BACKLOG gpu-holder-refcount-races). `wilbur/gpu_priority.py`.
- Breaker/config fast-follow: write/edit signatures carry a content digest, so a same-path retry with new content is not hard-blocked; a typed line resets the runaway-output streak; a `wilbur loop` goal starts with an empty failure ledger (`Agent.begin_task`); `Config.validate()` at load rejects non-numeric integer knobs and clamps them to their use-site minimums (BACKLOG wilbur-2400-fastfollow).
- REPL input history: the prompt_toolkit history file is redacted with `wilbur/redact.py` (same rules as sessions) and kept 0600; up-arrow recall in the running session is unchanged (secrets-in-session-log follow-up). `wilbur/repl.py` `redacting_file_history`.
- Tests: `tests/test_publish_preflight.py`, `tests/test_real_home_guard.py`, `tests/test_contained_followups.py`, `tests/test_gpu_holder_races.py`, `tests/test_fastfollow_2400.py`, `tests/test_history_redact.py` (each with a control).


## 2.60.0
- Wrong absolute paths: the static system prompt says "Use paths relative to the project root" (file tools already resolve relative paths there; the prompt stays within its size budget). A failed file op on an absolute path whose parent is an ANCESTOR of the root (the model dropped a long hidden segment such as `.bench-work-<task>-N`), or whose parent does not exist (mangled), now names the project-relative path by trying the path's tails, longest first (`/tmp/b/src/x.py` -> `src/x.py`); `list_files`/`glob`/`grep` on a missing absolute path get the same "did you mean" line. A real unrelated directory still gets no guess. Evidence: bench/results/trace-2590-2026-11-25 (26 failed `read_file`/`list_files`). `wilbur/tools/hints.py` `_project_candidate`, `missing_path_hint`.
- Read batches (2.59.0 verifier minors): a read the circuit breaker will block is no longer submitted to the pool (it ran, then was discarded); after a cancel no further reads are submitted and a queued read returns at once without running, so a cancel waits only for reads already running. `wilbur/agent.py` `_prefetch_reads`, `_cancellable_run`.
- `glob` walk is bounded: it prunes skip dirs, symlinked dirs and other mount points while descending (never entering them), stops at a 20 s time budget or 5,000 matches, and says when the result was cut short (BACKLOG glob-unbounded-walk). `wilbur/tools/search.py` `bounded_glob`.
- Session files are written 0600 in a 0700 directory, and the saved copy redacts secret values (`<REDACTED>`): values read from the files in the new `secret_files` config list (plus `WILBUR_SECRET_FILES`) and secret-shaped text (password/token/api_key assignments, Authorization headers, nmcli passwords, common token prefixes). The live conversation and the executed commands keep the real value. The system prompt says to read a secret from its file and never inline it (BACKLOG secrets-in-session-log). `wilbur/redact.py`, `wilbur/session.py` `save`.
- Streaming: the reply timeout is an idle cap reset on every received chunk, so a long reply that keeps streaming is no longer cut with a misleading "no output" message; a malformed NDJSON line raises a clear retryable `TransientOllamaError` instead of a raw `JSONDecodeError` (BACKLOG llm-stream-total-cap). `wilbur/llm.py` `OllamaClient._stream`.
- Tests: `tests/test_wrong_paths_2600.py`, `tests/test_glob_bounded.py`, `tests/test_session_redact.py`, `tests/test_llm_stream_cap.py` (each fails on 2.59.0, with controls).


## 2.59.0
- Batched tool calls: the static system prompt no longer says "Run one tool at a time"; it says "Batch independent calls in ONE reply (reads run in parallel); test once after edits", and the `read_file`, `glob`, `list_files` and `run_tests` descriptions say the same. When one reply holds two or more consecutive `read_file`/`grep`/`glob`/`list_files` calls, their `tool.run` executes on a small thread pool (at most 4 workers, joined before the round continues); events, the failure ledger, hints and the transcript are still applied one call at a time in reply order. A write or command in the reply ends the parallel run, so a read after an edit sees the edit and never overlaps it. Evidence: in the 2.57.2 trace run (bench/results/trace-2572-2026-11-25) rounds roughly equalled tool calls (`26_config_key_migration` rep 0: 17 calls in 15 rounds, 183 s, 4 file reads in 4 consecutive rounds); across the suite rounds/rep rose 7.8 -> 9.5 and p90 40.9 -> 60.5 s. `wilbur/agent.py` `PARALLEL_READ_TOOLS`, `_prefetch_reads`.
- `glob` resolves a relative `path` under the project root, not the process cwd, as `grep` and the hints already did. The outside-project file hint no longer guesses `<root>/<basename>` when the raw path's parent is a real directory elsewhere (it could name an unrelated same-named file); it then names the project root only. (BACKLOG hints-2580-verifier-minors.)
- Tests: `tests/test_read_batching.py` (prompt and descriptions carry the guidance; three reads meet a 3-party barrier, so they ran concurrently, and return in order; a single read runs inline; a write in the reply stays sequential and is not overlapped; glob relative-to-root with a decoy in the process cwd; the basename false positive and the plausible targets).


## 2.58.0
- Environment facts in the session briefing: the static system prompt now states the project root, that shell and test commands already run there (no `cd` needed, paths relative to it), and which interpreters are on PATH (`python` absent -> "use `python3`", plus a project `.venv`/`venv`/`env` python when present). Probed once when the agent is built, before the per-session extras, so the cached prefix never changes mid-session; `/cd` re-probes it as part of the system rewrite it already does. `wilbur/tools/hints.py` `env_facts`, `wilbur/agent.py` `Agent.__init__`.
- Recovery hints on failed tool calls (one `[harness]` line; the command or path is never rewritten): `run_bash` exit 127 `X: not found` names the alternative (`python` -> `python3` or the project venv, `pip` -> `python3 -m pip`, `X` -> `X3`) and says retrying will fail again; a failed `run_bash` whose leading `cd <dir>` is a missing dir or an ancestor of the project root says "run_bash already runs in <root>; drop the cd"; `read_file`/`edit_file`/`write_file` on an absolute path outside the project that fails point at the project path that exists or is writable (`write_file` now returns an error for an `OSError` instead of raising); `grep`/`glob`/`list_files` on a missing path name the project root and the nearest match. `grep` now runs in the project root, so a relative path means the project's, as in `run_bash`. Evidence: the 2.57.2 trace run (10 tasks x4, 40/40 pass, 45 failed calls) had 15 `python: not found` calls and about 6 wrong-cwd/path calls, each costing a round.
- Tests: `tests/test_recovery_hints.py` (each hint fires on its case, with controls: no hint on success or an unrelated error; facts say "use python3" only when `python` is absent).


## 2.57.2
- Bench instrumentation: every result row carries a `tool_trace`, one entry per tool call: `i`, `tool`, `args_head` (up to 160 characters: the command for `run_bash`, the path for file tools, `pattern @ path` for searches), `ok`, `error_head` (up to 200 characters of the error, failing calls only), `exit_code` when the output starts `exit N`, `cwd` when the call names one, and `red_test` for a `run_tests` red run. Blocked calls and unknown tools are traced too. The list is capped at 200 entries per rep, with the overflow counted in `tool_trace_dropped`. A failing call can now be classed as benign (grep no-match, a deliberately red test) or a real mistake (a guessed path), not only counted. Rows written before 2.57.2 load unchanged. Agent and tool behaviour is unchanged. `bench/runner.py` `trace_entry`.
- Tests: `tests/test_bench_tool_trace.py` (a fake-backend run with a failing `run_bash` and a missing-file `read_file` records the command, exit code and error text, and the passing calls record `ok` with no error; the cap and truncation; blocked and red-test entries; old rows without the field still load).


## 2.57.1
- Bench-harness correctness (parity G6 verifier findings): `36_long_context_needle`'s hidden test now compares every function except the one SKU under repair (a per-function digest), so a behaviourally-equivalent rewrite such as `(TAX + 1)` is accepted while any change to another function is still caught -- it no longer pins the whole file's sha256. `37_slow_build_step`'s `build.sh` stamps a nonce derived from `totals.py`, and the hidden test requires it, so a hand-written `report.txt` (which the prompt forbids) fails even with the right mean. The quick A/B task count is derived from the task tree (`bench.ab_compare.task_count`, `quick.all_task_dirs`) instead of the literal 35, so the 41-task suite is counted correctly in `QUICK_TASK_COUNT`, `QUICK_MIN_N` and `estimate()`.
- Tests: `tests/test_bench_g6_tasks.py` adds the equivalent-fix-passes / other-function-fails cases (36), the hand-written-report-fails / built-report-passes cases (37), and a derived-count assertion.


## 2.57.0
- Bench coverage (parity G6): six tasks for areas the suite did not measure. `36_long_context_needle` (one wrong function among 2,400 in a ~300 KB module; every other function must stay byte-identical), `41_long_log_needle` (the one 500 among 6,000 log lines), `37_slow_build_step` (a fix that only counts once a ~25 s `build.sh` has regenerated its output), `38_wilbur_md_rules` (a new function that must follow the project's WILBUR.md rules), `39_verify_before_done` (a visible test that passes on a partial fix, hidden edge cases that do not) and `40_multi_step_change` (rename, new parameter and caller update in one request). The full suite is now 41 tasks, so a full A/B run takes about 25 s more per rep. These are not yet measured on a model.
- Tests: `tests/test_bench_g6_tasks.py` (each task's hidden test fails on start/ as a control and passes on reference/, task.json complete, needles unique); the reference-solving client passes all 41 (`tests/test_bench_runner.py`).


## 2.56.0
- One-shot fallback model (parity G8, BACKLOG fallback-model-one-shot): `wilbur -p` now runs the same `choose_session_model` check as the REPL and `wilbur loop`, so with the second 3090 missing it uses `fallback_model` and prints the notice on stderr (stdout stays the reply) instead of failing the fit on the two-card default. It never prompts. `-p -m X` is never swapped. `wilbur/__main__.py` `_oneshot_fallback`.
- Loop breaker budget (parity G7, BACKLOG loop-breaker-carryover): every goal-loop / `wilbur loop` turn starts with a fresh tool-failure budget (`Agent.begin_user_turn`), as a typed REPL line has since 2.51.1. Before, a breaker trip in one turn re-tripped the next turn on its first failing call. The per-call ledger is kept, so an identical failing call stays blocked. `wilbur/loop.py` `run_goal_loop`.
- Tests: `tests/test_oneshot_fallback.py` (fallback with one card, controls: both cards keep the default, `-m` never swapped, no fallback configured; mutation with the hook unwired fails); `tests/test_loop_breaker_fresh_budget.py` (turn 2 recovers; control without the reset re-trips).


## 2.55.1
- Bench: a `run_tests` call that ran the suite and reported failing or erroring tests is counted in a new per-row `red_test_runs` field, not in `tool_errors`. It is the model's red/green loop, not a broken tool call. Parity round 3 found that most "tool errors" on `12_json_deep_merge` were red test runs, which inflated the G3 error rate. Pytest and other runners' summaries are recognised; a run that could not run (`DID NOT RUN`, timeout, bad cwd, denial, unparseable output) is still a tool error. `bench/runner.py` `is_red_test_run`.
- Tests: `tests/test_bench_isolation.py` (pytest red and collection-error runs, a jest red run, controls: a green run, `DID NOT RUN`, a runner-missing error and the same text from `run_bash` stay tool errors).


## 2.55.0
- Untested "done" guard (parity G4, bench `12_json_deep_merge` 4 of 8 reps: a shallow-copy fix reported as verified on the model's own `python -c` check while nested dicts were still mutated). When a call wrote code files and no test ran after the last write (`run_tests`, a test command in `run_bash`, or the harness's own project check), a reply claiming fixed/verified/working/passing/done gets one `[harness]` nudge per call: run the sibling tests it names (`test_x.py`, `x_test.py`, `tests/test_x.py`), or the project's check, or write a pytest test for the stated requirement and run it. Docs-only writes and replies without a claim are untouched; `verify_done_claims: false` turns it off. `wilbur/doneclaim.py` `untested_done_nudge`.
- Contained commands: `systemd-run` gets `--expand-environment=no`. Newer systemd-run expands `${VAR}` and `$$` in `--scope` commands (its man page says that default is changing), so `echo $$` became `$`. This was the public mirror CI failure (red 2.44.1-2.54.0, `test_timeout_kills_detached_grandchild`), and any host with such a systemd mangled every `$` in Wilbur's shell commands. A systemd-run without the option fails the scope probe and falls back to RLIMIT.
- Kill guard: `pkill -n` (newest) no longer swallows the pattern after it (verifier finding on 2.54.0).
- Mirror CI (mirror-only workflow, shipped with 2.54.0): failed tests are posted as check-run annotations and a step summary, and junit + output are uploaded as the `pytest-failures` artifact, so a failure can be read from the public API without repo admin.
- Tests: `tests/test_untested_done.py` (nudged once, harness-run project tests count, sibling test discovery and naming, controls: a test run after the write, no write, docs-only write, no claim, switched off); `tests/test_contained_commands.py` (`--expand-environment=no` before `--`, `$$`/`${HOME}` reach the shell; mutation `=yes` reproduces the CI failure); `tests/test_shell_kill_guard.py` (`pkill -n`).


## 2.54.0
- Kill guard (parity G5, bench `32_kill_pid_list` 5 of 8 reps failed: the model's self-test SIGTERM'd Wilbur and then tried PID 1 under approve-all). `run_bash` refuses, before approval and in every approval mode, a `kill` of 1, 0, -1, Wilbur's own PID, its parent, its process group (PGID or -PGID) or `$PPID`, and a `pkill`/`killall` whose pattern matches Wilbur's own process, init/systemd, or that selects by user (`-u`). The message says to signal only the child PIDs the command started. New `wilbur/tools/killguard.py`; a `$(pgrep ...)` target cannot be resolved before running and is left to approval.
- Tests: `tests/test_shell_kill_guard.py` (blocked forms incl. wrappers and chained segments, allowed forms incl. `kill $!`, `kill -l` and quoted text, refused without running or asking, and a control that `kill <child pid>` still runs). Mutation control: with the guard unwired, the test run SIGTERMs itself.


## 2.53.0
- Parity review (docs/PARITY-ANALYSIS.md): a ranked gap report from the two full 2x3090 A/B runs (default model 267/280 tasks, 8.4% of tool calls errored), one BACKLOG item per open gap.
- `--approve auto-edit` and the `auto_approve` config grant `edit_file`/`write_file` inside the session's working directory only. An edit or write outside it is asked about, as in Claude Code's acceptEdits mode (`OUTSIDE_CWD` in `wilbur/tools/base.py`). `--approve all` is unchanged.
- Bench: per-rep workdirs go to a fresh temp dir, and a `--workroot` inside any git repo is refused (`bench/runner.py` `isolated_workroot`). The 2026-11-23 run staged them inside this repo, so `35_integrity_manifest` treated the Wilbur repo as its project and wrote `integrity.py` into the repo root.
- Bench rows carry `tool_error_kinds`, a tally of `tool: error class`, so the tool-error rate can be ranked by cause.
- `contained.scope_available()` probes with exactly the `systemd-run` argv that `wrap()` gives real commands (it omitted `MemorySwapMax=0`), so a user manager that rejects a property used by real commands makes Wilbur fall back to RLIMIT instead of failing every command. This is the suspected cause of the public mirror CI being red since 2.44.1. It is not confirmed, because the CI log needs admin rights. A clean python:3.12-slim + git + make container passes the mirror suite before and after the change (1116 passed).
- Tests: `tests/test_bench_isolation.py` (not mirrored: imports bench/) and `tests/test_parity_isolation.py` (temp workroot, a repo workroot refused, task dir left untouched, the outside-cwd flag with an in-cwd control, auto-edit and auto_approve ask outside the cwd, approve-all still grants, error-kind tally with a different-cause control).


## 2.52.1
- `run_tests` takes a `cwd` argument (absolute, or relative to the session cwd). Owner session 2026-11-24: a `cd <repo> &&` in an earlier `run_bash` does not carry over, so `run_tests ./venv/bin/python -m pytest` ran from `$HOME`, hit `~/venv` ("No module named pytest") six times, and tripped the tool-failure breaker.
- A `run_tests` command that exits non-zero without reporting any test counts now reads `tests: DID NOT RUN ... ran in <dir>` instead of `0 passed, 0 failed`, which looked like a clean suite.
- `run_bash` and `run_tests` timeouts name the timeout used and say that an identical retry will time out again (raise `timeout`, narrow the command, or run it in the background), after three identical 60 s retries on a slow suite in the same session.


## 2.52.0
- Project-directory awareness (owner session 2026-11-23: the laptop launcher starts `wilbur` in `$HOME`, so the model ran `./venv/bin/python -m pytest` against `~/venv` and the project's WILBUR.md never loaded). New `wilbur/projectdir.py`.
- Last project: the git root is recorded in `~/.local/state/wilbur/last-project` (env `WILBUR_LAST_PROJECT`) at startup inside a repo, from an absolute `cd <dir> &&`/`cd <dir>;` prefix in `run_bash`/`run_tests`, and by the new `/cd <dir>` REPL command, which also moves the session cwd and reloads the project's instructions. An interactive start in `$HOME` with no `-C` returns to the recorded dir and prints one line; new `--here` stays. `-p` and `wilbur loop` never switch. `$HOME` itself is never recorded.
- `No module named X` hint: a `run_bash`/`run_tests` result containing it gets up to 3 `[harness]` lines naming the interpreter, the cwd, and any `.venv`/`venv`/`env` python under the cwd or its git root, with the `-m pip install X` line for it.
- Global instructions: `~/.config/wilbur/WILBUR.md` (env `WILBUR_GLOBAL_INSTRUCTIONS`, first 6000 chars) is loaded into every session's system prompt (REPL, `-p`, `wilbur loop`), ordered persona -> global -> project.
- Tests: `tests/test_projectdir.py` (switch only from `$HOME`, controls for `--here`, `-C`, a non-`$HOME` cwd and `-p`; hint absent without the error; global block absent without the file and ordered before the project's when present). The suite redirects both new paths into its temp dir.

## 2.51.1
- Fix: a tripped tool-failure circuit breaker now resets when the user types a message. `consecutive_tool_failures` carried over between user turns, so after a trip, `/continue` or any new instruction tripped the breaker again on the model's first failing call, despite the stop message promising "a fresh budget" (owner session 2026-11-23: counts 7, 8, 9 on three replies). New `Agent.begin_user_turn()`, called by the REPL for each typed line; keep-going nudges do not reset it, and the per-call ledger is kept, so an identical failing call stays blocked. Tests: `tests/test_breaker_resets_on_user_turn.py` (fresh budget after a trip, the breaker still trips within one turn, the REPL calls the reset, and a control reproducing the re-trip without it).

## 2.51.0
- KV-aware VRAM fit. `model_need_gb`/`fit_report` (and so the session fallback check and the `wilbur loop` gpu-queue lease size) now compute the KV cache from `/api/show` `model_info`: full-attention layers only for hybrids (per-layer `head_count_kv` list, or `block_count` - `nextn_predict_layers` over `full_attention_interval`), x `head_count_kv` x (`key_length` + `value_length`) x `num_ctx` x bytes per element. New config `kv_cache_type` (`f16` default, `q8_0`, `q4_0`; any other value is rejected at load) must match the server's `OLLAMA_KV_CACHE_TYPE`. Models without the metadata keep the old q4_0 heuristic (~0.9 GB per 64K), and the fit report names which one it used. The old heuristic under-counted the 27b's f16 KV at 64K by about 4.7x (0.9 vs 4.3 GB). Tests: `tests/test_kv_cache_fit.py` (both measured llama.cpp KV sizes within 10%, 27b at 131072 fits one card with q8_0 and not with f16, 35b-a3b at 131072 q8_0 fits two cards, heuristic fallback, and a control proving the old heuristic underestimates the 27b).

## 2.50.0
- Two-card default with an automatic one-card fallback (owner, 2026-11-23). New config `fallback_model` (empty = off). At session start (`wilbur` and `wilbur loop`, not with an explicit `-m`) the configured `model` is fit-checked over `gpu_indices`; when it does not fit, or its VRAM is unknown because a listed card is missing, and `fallback_model` is installed and fits the cards that are present, the session runs on the fallback (main, and subagents unless `subagent_model` names another model) and prints one line saying which and why. Neither fits: no switch, the existing warnings apply. `Config.save` keeps the configured `model` on disk. `wilbur loop`'s gpu-queue lease now carries the model's estimated need as `params.vram_gb` (gq's lease routing field, becomes `min_vram_gb`) and `params.needed_vram_gb` (admission), so a two-card model spans both gq lanes and a one-card model leases one. Recommended config in MODELS.md / README.md: `model` `huihui_ai/Qwen3.6-abliterated:35b-a3b-q8_0`, `fallback_model` `huihui_ai/qwen3.8-abliterated:27b`, `gpu_indices` `[0, 1]`, `subagent_model` `""`. Tests: `tests/test_model_fallback.py` (both cards fit, second card missing or busy, neither fits, fallback off, save keeps the default, and a control proving the fit check says "does not fit").

## 2.49.1
- Fix: without a user systemd manager (CI, containers) the RLIMIT_AS fallback stops a runaway allocation with `MemoryError` (exit 1) rather than a signal, and `run_bash`/`run_tests` then gave the model no memory-cap hint. The hint now also fires on a non-zero exit whose output contains `MemoryError`. This was the public mirror's 2.49.0 CI failure. Tests: `test_rlimit_fallback_still_tells_the_model`, and `tests/test_contained_commands.py` passes with `WILBUR_NO_SCOPE=1`.

## 2.49.0
- Every model-issued command (`run_bash`, `run_tests`, and the A/B bench grader's pytest) now runs memory-capped and tree-killed (`wilbur/tools/contained.py`). Incident 2026-11-23 13:52–14:12: in the full A/B run a model wrote a CSV parser whose `while i <= len(line)` loop appended `""` forever; `subprocess.run(shell=True, timeout=…)` killed only `/bin/sh` on timeout, the python child was orphaned and allocated until RAM and all 28 GiB of swap were gone, the kernel never OOM-killed (zram livelock), and pop-os had to be power-cycled. Commands now run in a transient systemd user scope with `MemoryMax` (new config `tool_memory_max`, default `8G`) and `MemorySwapMax=0`, so the kernel kills them at the cap; on timeout or Ctrl-C the scope is killed by its exact unit name, which also catches children that called `setsid`. Without a user systemd manager it falls back to `RLIMIT_AS` plus the process-group kill. A killed command tells the model it most likely hit the memory cap. Tests: `tests/test_contained_commands.py` (cap kills the leak in seconds, setsid grandchild dies on timeout, RLIMIT fallback, and a control proving the same leak grows past the cap uncapped).

## 2.48.1
- Model A/B harness fix (pre-registered in `bench/AB-GEMMA4-31B.md` before the rerun): the ifeval and refusal probes now run through Wilbur's own client built from the live config, so they use Wilbur's num_predict (32768) and its client-side think budget (8192) instead of a fixed 4096 cap. An empty refusal-probe reply cut off by the limit (done_reason "length" or "think_budget") is recorded as `truncated` and is not a refusal; phrase refusals and other empty replies count as before. Probe rows record `done_reason` and `eval_count`; `ab_compare` reports the truncated count per arm (information, not a gate). Criteria, thresholds, CI method, seed and the 2.48.0 owner rule are unchanged; the 2026-11-23 full run's FAIL stands. Tests: `tests/test_bench_probe_truncation.py`, with a control showing the old 4096 path scored the 11-23 empty replies as refusals.

## 2.48.0
- Model A/B rule change (owner, 2026-11-23, choice "a"): agent-task and quality criteria are now NO_WORSE by the CI (the 95% bootstrap CI of candidate − incumbent does not lie entirely on the worse side: upper bound ≥ 0 for pass rates; a tie passes). Only prefill and decode must still be BETTER; refusals and VRAM fit are unchanged gates. The futility stop uses the new rule. Recorded in `bench/AB-GEMMA4-31B.md` before the next run. Tests: a 28/29 vs 29/29 near tie is NO_WORSE and is not dropped by futility; the 6/16 vs 16/16 control is still WORSE; identical arms still FAIL on speed.
- Fix: the quick screen's overall verdict now maps `tasks` onto the full rule name (`coding`), so a NO_WORSE task verdict counts as passing.
- `bench/ab_gemma4_31b.sh --candidate TAG` (repeatable, needs `--profile`) runs only that candidate of the profile; `--plan` prints the selection and exits with no preflight or GPU.

## 2.47.0
- Two-card hosts (BACKLOG `dual-3090-enable`, code part; default off = single-card behaviour). `Config.gpu_indices` lists the cards the main Ollama server may spread a model over, and the VRAM fit check sums free memory over exactly those cards (`models.gpu_free_mb_for`, used by all four fit call sites). `Config.subagent_base_url` (+ `subagent_gpu_index`) names a second Ollama server pinned to the other card: `task` subagents and the compaction summariser use it, so they run beside the main agent instead of queueing behind it. Tests: `tests/test_dual_gpu.py` (fakes only). The live two-server check and the bench/ two-card scripts' 3070-handover cleanup run after the 2026-11-23 A/B.

## 2.46.1
- A/B long-context probe is now calibrated per model: one throwaway prefill (seed 999) measures that model's chars/token, then each measured rep is sized to the middle of the pre-registered 36-41k band. The fixed 135,000-char briefing gave 55.8k tokens on qwen3.8 (2.42 chars/token), so every 2026-11-22 prefill row was out of range and the gate read INCOMPLETE. Rows carry `target_chars`.

## 2.46.0
- Model A/B rule change (owner, 2026-11-21): refusal rate and VRAM fit are now must-not-be-worse gates (NO_WORSE: a tie passes, a candidate that refuses more on average or spills fails). Every other criterion must still be clearly BETTER, and a tie there still fails. `bench/ab_compare.py` gains a shared `judge()`; the identical-arms control still FAILs and the refusing control is still WORSE.
- Quick screening, the new default for `bench/ab_gemma4_31b.sh` (`--full` keeps the full suite only). `bench/quick.py` runs a speed/fit benchmark once per model: VRAM fit with per-card VRAM and no spill, prefill on a cold 36-41k-token briefing, and decode tok/s. The incumbent's results are cached for the run. It then runs the 35 tasks in Wilbur's agent loop, interleaved incumbent/candidate.
- A pre-registered, futility-only stop (`ab_compare.reachable`) drops a candidate as soon as even the best case for every remaining task cannot pass. There is no early pass. A quick PASS is queued for the full suite automatically.
- `--estimate` prints the per-candidate time from measured wall times, or ASSUMED constants before anything has been measured.
- `--preflight-only`, with `AB_*` overrides, makes the clean-room preflight testable on a fake nvidia-smi/tmux/ollama PATH. Tests: tests/test_bench_quick.py and tests/test_bench_preflight.py.
- The pre-registration doc records the candidate order and the public vendor prior (Qwen3.8 27B ahead on LiveCodeBench v6 and GPQA, ASSUMED). Not yet run live.

## 2.45.0
- Pre-registered model A/B harness (`bench/AB-GEMMA4-31B.md`, written before any live run): the incumbent `huihui_ai/qwen3.8-abliterated:27b` against abliterated Gemma 4 / Qwen builds, per hardware profile (`3090`, `3090+3070`, `2x3090`). A candidate PASSES only if the 95% bootstrap CI of the difference is on the better side of zero on every criterion: coding, tool-call errors, agentic completion, instruction following, long-context quality and prefill at a 36-41k-token briefing, decode tok/s, VRAM fit at num_ctx with no spill, stability, refusals. A tie is not better.
- `bench/tasks` grows from 7 to 35 tasks (coding, instruction_following, agentic, refusal_probe); each start tree fails and each reference passes (`bench/VALIDATION-08-21.md`, `bench/VALIDATION-22-35.md`).
- `bench/runner.py` records category, model label, tool calls/errors, truncation, thinking cuts, runaway replies, turn-limit hits, hangs and refusals per trial, with `--resume`, `--label`, `--config`, `--workroot` and a `--control-refuse` control. The live path was broken before this: it patched a client instance in as the `OllamaClient` class, a TypeError on the first live call; the Agent now builds its own client from the owner's config with only the model swapped.
- `bench/probes.py` (long-context, decode, VRAM incl. per-card, single-turn instruction following, refusal prompts), `bench/ab_compare.py` (scorer, exit 0 PASS / 1 FAIL / 2 INCOMPLETE), `bench/ab_gemma4_31b.sh` (one clean-room, resumable command; refuses with exit 2 on an open Wilbur session, gq running, VRAM held, busy CPU, a profile mismatch or low disk) and owner-run root helpers for the 3070 handover. Not yet run live. Tests in tests/test_bench_ab.py.
- MODELS.md lists the A/B candidates with digest and licence.

## 2.44.2
- A repeated successful call is now flagged, not just a repeated failing one. The failure ledger only counted FAILED repeats, so a model reissuing the identical successful call with the identical output went unflagged (session desktopuser1-1792984315: the same `run_bash` 4 times, same output). The state now keeps a success-repeat ledger keyed on call signature AND output -- a call that returns something new is progress, not a repeat -- and on the 3rd identical one the tool result carries a `[harness]` note telling the model to change the arguments or approach. Tests in tests/test_state.py and tests/test_agent.py.

## 2.44.1
- `/restart` works from a plain checkout, not only an install. 2.44.0 relaunched with `python -I -m wilbur`, which cannot find the package without an install (the public mirror's CI caught it); the new process now runs under `-I -c` with this package's own parent directory first on sys.path, so the working directory still cannot shadow it. The restart tests run the real relaunch command from `/`.

## 2.44.0
- `/restart`: reload Wilbur after an update without losing the session. It saves the session, ends the process with the model left loaded (the new process reuses the cached prompt), restores the terminal, and re-executes Wilbur on `--resume <session>` so the code now on disk is imported. A /model, /context or URL change made in the session is passed on as a flag; the approval mode goes with it via the new `--approve {ask,auto-edit,all}` option. If the exec fails it prints the `wilbur --resume` line to use. Tests in tests/test_restart.py; checked end to end in a pty with the fake backend.

## 2.43.1
- A line typed mid-turn no longer stops a long run after the model has worked past it. Keep-going treated any line read during the turn as "the user wins over the nudge", even when the model had read it and gone on with tool calls; session desktopuser1-1793148787 (2026-10-27) read a line at message 15, worked ~120 more messages to the round cap, and stopped as `user_interrupt` on that long-answered line. The count now clears when the model replies with a tool call after reading it; a line the final reply answers still stops the run. Tests in tests/test_bounded_message_check.py.

## 2.43.0
- Runaway reasoning no longer stalls or ends a run. Measured 2026-10-26 on huihui_ai/qwen3.8-abliterated:27b (Ollama 0.40.2): three calls in a row each ran about 6 minutes, spent the whole 32,768-token output cap in the reasoning channel on a 30k prompt (Ollama saved a 62,602-token prompt+reply), and came back with nothing in the message; the second cap hit ended the run. Live probe the same evening: a one-line "is 1000003 prime?" was still reasoning at 3,000 tokens.
- Reasoning budget: new config `think_budget_tokens` (default 8192, ~90 s on the 3090; 0 = none). A reply still reasoning past it, with no answer or tool call begun, has its stream closed, which stops the server generating (`Reply.thinking_cut`).
- Thinking-off retry: a reply that is all reasoning and no answer (cut at the budget, stopped at the output cap, or empty with only reasoning) is asked again, identically, with Ollama `think: false`. The prompt is unchanged and no nudge is added, so the cached prefix is reused (live: the retry answered in 0.8 s). It is tried once per reply; if the thinking-off answer is still empty, the existing empty-reply nudges take over. A long real answer cut at the output cap still gets the 2.41.1 "smaller pieces" nudge. The REPL prints "reasoning ran N tokens with no answer; asking again with thinking off". Tests in tests/test_think_budget.py.

## 2.42.1
- Safety hint (wilbur/directives.py): a `run_bash` command that unbinds/binds/removes a host PCI/USB controller or driver via sysfs, or runs `modprobe -r`, now earns a `host_hw` tool-result hint -- that resets the controller, and a shared one may also carry the keyboard, disks or security keys. It steers to passing the device into a container (`--device`/`--net=host`/cgroup rules) instead and to stopping rather than retrying a failed host-level op. Rides in the tool RESULT, not the system prompt, so the cached prefix and its token budget are untouched (the regex also matches the /sys path inside a `docker run --privileged sh -c '...'`). Added after Wilbur's `xhci unbind` on 2026-10-24 reset USB bus 3 and dropped the Alfa adapters. Test in tests/test_directives.py.

## 2.42.0
- Syntax highlighting, our own code (no new dependency): wilbur/highlight.py lexes Python, JS/TS, shell, JSON, YAML, C/C++, C#, PHP, SQL, Markdown and diff, colouring keywords, strings, comments, numbers and builtins in the 256-colour palette. Fenced code blocks in replies and `read_file` views in tool results are highlighted; Python triple-quoted strings and `/* */` comments carry across lines. A 2,000-line file renders in about 17-22 ms (tests/test_highlight.py, tests/test_render.py).
- Light markdown for replies (wilbur/render.py): headings, bold, italic, `inline code`, bullet and numbered lists, block quotes, rules, links (OSC 8, clickable where the terminal supports it, with the URL shown) and tables aligned by display cells, so wide characters keep columns straight. `MarkdownStream` renders chunk by chunk, so a fence that opens in one chunk and closes in a later one still renders as code.
- Coloured diffs for `write_file` and `edit_file`, like Claude Code's edit display: "Updated <path> with N additions and M removals  +N −M", then numbered rows, removed lines red and added lines green, two lines of context and `...` between distant hunks, capped at 40 rows.
- Status line: the pinned bottom toolbar shows context used as a % of `num_ctx` (accent past 60%, red past 85%) and the session's tokens per second.
- Display only: the system prompt, tool schemas and model I/O are unchanged, and saved sessions keep the raw text. NO_COLOR or a non-TTY stdout prints everything byte for byte unstyled.
- Welcome box aligned in every terminal: its marker is now `●` (one cell everywhere) instead of `⏺`, which COSMIC draws as a 2-cell emoji, and lines are padded by display cells rather than code points (tests/test_ui_render.py).
- `ui.visible_width` skips OSC sequences (hyperlinks) as zero width.

## 2.41.1
- No more false "model server not responding" during long generations. Measured 2026-10-21 on huihui_ai/qwen3.8-abliterated:27b (Ollama 0.40.2): while the model writes a tool call, Ollama sends no bytes until the call is complete. Wilbur's socket sat 109 s without a byte while the server's token count climbed from 7,215 to 9,489, so a long `write_file` hit the 120 s stall window and Wilbur cancelled a healthy generation (22:14 and 22:16 that night). When the window passes, Wilbur now asks the server whether it is still generating and keeps waiting while it is, showing "model still generating (Ns without output)". Only a server that has stopped raises the error. The check reads Ollama `/api/ps`: a model's `expires_at` is refreshed when a request ends, so an expiry set before the silence began means nothing has finished. Server time comes from the response's Date header, so the check also works through the contrib/remote-gpu tunnel. It falls back to local `nvidia-smi` for a loopback server, and to the old behaviour when neither can tell. The stream is read on a helper thread, because a timed-out socket file cannot be resumed. New config `stall_busy_check` (`auto`, `ps`, `gpu` or `off`; default `auto`) and `stall_busy_max_s` (default 600). `first_token_timeout_s` (default 120) is still the stall window.
- Output cap per reply: new config `num_predict` (default 32768 tokens; 0 = none) is sent to Ollama. A reply stopped at the cap (`done_reason` `length`) prints "reply hit the output cap … and was cut off". The model is told once to continue in smaller pieces; a second cut-off ends the turn with the partial reply marked as cut off.
- New tool rule: write a large file or long script in parts of about 200 lines or fewer, and split a big step into several tool calls (tests in tests/test_stall_busy.py).

## 2.41.0
- Tool-use directives: the system prompt now says which tool to reach for and when (wilbur/directives.py). Measured on 44 saved sessions: run_bash 954 calls against grep 15 and glob 4 (424 of them shell grep/find/ls), raw pytest in run_bash 45 times against run_tests 9, list_skills never called, gate-run never used, and 17 of 47 "done" replies came after a write with no test run since. Seven conditional rules cover search (grep/glob), re-reads, run_tests while iterating and before "done", task for many-file work, skills, and `git worktree` for a risky change in a dirty shared tree. Host rules for `gate-run --max-load 8`, `tb` and `session-claim` are added only when that command is on PATH (probed once per process, so the static prompt prefix never changes mid-session), and a rule whose tool a restricted subagent lacks is left out.
- One-time tool hints: when a call takes the expensive road (shell grep/find, bare pytest outside gate-run, six searches in a row that belonged in a task), the result carries a single `[harness] tip` naming the right tool, once per kind per session. It rides in the tool result, never the system prompt. New config `tool_hints` (default true).
- Smaller prompt: the persona block's working rules repeated six of the system prompt's rules word for word and are gone; the chunking rule was stated twice. System prompt with all rules: 5,289 chars, about 1,511 tokens, down from 5,593 chars (1,598) at 2.40.1 (tests in tests/test_directives.py).

## 2.40.1
- Append-only harness briefing, so the Ollama prompt prefix never diverges before the end of the previous prompt. Since 2.38.0 the briefing was removed and re-sent as the last message on every call, so each prompt diverged at the old briefing's start. huihui_ai/qwen3.8-abliterated:27b is architecture qwen35 (hybrid, recurrent layers): llama.cpp can resume it only from a context checkpoint, created only near the end of each prompt (n_tokens-513 and n_tokens-1). A briefing over ~512 tokens (live session: about 640) put both checkpoints past the divergence, so llama.cpp erased them and re-read from position 0: on 2026-10-20, 33 of ~50 big-context calls re-read 36-41k tokens (34-40 s each). The briefing is now appended to the stored transcript as a user-role note only when its text differs from the last stored briefing, and is never removed or edited, so every call's message list extends the previous call's exactly (tests/test_prompt_cache_prefix.py, with a control that rejects the 2.40.0 shape). Compaction drops old briefings instead of summarising them (one re-read), and the current briefing is re-appended on the next call.

## 2.40.0
- Long-task robustness. On long tasks an abliterated thinking model would try, retry, return a wall of text, and then spin without finishing. Four guards in the agent loop stop that:
  - **Empty-reply recovery.** When a turn puts everything in the reasoning channel and leaves `content` empty with no tool call, the harness nudges firmly ("put your answer in the message, not the reasoning; call a tool or state what is left") up to `empty_reply_retries` times (new config, default 2). If the retries are spent but the model did reason, a short summary of the reasoning is surfaced (truncated to ~1500 chars with a "(reasoning truncated)" note) and the run is allowed to carry on, instead of hard-stopping a task still making progress or dumping the whole wall of text. Only a truly empty reply (no reasoning) stops the run. The empty counter resets on any non-empty or tool reply and is bounded, so an always-empty model still terminates with a clear reason.
  - **Tool-failure circuit breaker.** After `tool_failure_block_at` identical-signature failures (default 3) the harness hard-blocks that exact call: it is not executed again and the model is told to change the tool, arguments, or approach. After `tool_failure_stop_at` consecutive failing tool calls (default 6) with no successful write or progress between them, the run checkpoints and stops cleanly with a "stuck: N failures; last error ...; next step ..." summary rather than spinning to the round cap. Any successful write resets the counter.
  - **Runaway-output cap.** A no-tool reply whose `content` exceeds `runaway_content_chars` (default 8000) for two turns running is truncated in the transcript and nudged for a concrete next action, so a wall of prose cannot recur every turn.
  - **Long-task continuity.** The round-cap path still checkpoints and carries on, and the empty-reply and tool-failure recovery never hard-stop a task that is still progressing (tests in tests/test_long_task_robustness.py and tests/test_keepgoing.py).

## 2.39.1
- Fixed a model-reload loop introduced in 2.39.0: the 60 s keep-alive re-arm sent `/api/generate` with no `options`, so Ollama reloaded the model at its default context and the next chat reloaded it again at `num_ctx` 65536 (a reload pair every 1-3 minutes, each about 60 s load plus 30 s re-reading the context). The re-arm is gone; every `/api/chat` request now carries `keep_alive` (new config `ollama_keep_alive`, default `30m`), so the model stays resident with the options it was loaded with. The foreign-model watcher is unchanged and never posts for Wilbur's own model; exit still unloads with `keep_alive` 0 (tests in tests/test_model_reload_loop.py).

## 2.39.0
- Exclusive GPU for the whole session (owner 2026-10-18: "wilbur should get exclusive use when already in session, nothing should be able to take it away"). Terminal Wilbur takes gpu-queue wait mode when the session starts and keeps it until it exits: between turns it no longer hands the GPU back, so gpu-queue admits no other job while a session is open. Queued jobs wait; a job already running when the session starts finishes first (or `/override` stops it). The heartbeat runs for the whole session, granted or still waiting, and a wait mode gpu-queue lost (expired, or released by another `wilbur` client) is re-taken on the next heartbeat. A crashed session stops heartbeating, so gpu-queue's idle TTL (600 s) frees the card. (2026-10-18 00:41 the per-turn release let a tor-survey lease in at 00:48 and its qwen2.5:14b evicted Wilbur's 27B.)
- Wilbur's model stays loaded while the session holds the GPU: keep_alive is re-armed to 15 min every 60 s, only for a model already resident (never loads one behind the queue's back). On exit the model is unloaded, but only when this session held the grant; a crashed session's model is freed by Ollama 15 min later (tests in tests/test_gpu_session_hold.py).
- A model loaded straight into Ollama (bypassing gpu-queue) while the session holds the GPU is unloaded within a minute and logged. Wilbur's own models (main, subagent, reviewer, `WILBUR_VOICE_MODEL`, and `WILBUR_GPU_KEEP_MODELS`) are kept; `WILBUR_GPU_EVICT_FOREIGN=0` turns this off. It is reactive: it ends an intruding load but cannot stop the load from evicting Wilbur's model first.

## 2.38.0
- Prompt-prefix cache stays warm between calls. The harness briefing (objective, plan, failure ledger) is now sent as the last message of each call, as a user-role note, and is never stored in the transcript; it used to be pinned right after the system prompt and regenerated every turn, so any plan tick or new failure made Ollama re-read the whole history (48,813 and 49,402 tokens, ~48 s each, on consecutive turns). A system-role briefing at the end would not help: Ollama collates every system message into the top block. A restored pre-2.38 session drops its stored briefing on the first call (tests in tests/test_prompt_cache_prefix.py).
- `/compact` compacts the history now and prints tokens before -> after; with too little history it says so.
- Live status line during model calls: `reading ~N tokens`, then `thinking`, then `writing`, each with its own elapsed seconds and the turn total. The Ollama client now streams (`stream: true`) and folds the chunks into one reply.
- Stall handling: no bytes from the model server for `first_token_timeout_s` (default 120 s) raises "model server not responding" at once instead of a silent wait of up to 600 s per attempt with retries; `/retry` asks again. Connection failures and 5xx are still retried with backoff. A null `prompt_eval_count`/`eval_count` is read as 0 (tests in tests/test_responsiveness.py).

## 2.37.5
- Compaction triggers on the real prompt size. `_maybe_compact` now uses the larger of the chars/token estimate and the prompt_tokens Ollama reported for the last call plus the estimate of what was added since, and the estimate counts the tool schema sent with every call. A long session whose estimate (45,004) sat just under the 45,056 budget while the real prompt was 64,582 of 65,536 never compacted and stopped with `empty_reply`.
- An empty reply that comes back with the window full (prompt_tokens within `reserve_output` of `num_ctx`) is compacted and retried once instead of counting toward the empty-reply stop (tests in tests/test_compact_real_prompt.py).

## 2.37.4
- Session save writes through a per-writer tmp file (pid and thread in the name) and removes it when the write or rename fails, so two saves of one session cannot interleave bytes and a failed save leaves no stray `.tmp` (tests in tests/test_session.py).
- RUNBOOK.md (private build): what runs where, start/stop, publish and the owner's local update (editable install), rollback, recovery and health checks.
- BACKLOG: `deps-ownership` and `enterprise-gaps` lists.

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
