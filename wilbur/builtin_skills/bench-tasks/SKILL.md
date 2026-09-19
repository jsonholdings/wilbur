---
name: bench-tasks
description: How to add a new task under bench/tasks/ for the agent-quality benchmark
---
`bench/` measures Wilbur's agent loop against fixed coding tasks (see `bench/README.md`).

To add `bench/tasks/<NN_name>/`:

1. Pick the next two-digit prefix (`NN`) after the highest existing task dir, so tasks stay
   ordered by when they were added.
2. `task.json`: task id, the prompt given to the agent, the entry file path (relative to the
   staged tree), and the test file path.
3. `start/`: the buggy or incomplete starting tree the agent is handed.
4. `reference/`: a known-good solution to the same tree.
5. `test_solution.py`: a pytest file that MUST fail against `start/` and pass against
   `reference/` -- prove both directions before committing, and record the real command and
   output in `bench/VALIDATION.md` per the existing entries there.
6. Do not touch `bench/tasks/07_*` or `wilbur/tools/test_runner.py` -- another owner's area.

Dry-run the whole suite with `python3 bench/runner.py --dry-run --reps 2` then
`python3 bench/summarize.py bench/results.jsonl` before considering a new task done; `--dry-run`
uses a scripted fake model, not a live one, so this proves the harness wiring, not model quality.
