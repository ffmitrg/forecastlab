"""Boundary tests beyond the frozen synthetic policy evaluation."""
import pytest

from app.demo import DEMO_QUESTION
from app.review_policy import apply_review_policy
from app.schemas import Review, ReviewIssue


@pytest.mark.parametrize("text", [
    "缺少2026年9月25日的实际发布结果",  # Before the cutoff.
    "缺少2026年9月26日的实际发布结果",  # Same day: time is ambiguous.
    "缺少2027年2月30日的实际发布结果",  # Invalid calendar date.
    "缺少2026年9月的实际收盘数据",      # Current month is ambiguous.
    "不应要求未来的实际发布结果",      # Rejects, rather than makes, the demand.
    "缺少未来的实际发布结果；模型概率没有校准",  # Mixed issue without protected words.
    "Missing the published release schedule for November.",  # Unknown wording.
    "缺少未来的实际发布结果是否合理",  # A question, not a pure demand.
])
def test_ambiguous_historical_or_mixed_text_is_not_removed(text):
    raw = Review(status="blocked", missing_evidence=[text])
    output, trace = apply_review_policy(raw, DEMO_QUESTION)
    assert output.missing_evidence == [text]
    assert trace["decisions"][0]["action"] == "keep"
    assert output.status == "blocked"


@pytest.mark.parametrize("text", [
    "缺少 2026 年 11 月 15 日的实际发布结果。",
    "缺少2026-11-15的实际发布结果",
    "缺少2026/11/15的实际发布结果",
    "缺少2027年2月的实际收盘数据",
])
def test_future_result_with_valid_date_is_removed_without_unexplained_unblocking(text):
    output, trace = apply_review_policy(Review(status="blocked", missing_evidence=[text]), DEMO_QUESTION)
    assert output.missing_evidence == []
    assert trace["removed_future_demands"]
    # Missing-evidence items have no severity: the blocked cause is not known.
    assert output.status == "blocked"


def test_review_and_audit_are_independent_of_input_mutation():
    raw = Review(status="blocked", issues=[ReviewIssue(
        severity="high", claim="缺少未来的实际发布结果", explanation="未来结果尚未发生。")])
    before = raw.model_dump(mode="json")
    output, trace = apply_review_policy(raw, DEMO_QUESTION)
    assert raw.model_dump(mode="json") == before
    assert trace["raw_review"] == before
    assert output.status == "qualified"
    raw.issues[0].claim = "changed later"
    assert trace["raw_review"] == before


def test_low_severity_missing_result_cannot_explain_a_blocked_status():
    raw = Review(status="blocked", issues=[ReviewIssue(
        severity="low", claim="缺少未来实际发布结果", explanation="未来结果尚未发生。")])
    output, _ = apply_review_policy(raw, DEMO_QUESTION)
    assert output.issues == []
    assert output.status == "blocked"
