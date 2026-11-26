# W3 live checks, 2026-11-26

Clean-room (CLEANROOM.txt): both 3090s at 1 MiB / 0 %, `ollama ps` empty, no Wilbur process, before the run.
Model `huihui_ai/Qwen3.6-abliterated:35b-a3b-q8_0`, `gpu_indices [0,1]`, `kv_cache_type q8_0`. Scratch repos, config,
sessions and history under `/mnt/work12/wilbur-live-2611/` (built by `make_repos.py`, driven by `drive.py` through the
real `Repl`, all inside ONE gq lease per run by `lease_all.py`). Logs stay on that disk.

## Run 1: 2.61.0, gq lease db9f544653fb (00:26-00:36)

| item | scenario | result |
|---|---|---|
| keep-working-regression-20261026 | A: 12 x ~4.7k-token modules, fix a bug in each, num_ctx 32768 (forces compaction) | **FAIL**: the model read all 12 in one round (~56.8k tokens), the next round compacted them to ~3.7k before the model saw them, and it re-read all 12. 7 compactions, 96 reads, 0 edits in 359 s; stopped by PID. Never asked or stopped: it thrashed. |
| keep-going-live-ab-verify | B: 5-item task list, REPL, auto-continue on, num_ctx 131072 | **PASS for keep-going** (items 1-4 done, no "continue" needed, no keep-going stop), but the run then STOPPED on `RuntimeError: ollama: XML syntax error on line 72: element <function> closed by </parameter>` (a tool call Ollama could not parse); item 5 not done. 96 s. |
| overnight-live-run-verify-team-mode | C: `wilbur loop --once` team task top_words (queue-c.json) | **PASS**: planner 17.7 s, tester 32.9 s, coder 51.9 s, reviewer 19.5 s, `red->green`, APPROVE, `make test: PASS`, 122 s. Criteria re-checked independently on commit d52bfd5 (all four asserts + README heading + make test OK). |

## Fixes in 2.62.0

- Round read overflow: reads after the first in a round that would cross the compaction budget are held back with a note.
- Tool-call parse errors are retried (up to 2) with a harness note instead of ending the run.

## Run 2: 2.62.0 (worktree), gq lease 67083c0e402e (from 00:39)

| item | scenario | result |
|---|---|---|
| keep-working-regression-20261026 | A (same repo, rebuilt) | **PASS**: 17 reads held back, 6 compactions, 29 reads, 15 edits/commands, all 12 modules fixed, `unittest` OK, ended `done` with no question, 2 agent runs (one auto-continue), 391 s. |
| keep-going-live-ab-verify | B (same list) | **PASS**: the Ollama tool-call parse error happened again once; "model server could not parse a tool call -- asking again (1)", then all 5 items done (tests OK, `mean 1 2 3` -> 2.0, mode/median correct, USAGE.md written), ended `done`, 3 agent runs, no "continue" typed, 101 s. |

Not run here: `long-run-health-review-20261024` needs Wilbur's runtime logs, which require the owner's explicit word.
A ran at num_ctx 32768 to force compaction; the owner's 131072 window takes the same code path with larger inputs.
