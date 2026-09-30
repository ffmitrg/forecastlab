from copy import deepcopy
import importlib
import importlib.util
import pytest
from app.schemas import QuestionFraming, QuestionSpec, ImportedEvidence, AssessmentCandidate, utcnow
from app.sources import import_evidence


def module():
    assert importlib.util.find_spec("app.agents.evidence"), "premise-level evidence analysis not implemented"
    return importlib.import_module("app.agents.evidence")


def setup(tmp_path, clear_framing):
    q = QuestionSpec(question="青岚社区的测试是否足以支持按期发布？", mode="scenario", as_of=utcnow())
    framing = QuestionFraming.model_validate(clear_framing)
    framing.premises[0].user_review = "retained"
    p2 = framing.premises[0].model_copy(update={"id": "P002", "content": "兼容测试已完成"})
    framing.premises.append(p2)
    retrieval = import_evidence([ImportedEvidence(file_id="test-material", title="项目公告", excerpt=
        "发布方宣布单元测试完成，但兼容测试仍未通过。")], q, tmp_path)
    e = retrieval.evidence[0]; p = e.passages[0]
    citation = {"evidence_id": e.id, "snapshot_hash": e.snapshot_hash, "paragraph_id": p.paragraph_id, "quote": "单元测试完成"}
    good = {"summary": "公告说明不同测试阶段的状态。", "findings": [
        {"target_premise_ids": ["P001"], "claim": "公告表示单元测试已完成", "relation": "supports", "citations": [citation], "limitation": "不是全部测试"},
        {"target_premise_ids": ["P002"], "claim": "兼容测试仍未通过", "relation": "challenges",
         "citations": [{**citation, "quote": "兼容测试仍未通过"}], "limitation": "不能据此确定未来发布日期"}]}
    return q, framing, retrieval, good


@pytest.mark.parametrize("field,value", [("evidence_id", "E999"), ("snapshot_hash", "wrong"), ("paragraph_id", "never-provided"), ("quote", "原文没有这句话")])
def test_each_finding_requires_valid_quote_and_active_premise(tmp_path, clear_framing, field, value):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    good["findings"][0]["citations"][0][field] = value
    valid, rejected = m.validate_findings(AssessmentCandidate.model_validate(good), frame, retrieval.evidence,
        {e.id: e.passages for e in retrieval.evidence})
    assert len(valid) == 1 and len(rejected) == 1
    assert valid[0].target_premise_ids == ["P002"]


def test_rejected_premise_never_enters_valid_findings(tmp_path, clear_framing):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    frame.premises[0].user_review = "rejected"
    valid, rejected = m.validate_findings(AssessmentCandidate.model_validate(good), frame, retrieval.evidence,
        {e.id: e.passages for e in retrieval.evidence})
    assert len(rejected) == 1 and all("P001" not in f.target_premise_ids for f in valid)


def test_one_source_supports_and_challenges_different_premises(tmp_path, clear_framing, mock_model):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    model = mock_model([good]); assessment = m.assess_evidence(q, frame, retrieval, model, tmp_path)
    assert {f.relation for f in assessment.findings} == {"supports", "challenges"}
    assert all(f.citations for f in assessment.findings)
    assert model.calls[0][1]["question_framing"]["draft_id"] == frame.draft_id


def test_second_invalid_result_is_excluded_and_logged(tmp_path, clear_framing, mock_model):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    good["findings"][0]["citations"][0]["quote"] = "编造的引文"
    model = mock_model([good, good]); assessment = m.assess_evidence(q, frame, retrieval, model, tmp_path)
    assert len(assessment.findings) == 1
    assert assessment.rejected_findings[0].reason
    assert model.call_count == 2
    assert any(g.cause == "validation_failed" for g in assessment.gap_details)


def test_conflict_scope_differences_remain_visible(tmp_path, clear_framing, mock_model):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    good["conflicts"] = [{"issue": "测试状态不同", "finding_indexes": [0,1], "scope_comparison": "单元测试与兼容测试是不同口径",
        "status": "resolved", "explanation": "这两项不是同一事实的直接矛盾"}]
    a = m.assess_evidence(q, frame, retrieval, mock_model([good]), tmp_path)
    assert a.conflict_details[0].status == "resolved"
    assert "不同口径" in a.conflict_details[0].scope_comparison
    assert a.conflicts and "已解释" in a.conflicts[0]


def test_future_resolution_is_not_evidence_gap(tmp_path, clear_framing, mock_model):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    good["gaps"] = [{"missing": "未来是否最终发布的实际结果", "topic": "future_outcome", "cause": "not_found"}]
    a = m.assess_evidence(q, frame, retrieval, mock_model([good]), tmp_path)
    assert all("未来是否最终发布" not in g.missing for g in a.gap_details)


def test_downstream_context_never_keeps_orphaned_finding(tmp_path, clear_framing, mock_model):
    m = module(); q, frame, retrieval, good = setup(tmp_path, clear_framing)
    a = m.assess_evidence(q, frame, retrieval, mock_model([good]), tmp_path)
    original = a.model_dump()
    context = m.make_evidence_context(retrieval.evidence, a, max_chars_per_source=64)
    provided = {(e["id"], p["paragraph_id"]) for e in context["evidence"] for p in e["passages"]}
    assert all((c.evidence_id, c.paragraph_id) in provided for f in context["findings"] for c in f.citations)
    small = m.make_evidence_context(retrieval.evidence, a, max_chars_per_source=4)
    assert small["findings"] == [] and small["limitations"]
    assert a.model_dump() == original
