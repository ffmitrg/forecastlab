"""Exercise Agent 6 through the graph, persistence, exports, and resume path.

These are deterministic integration tests with frozen model outputs, not a live
LLM quality benchmark. No API key or network request is needed.
"""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import create_app
from app.demo import DEMO_QUESTION, demo_evidence
from app.graph import build_graph, execute
from app.schemas import Review, RunRecord
from app.storage import RunStore


FUTURE_ISSUE = {
    "severity": "high",
    "claim": "缺少未来的发布结果",
    "explanation": "2026年11月15日的实际发布结果尚未发生。",
    "affected_ids": ["E001"],
}
REAL_ISSUE = {
    "severity": "high",
    "claim": "把发布计划写成已经正式发布",
    "explanation": "E001 只说明计划，不能据此认定正式版已经发布。",
    "affected_ids": ["E001"],
}


class FrozenReviewModel:
    def __init__(self, review, *, can_estimate=True, fail_forecast=False,
                 world_summary="截至日仍处于开发阶段。"):
        self.review = deepcopy(review)
        self.can_estimate = can_estimate
        self.fail_forecast = fail_forecast
        self.world_summary = world_summary
        self.calls = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, role, payload, schema, instructions):
        self.calls.append((role, deepcopy(payload)))
        self.usage["calls"] += 1
        ids = [item["id"] for item in payload.get("evidence", [])]
        if role == "evidence":
            output = {"summary": "材料提供发布计划与开发约束。", "evidence_ids": ids}
        elif role == "world":
            output = {"summary": self.world_summary, "actors": [], "evidence_refs": ids}
        elif role == "review":
            output = deepcopy(self.review)
        elif role == "evidence_audit":
            output = {
                "can_estimate": self.can_estimate,
                "blocking_reasons": [] if self.can_estimate else ["资料不足以支持当前问题"],
            }
        elif role == "forecast":
            if self.fail_forecast:
                raise RuntimeError("temporary forecast outage")
            output = {
                "status": "completed",
                "conclusion": "计划提供按期发布的可能性，仍存在延期风险。",
                "probabilities": {"是": .6, "否": .4},
                "supporting": [{"text": "截至日前已公布目标日期。", "evidence_ids": ids[:1]}] if ids else [],
                "limitations": ["固定测试输出，不能视为实际预测。"],
            }
        else:
            raise AssertionError(f"Unexpected model role: {role}")
        return schema.model_validate(output)


def run_graph(tmp_path, raw_review, *, evidence=None, **model_options):
    model = FrozenReviewModel(raw_review, **model_options)
    record = RunRecord(run_id="run_review_policy", question=DEMO_QUESTION,
                       evidence_mode="import", model="frozen-test")
    state = build_graph(record, demo_evidence() if evidence is None else evidence,
                        model, tmp_path).invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    return state, model


def test_real_high_issue_stays_blocked_but_evidence_only_probability_is_allowed(tmp_path):
    raw = {"status": "blocked", "issues": [FUTURE_ISSUE, REAL_ISSUE]}
    state, model = run_graph(tmp_path, raw)

    assert state["review"]["status"] == "blocked"
    assert state["review"]["probability_basis"] == "evidence_only"
    assert any(issue["claim"] == REAL_ISSUE["claim"] for issue in state["review"]["issues"])
    assert all(issue["claim"] != FUTURE_ISSUE["claim"] for issue in state["review"]["issues"])
    assert state["forecast"]["probabilities"] == {"是": .6, "否": .4}
    forecast_payload = next(payload for role, payload in model.calls if role == "forecast")
    assert not {"world", "actions", "simulation", "review"} & forecast_payload.keys()
    trace = state["review_policy_audit"]
    assert trace["final_status"] == "blocked"
    assert trace["final_probability_basis"] == "evidence_only"
    assert trace["evidence_audit"]["can_estimate"] is True


@pytest.mark.parametrize("can_estimate,status,basis", [
    (True, "qualified", "evidence_only"),
    (False, "blocked", "none"),
])
def test_only_future_demand_relaxes_only_with_available_evidence_audit(
        tmp_path, can_estimate, status, basis):
    state, model = run_graph(tmp_path, {"status": "blocked", "issues": [FUTURE_ISSUE]},
                             can_estimate=can_estimate)
    assert state["review"]["status"] == status
    assert state["review"]["probability_basis"] == basis
    assert [role for role, _ in model.calls].count("evidence_audit") == 1
    assert (state["forecast"]["probabilities"] is not None) is can_estimate


