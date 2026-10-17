"""run.json `metrics`: the shared agent-metrics record (2.36.0)."""
from __future__ import annotations

import json

import pytest

from wilbur import run_metrics, runstate


def _usage(prompt=0, completion=0, context=None, calls=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "context": context, "calls": calls, "seconds": 0.0}


def test_usage_from_stats_carries_context_and_unknown_before_first_call():
    assert runstate.usage_from_stats({"in_tokens": 0})["context"] is None
    u = runstate.usage_from_stats({"in_tokens": 900, "out_tokens": 40, "last_prompt_tokens": 512})
    assert u["context"] == 512 and u["total_tokens"] == 940


def test_public_build_has_no_metrics_key(monkeypatch):
    monkeypatch.setattr(run_metrics, "_am", None)
    p = runstate.write("pub", state=runstate.WORKING, usage=_usage(10, 5))
    data = json.loads(p.read_text())
    assert "metrics" not in data and "metrics_samples" not in data



def test_metrics_fault_never_costs_the_run_json_write(monkeypatch):
    def boom(*a, **k):
        raise TypeError("malformed usage row")
    monkeypatch.setattr(run_metrics, "record", boom)
    p = runstate.write("fault", state=runstate.WORKING, usage=_usage(10, 5))
    assert p is not None
    data = json.loads(p.read_text())
    assert data["state"] == runstate.WORKING and "metrics" not in data


needs_pkg = pytest.mark.skipif(not run_metrics.available(), reason="jh-agent-metrics not installed (public build)")


@needs_pkg
def test_record_is_valid_and_keeps_unknown_cache_fields():
    import jh_agent_metrics as am
    rec, ring = run_metrics.record("r1", state="working", model="m:1", started_at=1000.0,
                                   usage=_usage(300, 20, context=300, calls=1), now=1060.0)
    assert am.validate(rec) == []
    assert rec["source"] == "wilbur" and rec["state"] == "running" and rec["elapsed_s"] == 60.0
    assert rec["tokens"]["total"] == 320 and rec["tokens"]["cache_read"] is None
    assert rec["context"] == 300 and rec["calls"] == 1
    assert rec["tokens_per_min"] is None  # one sample: UNKNOWN, not 0
    run_metrics.forget("r1")


@needs_pkg
def test_per_min_comes_from_the_sample_ring():
    run_metrics.record("r2", state="working", model="m", started_at=0.0, usage=_usage(0, 0), now=1000.0)
    rec, ring = run_metrics.record("r2", state="working", model="m", started_at=0.0,
                                   usage=_usage(400, 100), now=1060.0)
    assert len(ring) == 2
    assert rec["tokens_per_min"] == 100.0 and rec["fresh_per_min"] == 100.0  # 500 / 5-min window
    run_metrics.forget("r2")


@needs_pkg
def test_runstate_write_embeds_metrics_with_subagents_and_remove_forgets():
    import jh_agent_metrics as am
    runstate.write_subagents("r3", [{"id": 1, "status": "error", "state": "error"},
                                    {"id": None, "state": "running"}])
    p = runstate.write("r3", state=runstate.WAITING_INPUT, started_at=1.0, usage=_usage(10, 5, context=10))
    data = json.loads(p.read_text())
    assert am.validate(data["metrics"]) == []
    assert data["metrics"]["state"] == "waiting"
    assert data["metrics"]["subagents"] == [{"id": "1", "state": "failed", "tokens": None}]
    assert isinstance(data["metrics_samples"], list) and data["metrics_samples"]
    runstate.remove("r3")
    assert "r3" not in run_metrics._samples
