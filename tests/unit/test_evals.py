"""Eval-driven quality gate: CI fails if a change makes offline ranking worse."""

from __future__ import annotations

import pytest

from jobpilot.evals import PersonaResult, _spearman, load_dataset, report_markdown, run_eval
from jobpilot.llm.fake import FakeLLM, HashEmbedder
from tests.fakes import scores_responder


def test_dataset_is_complete() -> None:
    data = load_dataset()
    job_ids = {j["id"] for j in data["jobs"]}
    pairs = sum(len(p["labels"]) for p in data["personas"])
    assert pairs == 30
    for persona in data["personas"]:
        assert set(persona["labels"]) == job_ids
        assert set(persona["labels"].values()) <= {"good", "ok", "bad"}


async def test_offline_ranking_quality_gate() -> None:
    results = await run_eval(HashEmbedder())
    for r in results:
        assert r.pairwise_accuracy() >= 0.9, r.persona
        assert r.spearman() >= 0.7, r.persona
    report = report_markdown(results, embedder_name="hash", llm_name="off")
    assert "| **mean** |" in report


async def test_eval_with_stage2() -> None:
    results = await run_eval(HashEmbedder(), FakeLLM({"JobScores": scores_responder()}))
    assert all(max(r.scores.values()) > 60 for r in results)


def test_metric_helpers() -> None:
    r = PersonaResult("p", {"a": 90, "b": 50, "c": 10}, {"a": "good", "b": "ok", "c": "bad"})
    assert r.ranked() == ["a", "b", "c"]
    assert r.precision_at(1) == 1.0
    assert r.pairwise_accuracy() == 1.0
    assert _spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