def test_no_evidence_remains_blocked_without_an_evidence_audit_call(tmp_path):
    # Do not reference E001 because this run deliberately has no evidence IDs.
    future_issue = {**FUTURE_ISSUE, "affected_ids": []}
    state, model = run_graph(tmp_path, {"status": "blocked", "issues": [future_issue]}, evidence=[])
    assert state["review"]["status"] == "blocked"
    assert state["review"]["probability_basis"] == "none"
    assert state["forecast"]["probabilities"] is None
    assert "evidence_audit" not in [role for role, _ in model.calls]
    assert any(issue["claim"] == "证据包为空" for issue in state["review"]["issues"])
    assert state["review_policy_audit"]["evidence_audit"] is None


def test_snippet_only_caution_survives_future_demand_filter(tmp_path):
    snippets = [item.model_copy(update={"source_type": "snippet_only"}) for item in demo_evidence()]
    state, _ = run_graph(tmp_path, {"status": "blocked", "issues": [FUTURE_ISSUE]}, evidence=snippets)
    assert any(issue["claim"] == "来源仅有搜索片段" for issue in state["review"]["issues"])


@pytest.mark.parametrize("issue", [
    {"severity": "high", "claim": "缺少2026年11月15日的相关资料",
     "explanation": "尚不清楚这里指计划文件还是实际结果。", "affected_ids": ["E001"]},
    {"severity": "high", "claim": "缺少未来的发布结果，且引用原文与结论不符",
     "explanation": "E001 只写计划，报告却写已经发布。", "affected_ids": ["E001"]},
    {"severity": "high", "claim": "缺少未来的发布结果",
     "explanation": "最新复测记录尚未提供。", "affected_ids": ["E001"]},
])
def test_ambiguous_or_mixed_review_opinion_is_kept_whole(tmp_path, issue):
    state, _ = run_graph(tmp_path, {"status": "blocked", "issues": [issue]})
    assert issue in state["review"]["issues"]
    assert state["review"]["status"] == "blocked"
    decisions = state["review_policy_audit"]["decisions"]
    assert any(item["field"] == "issues" and item["index"] == 0 and item["action"] == "keep"
               for item in decisions)


def test_future_wording_in_world_cannot_erase_an_unexplained_review_block(tmp_path):
    state, _ = run_graph(tmp_path, {"status": "blocked"},
                         world_summary="缺少2026年11月15日的实际发布结果，无法判断。")
    assert state["review"]["status"] == "blocked"
    assert state["review"]["probability_basis"] == "evidence_only"


@pytest.mark.parametrize("original_status", ["passed", "qualified"])
@pytest.mark.parametrize("can_estimate", [True, False])
def test_world_only_future_gap_still_gets_evidence_audit_without_filtering_review(
        tmp_path, original_status, can_estimate):
    raw = {"status": original_status, "missing_evidence": ["最新复测记录"]}
    state, model = run_graph(tmp_path, raw, can_estimate=can_estimate,
                             world_summary="缺少2026年11月15日的实际发布结果，无法判断。")
    assert state["review"]["missing_evidence"] == raw["missing_evidence"]
    assert state["review"]["status"] == (original_status if can_estimate else "blocked")
    assert state["review"]["probability_basis"] == ("evidence_only" if can_estimate else "none")
    assert [role for role, _ in model.calls].count("evidence_audit") == 1
    trace = state["review_policy_audit"]
    assert trace["removed_future_demands"] is False
    assert trace["status_before"] == trace["status_after"] == original_status
    assert trace["decisions"][0]["action"] == "keep"
    if can_estimate:
        forecast_payload = next(payload for role, payload in model.calls if role == "forecast")
        assert "world" not in forecast_payload
        assert state["forecast"]["probabilities"] is not None
    else:
        assert state["forecast"]["probabilities"] is None


def test_kept_review_references_still_must_exist(tmp_path):
    issue = {**REAL_ISSUE, "affected_ids": ["E999"]}
    with pytest.raises(ValueError, match="审查意见.*不存在"):
        run_graph(tmp_path, {"status": "blocked", "issues": [issue]})


