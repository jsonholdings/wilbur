"""`wilbur -p --output-format json|stream-json`: valid JSON on stdout, the
documented exit codes, and text mode byte-identical to before (the control)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.__main__ as main_mod
import wilbur.agent as agent_mod
from wilbur import headless
from wilbur.config import Config
from wilbur.fake_backend import FakeClient
from wilbur.llm import Reply, ToolCall


def _script(tmp_path, replies):
    # A fresh path per run: the fake backend keeps one queue per script path.
    path = tmp_path / f"script-{len(list(tmp_path.glob('script-*.json')))}.json"
    path.write_text(json.dumps(replies))
    return str(path)


def _run(tmp_path, capsys, replies, fmt):
    argv = ["--backend", "fake", "--fake-script", _script(tmp_path, replies),
            "-C", str(tmp_path), "-p", "list files"]
    if fmt:
        argv += ["--output-format", fmt]
    code = main_mod.main(argv)
    return code, capsys.readouterr()


TOOL_THEN_ANSWER = [
    {"content": "", "tool_calls": [{"name": "glob", "arguments": {"pattern": "*.txt"}}]},
    {"content": "found a.txt"},
]


def test_json_gives_one_valid_object(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")
    code, out = _run(tmp_path, capsys, TOOL_THEN_ANSWER, "json")
    assert code == 0
    lines = out.out.strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["type"] == "result" and obj["result"] == "found a.txt"
    assert obj["exit_code"] == 0 and obj["is_error"] is False
    assert [c["name"] for c in obj["tool_calls"]] == ["glob"]
    assert obj["tool_calls"][0]["error"] is False
    assert obj["usage"]["model_calls"] == 2
    assert obj["session_id"]


def test_stream_json_one_object_per_line(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")
    code, out = _run(tmp_path, capsys, TOOL_THEN_ANSWER, "stream-json")
    assert code == 0
    objs = [json.loads(line) for line in out.out.strip().splitlines()]
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "assistant" and "tool_start" in kinds and "tool_end" in kinds
    assert kinds[-1] == "result" and kinds.count("result") == 1
    assert objs[-1]["result"] == "found a.txt"


def test_text_mode_is_byte_identical_with_and_without_the_flag(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")
    code_default, out_default = _run(tmp_path, capsys, TOOL_THEN_ANSWER, None)
    code_text, out_text = _run(tmp_path, capsys, TOOL_THEN_ANSWER, "text")
    assert code_default == code_text == 0
    assert out_default.out == out_text.out == "found a.txt\n"
    # The control: json mode really does change stdout.
    _, out_json = _run(tmp_path, capsys, TOOL_THEN_ANSWER, "json")
    assert out_json.out != out_text.out


def test_usage_sums_backend_token_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([
        Reply(content="", tool_calls=[ToolCall(name="glob", arguments={"pattern": "*"})],
              prompt_tokens=100, eval_tokens=5),
        Reply(content="done", tool_calls=[], prompt_tokens=120, eval_tokens=7),
    ]))
    import io
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = main_mod._oneshot_headless(Config(), str(tmp_path), "go", auto=True, fmt="json")
    obj = json.loads(buf.getvalue())
    assert code == 0
    assert obj["usage"] == {"prompt_tokens": 220, "output_tokens": 12, "model_calls": 2}


def test_backend_error_exits_1_with_json(monkeypatch, tmp_path, capsys):
    class Boom(FakeClient):
        def chat(self, *a, **kw):
            raise RuntimeError("backend died")
    monkeypatch.setattr(agent_mod, "OllamaClient", Boom([]))
    code = main_mod._oneshot_headless(Config(), str(tmp_path), "go", auto=True, fmt="json")
    obj = json.loads(capsys.readouterr().out)
    assert code == 1 == obj["exit_code"] and obj["is_error"] is True
    assert "backend died" in obj["error"]


def test_interrupt_exits_130(monkeypatch, tmp_path, capsys):
    class Stop(FakeClient):
        def chat(self, *a, **kw):
            raise KeyboardInterrupt
    monkeypatch.setattr(agent_mod, "OllamaClient", Stop([]))
    code = main_mod._oneshot_headless(Config(), str(tmp_path), "go", auto=True,
                                      fmt="stream-json")
    obj = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 130 == obj["exit_code"]


def test_fake_backend_without_script_exits_2_with_json(tmp_path, capsys):
    code = main_mod.main(["--backend", "fake", "-C", str(tmp_path), "-p", "x",
                          "--output-format", "json"])
    obj = json.loads(capsys.readouterr().out)
    assert code == 2 == obj["exit_code"] and obj["is_error"] is True


def test_fake_backend_without_script_text_mode_prints_no_json(tmp_path, capsys):
    code = main_mod.main(["--backend", "fake", "-C", str(tmp_path), "-p", "x"])
    assert code == 2 and capsys.readouterr().out == ""


def test_missing_script_file_exits_2_with_json(tmp_path, capsys):
    code = main_mod.main(["--backend", "fake", "--fake-script", str(tmp_path / "nope.json"),
                          "-C", str(tmp_path), "-p", "x", "--output-format", "json"])
    obj = json.loads(capsys.readouterr().out)
    assert code == 2 == obj["exit_code"] and "not found" in obj["error"]


def test_turn_limit_exits_3_in_json(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")
    loop = [{"content": "", "tool_calls": [{"name": "glob", "arguments": {"pattern": f"{i}*"}}]}
            for i in range(3)]
    config = Config()
    config.backend, config.fake_script, config.max_turns = "fake", _script(tmp_path, loop), 2
    code = main_mod._oneshot_headless(config, str(tmp_path), "go", auto=True, fmt="json")
    obj = json.loads(capsys.readouterr().out)
    assert code == headless.EXIT_TURN_LIMIT == obj["exit_code"]
