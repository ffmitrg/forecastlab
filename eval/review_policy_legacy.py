"""Frozen temporal-review baseline from bb4d339a9fe94c016e751998943fc0f366b6ea0a.

The matcher, opinion filtering, informational issue and status logic reproduce
backend/app/graph.py at that commit. Evidence availability, evidence_audit and
probability_basis are deliberately outside this isolated policy comparison.
Do not import the current matcher: doing so would silently move the baseline.
"""
import re

from app.schemas import QuestionSpec, Review, ReviewIssue


BASELINE_COMMIT = "bb4d339a9fe94c016e751998943fc0f366b6ea0a"


def mistakes_future_outcome_for_missing_evidence(text: str, question: QuestionSpec,
                                                 *, assume_missing: bool = False) -> bool:
    """Unchanged matcher from the baseline commit."""
    if not assume_missing and not re.search(r"缺少|缺失|不足|尚未|未知|无法|不能|没有|未有|未发生|未提供|不具备", text):
        return False
    if re.search(r"未来(?:的)?(?:结果|行情|数据|信息)|预测期|结算(?:日|时|结果)|截至日之后|截止日之后|后续(?:的)?(?:行情|数据|结果)|最终(?:结果|行情)", text):
        return True
    for match in re.finditer(r"(?:(\d{4})\s*[年/-]\s*)?(\d{1,2})\s*[月/-]\s*(?:(\d{1,2})\s*日?)?", text):
        year = int(match.group(1)) if match.group(1) else question.as_of.year
        month = int(match.group(2))
        day = int(match.group(3)) if match.group(3) else None
        if not 1 <= month <= 12:
            continue
        if (year, month) > (question.as_of.year, question.as_of.month):
            return True
        if day is not None and (year, month, day) > (question.as_of.year, question.as_of.month, question.as_of.day):
            return True
    return False


def apply_legacy_policy(raw: Review, question: QuestionSpec, world_summary: str = "") -> tuple[Review, dict]:
    review = raw.model_copy(deep=True)
    future_gap_found = any(mistakes_future_outcome_for_missing_evidence(
        f"{issue.claim} {issue.explanation}", question) for issue in review.issues)
    future_gap_found |= any(mistakes_future_outcome_for_missing_evidence(x, question, assume_missing=True)
                            for x in review.missing_evidence)
    future_gap_found |= any(mistakes_future_outcome_for_missing_evidence(x, question)
                            for x in review.unsupported_claims)
    future_gap_found |= mistakes_future_outcome_for_missing_evidence(world_summary, question)
    decisions = []
    for field in ("issues", "missing_evidence", "unsupported_claims"):
        for index, value in enumerate(getattr(review, field)):
            text = f"{value.claim} {value.explanation}" if field == "issues" else value
            removed = mistakes_future_outcome_for_missing_evidence(
                text, question, assume_missing=field == "missing_evidence")
            decisions.append({"field": field, "index": index,
                              "action": "drop" if removed else "keep",
                              "reason": "Frozen legacy date/keyword matcher."})
    review.issues = [issue for issue in review.issues if not mistakes_future_outcome_for_missing_evidence(
        f"{issue.claim} {issue.explanation}", question)]
    review.missing_evidence = [x for x in review.missing_evidence
                               if not mistakes_future_outcome_for_missing_evidence(x, question, assume_missing=True)]
    review.unsupported_claims = [x for x in review.unsupported_claims
                                 if not mistakes_future_outcome_for_missing_evidence(x, question)]
    if future_gap_found:
        review.issues.append(ReviewIssue(
            severity="medium", claim="预测期结果未知应由概率表达",
            explanation="审查排除了要求未来行情的判断；概率仅使用截至日证据。"))
    if any(issue.severity == "high" for issue in review.issues) or review.unsupported_claims:
        review.status = "blocked"
    elif future_gap_found and review.status == "blocked":
        review.status = "qualified"
    return review, {"version": f"legacy-{BASELINE_COMMIT}", "decisions": decisions,
                    "raw_review": raw.model_dump(mode="json"),
                    "status_before": raw.status, "status_after": review.status}
