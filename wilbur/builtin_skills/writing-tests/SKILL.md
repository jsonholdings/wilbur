---
name: writing-tests
description: How to write a Wilbur test using this repo's fixtures and control-test convention
---
This repo's tests live under `tests/`, run with pytest.

- Use the `ctx`/`tools` fixtures (see `tests/test_tools.py`): `ctx(tmp_path)` builds a
  `ToolContext` with `approve=lambda tool, detail: Approval.GRANTED`; `tools()` calls
  `build_registry()`. Build a single tool directly (e.g. `MyTool()`) instead when you don't
  need the whole registry.
- Every test that isolates Wilbur's home directories relies on the autouse
  `isolate_wilbur_home` fixture in `tests/conftest.py`, which monkeypatches
  `CONFIG_PATH`/`SESSION_DIR`/`HISTORY_PATH` (and any similar path you add) into `tmp_path`.
  If you add a new module-level path constant that can be written to, add a matching
  `monkeypatch.setattr` there -- otherwise a test can leak into the owner's real
  `~/.config/wilbur`.
- **Write a control test for every "X is skipped/not found" assertion.** A check that never
  ran and a check that correctly found nothing both print nothing. Prove the negative is real:
  put a well-formed sibling next to the malformed thing you're testing and assert the sibling
  IS still found/handled.
- Prefer asserting on tool `ToolResult.output`/`is_error`, not on internal source structure.
- Name test files `test_<module>.py` matching the module under test (e.g. `wilbur/skills.py`
  -> `tests/test_skills.py`).
