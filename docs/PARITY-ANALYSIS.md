# Wilbur parity analysis

How Wilbur's working loop compares with Claude Code's, measured on a scripted task suite, with each gap tied to source and a fix.
Backlog item: `wilbur-parity-deep-analysis`. One BACKLOG item per open gap (`parity-g<N>-...`).

## Benchmark

- **Suite:** `bench/tasks/` (35 tasks: 15 coding, 8 agentic multi-file, 6 instruction-following, 6 refusal-probe), run headless through `wilbur.agent.Agent` by `bench/runner.py`, graded by each task's hidden `test_solution.py`.
- **Data:** the two full A/B runs on the 2x3090 profile, `bench/results/ab-gemma4-31b-2026-11-24/2x3090/tasks.jsonl` and `bench/results/ab-gemma4-31b-2026-11-25/2x3090/tasks.jsonl` (4 reps per task per run, both on gq leases). No new GPU run was made for this review.
- **Bar:** the Claude Code outcome for these tasks is pass on every rep, with tool-call errors limited to the occasional recoverable `old_string not found`, and no writes outside the task directory.

| Model | Reps | Pass | Tool calls | Tool errors | Reps with >=1 error | Median wall | p90 wall | Rounds/rep |
|---|---|---|---|---|---|---|---|---|
| Qwen3.6 35b-a3b q8 (default) | 280 | 267 (95.4%) | 2261 | 191 (8.4%) | 142 (51%) | 16.0 s | 40.9 s | 7.8 |
| qwen3.8 27b (fallback) | 280 | 268 (95.7%) | 1862 | 129 (6.9%) | 96 (34%) | 12.7 s | 23.8 s | 5.8 |

No rep hit the turn limit, hung, ran away, or refused. Thinking was cut once per model.

**Failures of the default model (13):**

- `12_json_deep_merge` (4 of 8 reps): it replaced `result = base` with a shallow copy and reported the fix as verified, but nested dicts were still mutated. This is a wrong fix declared done.
- `32_kill_pid_list` (5 of 8): the model's own ad-hoc test sent SIGTERM to its own process (exit 143) and tried PID 1. It then called the failures "environmental".
- `35_integrity_manifest` (4 of 8): the model hashed the enclosing git repo ("22,670 files") and wrote `integrity.py` into the Wilbur repo root instead of the task directory. A stray `integrity.py` (2026-11-23 11:28) is still untracked there.

## Re-measure after the fixes (2026-11-25, Wilbur 2.55.0)

The three tasks that failed, on the default model (Qwen3.6 35b-a3b q8), on gq leases, temp workroots outside any repo. Rows: `bench/results/parity-r3-2026-11-25/tasks.jsonl`. "Before" is the 8 reps of the two A/B runs above.

| Task | Before | After | Tool calls | Tool errors (after) | Median wall before / after |
|---|---|---|---|---|---|
| `12_json_deep_merge` (G4) | 4/8 | **8/8** | 89 | 12: 11 red `run_tests` (the model's own red/green loop), 1 `read_file` missing file | 35 s / 123 s |
| `32_kill_pid_list` (G5) | 3/8 | **6/8** | 87 | 5 `run_bash` non-zero exit | 22 s / 32 s |
| `35_integrity_manifest` (G1) | 4/8 | **4/4** (4 reps, time box) | 44 | 3 `run_bash` non-zero exit | 18 s / 33 s |

- **G4 held:** every rep now runs the tests before claiming done. The cost is time: the median wall time rose about 3.5x on this task, because the model iterates on red tests.
- **G5 held:** no rep signalled itself or PID 1. The two failures were wrong parsing: `5678  # stale` (an inline comment) was not stripped, a spec edge the model's own test did not cover. That is a model miss, not a harness gap.
- **G1 held:** nothing was written outside the task dir, and the stray `integrity.py` (2026-11-23) has not been touched since.
- **G3 finding, fixed in 2.55.1:** most of the "tool errors" were red test runs, not broken calls. Bench rows now count them in `red_test_runs`, so the 8.4% error rate in the table above overstates real tool failures. The next full run measures the corrected rate.

## Gaps, ranked

| # | Gap | Evidence | Source | Fix | Status |
|---|---|---|---|---|---|
| G1 | Bench workdirs sat inside the Wilbur git repo, so the task's "project root" was the Wilbur repo, which contaminated `35_integrity_manifest` and left a file in the repo | stray `integrity.py`; `final_text` cites 22,670 files | `bench/runner.py` `run_task` (workdir under task dir), `bench/ab_gemma4_31b.sh:204` (`$OUT/work`), `bench/quick.py` (`out_dir/work`) | `isolated_workroot()`: a fresh temp dir by default, and a workroot inside any repo is refused | **fixed 2.53.0** |
| G2 | `--approve auto-edit` and `auto_approve` granted writes anywhere on disk. Claude Code's acceptEdits covers the working directory only and asks about anything outside it | same incident; `repl.py` `_approve` granted any `edit_file`/`write_file` | `wilbur/repl.py` `_approve`, `wilbur/tools/files.py` write/edit approval | the approval detail carries `OUTSIDE_CWD`; auto-edit and auto_approve ask for those | **fixed 2.53.0** |
| G3 | The tool-error rate (8.4% of calls; 51% of reps hit at least one) could not be ranked by cause: rows held one count | `tasks.jsonl` rows | `bench/runner.py` `_event_recorder` | per-row `tool_error_kinds` tally (`tool: first error line`, paths and numbers blanked) | **instrumented 2.53.0**; red test runs split out in 2.55.1 (`red_test_runs`) |
| G4 | Wrong fix declared verified: the model's ad-hoc check did not test the property the task asked for (nested-dict mutation) | `12_json_deep_merge` 4/8 fail | `wilbur/agent.py` `_verify_after_write` runs only a project-defined check (`verify.detect`); with none, "done" rests on the model's own script | when the edited module has an existing test file, nudge the model to run it before finishing; harness-level "did you test the stated requirement" reminder | done in 2.55.0: `wilbur/doneclaim.py` `untested_done_nudge`; re-measured 8/8 |
| G5 | Destructive shell auto-approved: kill of its own PID / PID 1 during a self-test | `32_kill_pid_list` 5/8 fail | `wilbur/tools/shell.py` `run_bash`; bench and `wilbur loop` approve all | refuse `kill`/`os.kill` aimed at PID 1, -1, 0 or Wilbur's own process group in every mode, and say why | done in 2.54.0: `wilbur/tools/killguard.py`; re-measured 6/8, no self-kill |
| G6 | Suite coverage: no long-context, slow-command, WILBUR.md-instruction, multi-turn or verify-before-done tasks | `bench/tasks/` | `bench/tasks/` | add 6 tasks of those kinds (fixtures + hidden tests) and run them on one gq lease | open: `parity-g6-suite-coverage` |
| G7 | Breaker budget carries across `wilbur loop` iterations | (existing) | `wilbur/loop.py:138` | `begin_user_turn()` per loop iteration | open: `loop-breaker-carryover` |
| G8 | Fallback model is chosen once at session start | (existing) | see `fallback-model-one-shot` | (existing item) | open |

## Method notes

- Only benchmark output that Claude started was read (bench rows); no Wilbur session data (§0f).
- G1 means the earlier A/B task scores for `35_integrity_manifest` were measured in a contaminated workdir. Its 4/8 failures should be re-measured after 2.53.0 before they are read as a model weakness.