@pytest.mark.parametrize("issue", [FUTURE_ISSUE, REAL_ISSUE])
def test_positive_evidence_audit_cannot_override_cutoff_check(tmp_path, issue):
    late_evidence = [item.model_copy(update={"retrieved_at": DEMO_QUESTION.as_of + timedelta(days=1)})
                     for item in demo_evidence()]
    state, _ = run_graph(tmp_path, {"status": "blocked", "issues": [issue]},
                         evidence=late_evidence, can_estimate=True)
    assert state["review"]["status"] == "blocked"
    assert state["review"]["probability_basis"] == "none"
    assert state["forecast"]["probabilities"] is None
    trace = state["review_policy_audit"]
    assert trace["evidence_audit"]["can_estimate"] is True
    assert trace["evidence_available_at_cutoff"] is False


def test_unrelated_qualified_review_does_not_add_a_model_call(tmp_path):
    issue = {**REAL_ISSUE, "severity": "medium"}
    state, model = run_graph(tmp_path, {"status": "qualified", "issues": [issue]})
    assert state["review"]["status"] == "qualified"
    assert state["review"]["probability_basis"] == "full"
    assert [role for role, _ in model.calls] == ["evidence", "world", "review", "forecast"]
    assert state["review_policy_audit"]["evidence_audit"] is None


def test_audit_trace_survives_storage_export_and_forecast_only_resume(tmp_path, monkeypatch):
    raw = {"status": "blocked", "issues": [FUTURE_ISSUE, REAL_ISSUE],
           "missing_evidence": ["缺少截至日前已公布的2026年11月15日发布计划文件"]}
    model = FrozenReviewModel(raw, fail_forecast=True)
    monkeypatch.setattr(config, "MODEL_API_KEY", "test-only")
    monkeypatch.setattr("app.graph.ModelClient", lambda: model)
    store = RunStore(tmp_path)
    record = RunRecord(run_id="run_review_policy_resume", question=DEMO_QUESTION,
                       evidence_mode="import", model="frozen-test")
    execute(record, demo_evidence(), store)

    # Reload through a new store object so these assertions cannot accidentally
    # pass by observing an in-memory dict retained by execute().
    reloaded = RunStore(tmp_path).get(record.run_id)
    assert reloaded.status == "failed", reloaded.errors
    assert reloaded.failed_stage == "forecast"
    trace = deepcopy(reloaded.stage_outputs["review"]["review_policy_audit"])
    assert trace["version"]
    assert trace["raw_review"] == Review.model_validate(raw).model_dump(mode="json")
    assert trace["status_before"] == "blocked"
    assert trace["final_status"] == "blocked"
    assert trace["final_probability_basis"] == "evidence_only"
    assert trace["removed_future_demands"]
    decisions = {(item["field"], item["index"]): item for item in trace["decisions"]}
    assert decisions[("issues", 0)]["action"] == "drop"
    assert decisions[("issues", 1)]["action"] == "keep"
    assert decisions[("missing_evidence", 0)]["action"] == "keep"
    assert all(item["reason"] for item in decisions.values())

    snapshots = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (tmp_path / "runs" / record.run_id).glob("*.json")]
    assert any(snapshot.get("stage_outputs", {}).get("review", {}).get("review_policy_audit") == trace
               for snapshot in snapshots)

    with TestClient(create_app(tmp_path)) as client:
        export = client.get(f"/api/runs/{record.run_id}/export?format=json")
        assert export.status_code == 200
        assert export.json()["stage_outputs"]["review"]["review_policy_audit"] == trace
        completed_calls = len(model.calls)
        model.fail_forecast = False
        response = client.post(f"/api/runs/{record.run_id}/resume")
        assert response.status_code == 202
        finished = client.get(f"/api/runs/{record.run_id}").json()
        assert finished["status"] == "completed", finished["errors"]
        assert finished["resume_count"] == 1
        assert finished["stage_outputs"]["review"]["review_policy_audit"] == trace
        assert [role for role, _ in model.calls[completed_calls:]] == ["forecast"]
        resumed_export = client.get(f"/api/runs/{record.run_id}/export?format=json").json()
        assert resumed_export["stage_outputs"]["review"]["review_policy_audit"] == trace
