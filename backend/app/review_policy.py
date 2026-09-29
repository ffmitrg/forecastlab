"""Conservative, deterministic filtering of Agent 6's temporal review mistakes.

Only remove an opinion when *every* clause is a recognized demand for an
unobserved future outcome (or its direct consequence). Dates alone, unknown
wording, and mixed opinions are kept. This is not a semantic fact checker.
"""
from datetime import date
import re

from .schemas import QuestionSpec, Review


POLICY_VERSION = "agent6-temporal-v1"
_FIELDS = ("issues", "unsupported_claims", "missing_evidence")

# These references can describe information already available at the cutoff.
# A false keep costs an unnecessary objection; a false drop loses a real one.
_PROTECTED = re.compile(
    r"计划|日程|安排|预定|预计|预告|公告|合同|原文|引用|来源|历史|事前|事先|"
    r"已|当时|当前|此前|(?:截至|截止)日(?:前|之前|当时)|"
    r"错误|矛盾|冲突|反证|不一致|不支持|伪造|误|假设|模拟|"
    r"不应|不该|不必|无需|并非|不是|不能要求|不得要求"
)
_MISSING = r"(?:缺少|缺失|缺乏|尚缺|尚无|没有|未提供|未取得|无法取得|需要|要求)"
_UNKNOWN = r"(?:尚未发生|还未发生|尚未产生|尚未公布|仍未知|未知|尚不可知|未提供)"
_OUTCOME = (
    r"(?:实际)?(?:发布结果|发布记录|收盘数据|收盘价|收盘点位|行情数据|"
    r"比赛结果|比赛比分|最终排名|最终结果|结算结果|结果|行情|数据)|"
    r"是否(?:真的)?(?:按期)?发布"
)
_RELATIVE = re.compile(r"(?:未来|预测期(?:内)?|结算日|结算时|(?:截至|截止)日之后|后续)(?:的)?")
_DATE = re.compile(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](?:(\d{1,2})日?)?")
_CONSEQUENCE = re.compile(
    r"(?:因此|因而|所以)?(?:无法预测|不能预测|无法给出概率|无法判断|"
    r"无法(?:确认|核实)(?:结果|结局|结算结果|结算结局)|无法(?:据此)?确认是否发布|证据不足)"
)


def _future_target(text: str, question: QuestionSpec) -> bool:
    """Recognize a whole future-outcome noun phrase, not a date anywhere in text."""
    relative = _RELATIVE.match(text)
    if relative:
        target = text[relative.end():]
    else:
        match = _DATE.match(text)
        if not match:
            return False
        year, month, day = match.groups()
        # For a month with no day, only a later month is unambiguously future.
        try:
            mentioned = date(int(year or question.as_of.year), int(month), int(day or 1))
        except ValueError:
            return False
        rest = text[match.end():]
        after = rest.startswith("之后")
        if mentioned <= question.as_of.date() and not (
            after and day and mentioned == question.as_of.date()
        ):
            return False
        target = rest[2:] if after else rest
        target = target.removeprefix("的")
    return re.fullmatch(_OUTCOME, target) is not None


def _classify(parts: list[str], question: QuestionSpec, *, assume_missing: bool) -> tuple[str, str]:
    text = "；".join(parts)
    if _PROTECTED.search(text):
        return "keep", "涉及既有资料、事实判断或反对索要未来结果的意见，保留整条。"
    clauses = [re.sub(r"\s+", "", c) for c in re.split(r"[，,。；;！？!?\n]+", text) if c.strip()]
    found_demand = False
    for clause in clauses:
        missing = re.match(_MISSING, clause)
        if missing and _future_target(clause[missing.end():], question):
            found_demand = True
            continue
        unknown = re.search(_UNKNOWN + r"$", clause)
        if unknown and _future_target(clause[:unknown.start()], question):
            found_demand = True
            continue
        if assume_missing and _future_target(clause, question):
            found_demand = True
            continue
        if _CONSEQUENCE.fullmatch(clause):
            continue
        return "keep", "含未能确定为纯未来结果要求的表述，保守保留整条。"
    if found_demand:
        return "drop", "各分句仅索要截至日之后的实际结果或据此拒绝预测。"
    return "keep", "未识别到明确的未来实际结果要求。"


def apply_review_policy(review: Review, question: QuestionSpec) -> tuple[Review, dict]:
    """Return a new Review and a JSON-ready audit, without mutating model output.

    A blocked review can relax only when all opinions were removed AND at least
    one explicit blocker (high issue / unsupported claim) was among them.
    A bare blocked status or missing-evidence-only list has no known cause.
    """
    raw = review.model_dump(mode="json")
    result = review.model_copy(deep=True)
    decisions = []
    explicit_blocker_removed = False
    for field in _FIELDS:
        kept = []
        for index, item in enumerate(getattr(review, field)):
            parts = [item.claim, item.explanation] if field == "issues" else [item]
            action, reason = _classify(parts, question, assume_missing=field == "missing_evidence")
            decisions.append({"field": field, "index": index, "action": action, "reason": reason})
            if action == "keep":
                kept.append(item.model_copy(deep=True) if field == "issues" else item)
            elif field == "unsupported_claims" or (field == "issues" and item.severity == "high"):
                explicit_blocker_removed = True
        setattr(result, field, kept)

    all_removed = bool(decisions) and all(d["action"] == "drop" for d in decisions)
    status_reason = "保留原状态；未证明所有阻断原因均已排除。"
    if any(i.severity == "high" for i in result.issues) or result.unsupported_claims:
        result.status = "blocked"
        status_reason = "仍有严重意见或无依据主张，保持阻断。"
    elif review.status == "blocked" and all_removed and explicit_blocker_removed:
        result.status = "qualified"
        status_reason = "已移除全部意见，且明确的阻断意见均只索要未来结果。"

    return result, {
        "version": POLICY_VERSION,
        "raw_review": raw,
        "decisions": decisions,
        "removed_future_demands": any(d["action"] == "drop" for d in decisions),
        "status_before": review.status,
        "status_after": result.status,
        "status_reason": status_reason,
    }
