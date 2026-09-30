"""Agent 2: source-backed findings, with one shared repair budget."""
from __future__ import annotations
import re
from ..schemas import (AssessmentCandidate, EvidenceAssessment, EvidenceFinding, RejectedFinding,
                       ConflictDetail, GapDetail, EvidencePassage)
from ..provenance import load_snapshot, save_snapshot, split_passages, select_passages, resolve_citation
from ..llm import BudgetExceeded

PROMPT = """只依据提供的原文段落整理证据，不能用模型记忆补来源或结论。
每个finding必须有来源E编号、快照hash、段落编号和逐字原文quote，并指向活动前提P编号。
关系属于这个发现与前提，不属于整个网站；同一来源可以支持一项前提、挑战另一项。
不要因为检索任务叫challenge就把搜到的材料标成反证。没有可靠反证时不编造对立观点。
保留摘要/正文身份、日期未知、同源转载、未来计划等限制；计划不是实际发生的事实。
对前提为真与在该情景条件下讨论做区别，不将待核查前提当事实。
冲突用零起始finding_indexes关联双方，比较时间、指标和地区；口径不同不一定真矛盾。
缺口仅列信息截点前可能取得却未提供的资料；未来实际结果尚未发生不是证据缺口。
summary概括资料覆盖情况，不额外提出缺少引文的事实。不要输出概率。
不能引用未给出的段落，也不能把不相邻文字拼为一句引文；不输出字符偏移。"""


class EvidenceStageError(RuntimeError):
    def __init__(self, result, assessment):
        super().__init__(assessment.summary)
        self.result = result
        self.assessment = assessment


def active_framing(framing):
    if framing is None:
        return None
    data = framing.model_dump(mode="json", exclude={"raw_question", "inputs", "analysis_record", "clarifications"})
    active = {p.id for p in framing.premises if p.user_review != "rejected"}
    data["premises"] = [p for p in data["premises"] if p["id"] in active]
    data["retrieval_plan"] = [t for t in data["retrieval_plan"] if not t["target_premise_ids"] or any(p in active for p in t["target_premise_ids"])]
    for t in data["retrieval_plan"]:
        t["target_premise_ids"] = [p for p in t["target_premise_ids"] if p in active]
    return data


def validate_findings(candidate, framing, evidence, passages):
    sources = {e.id: e for e in evidence}
    active = {p.id for p in framing.premises if p.user_review != "rejected"} if framing else set()
    valid, rejected = [], []
    for index, finding in enumerate(candidate.findings):
        try:
            if set(finding.target_premise_ids) - active:
                raise ValueError("发现引用了不存在或被用户否认的前提")
            citations = []
            for citation in finding.citations:
                if citation.evidence_id not in sources:
                    raise ValueError("发现引用了不存在的证据编号")
                citations.append(resolve_citation(citation, sources[citation.evidence_id], passages.get(citation.evidence_id, [])))
            valid.append(EvidenceFinding(id=f"F{index+1:03}", target_premise_ids=finding.target_premise_ids,
                claim=finding.claim, relation=finding.relation, citations=citations, limitation=finding.limitation))
        except ValueError as exc:
            rejected.append(RejectedFinding(candidate={"candidate_index": index, **finding.model_dump(mode="json")}, reason=str(exc)))
    return valid, rejected


def _details(candidate, findings, framing, logs):
    ids = {f.id for f in findings}
    active = {p.id for p in framing.premises if p.user_review != "rejected"} if framing else set()
    queries = {log.task_id for log in logs}
    conflicts, gaps, rejected = [], [], []
    for c in candidate.conflicts:
        references = [f"F{i+1:03}" for i in c.finding_indexes]
        if len(set(references)) < 2 or set(references) - ids:
            rejected.append(RejectedFinding(candidate={"conflict": c.model_dump()}, reason="冲突引用了无效或不足两项的发现"))
        else:
            conflicts.append(ConflictDetail(issue=c.issue, finding_ids=references, scope_comparison=c.scope_comparison,
                                             status=c.status, explanation=c.explanation))
    for gap in candidate.gaps:
        if gap.topic == "future_outcome" or re.search(r"未来.*(?:实际结果|最终结果)|结算日.*实际结果", gap.missing):
            continue
        if set(gap.target_premise_ids) - active or set(gap.attempted_query_ids) - queries:
            rejected.append(RejectedFinding(candidate={"gap": gap.model_dump()}, reason="缺口引用未执行查询或无效前提"))
        else:
            gaps.append(gap)
    return conflicts, gaps, rejected


def _source_gaps(retrieval):
    gaps = []
    for log in retrieval.retrieval_log:
        if log.status == "failed":
            gaps.append(GapDetail(missing=f"检索任务 {log.task_id} 失败：{log.error}", attempted_query_ids=[log.task_id], cause="retrieval_failed"))
        elif log.status == "empty":
            gaps.append(GapDetail(missing=f"检索任务 {log.task_id} 未返回资料", attempted_query_ids=[log.task_id], cause="not_found"))
    for e in retrieval.evidence:
        if e.content_kind == "snippet":
            gaps.append(GapDetail(missing=f"{e.id} 只有搜索摘要，未取得正文", cause="snippet_only"))
        if e.published_at is None:
            gaps.append(GapDetail(missing=f"{e.id} 发布时间未知", cause="date_unknown"))
        if e.availability == "historical_exercise":
            gaps.append(GapDetail(missing=f"{e.id} 为事后整理的历史练习，不是严格盲测快照", cause="historical_unverified"))
    for x in retrieval.exclusions:
        gaps.append(GapDetail(missing=f"来源排除：{x.get('source', '')}；{x.get('reason', '')}",
                              cause="after_cutoff" if "晚于" in x.get("reason", "") else "validation_failed"))
    return gaps


def _compatibility(assessment):
    assessment.conflicts = [f"{'已解释' if c.status == 'resolved' else '未解决'}：{c.issue}；{c.scope_comparison}；{c.explanation}" for c in assessment.conflict_details]
    assessment.gaps = [g.missing for g in assessment.gap_details]
    return assessment


def assess_evidence(question, framing, retrieval, model, data_dir) -> EvidenceAssessment:
    if retrieval.status == "failed":
        a = _compatibility(EvidenceAssessment(summary="取证全部失败，请检查检索配置或创建新运行。",
            retrieval_log=retrieval.retrieval_log, exclusions=retrieval.exclusions, gap_details=_source_gaps(retrieval)))
        raise EvidenceStageError(retrieval, a)
    good_sources = []
    terms = [question.question]
    if framing:
        terms += [p.content for p in framing.premises if p.user_review != "rejected"] + framing.alternative_directions
    for original in retrieval.evidence:
        e = original.model_copy(deep=True)
        try:
            if e.snapshot_hash:
                snapshot = load_snapshot(e, data_dir)
            else:
                snapshot = save_snapshot(e.excerpt, {"provider": "legacy-record", "title": e.title,
                    "legacy_retrieved_at": e.retrieved_at.isoformat()}, data_dir)
                e.snapshot_path, e.snapshot_hash = snapshot.snapshot_path, snapshot.snapshot_hash
                e.retrieved_at = snapshot.stored_at
                e.availability = "synthetic" if e.date_status == "synthetic" else "unverified"
            e.passages = select_passages(split_passages(snapshot), terms)
            good_sources.append(e)
        except (ValueError, OSError) as exc:
            retrieval.exclusions.append({"source": e.id, "reason": f"原文快照不可验证（{type(exc).__name__}）"})
    retrieval.evidence = good_sources
    assessment = EvidenceAssessment(summary="没有可核对来源。", evidence_ids=[e.id for e in good_sources],
        retrieval_log=retrieval.retrieval_log, exclusions=retrieval.exclusions, gap_details=_source_gaps(retrieval))
    if not good_sources:
        assessment.gap_details.append(GapDetail(missing="证据包中没有可读取的有效来源", cause="not_found"))
        return _compatibility(assessment)
    passages = {e.id: e.passages for e in good_sources}
    payload = {"question": question.model_dump(mode="json"), "question_framing": active_framing(framing),
        "evidence": [e.model_dump(mode="json", exclude={"snapshot_path", "content_hash", "excerpt"}) for e in good_sources],
        "retrieval_log": [x.model_dump() for x in retrieval.retrieval_log]}
    candidate = None
    for attempt in range(2):
        try:
            candidate = model.complete("evidence12", payload, AssessmentCandidate, PROMPT, attempt_limit=1)
            findings, rejected = validate_findings(candidate, framing, good_sources, passages)
            conflicts, gaps, more_rejected = _details(candidate, findings, framing, retrieval.retrieval_log)
            rejected += more_rejected
            assessment.summary = candidate.summary
            assessment.findings, assessment.conflict_details = findings, conflicts
            assessment.gap_details = _source_gaps(retrieval) + gaps
            assessment.rejected_findings = rejected
            if not rejected:
                break
            payload["validation_feedback"] = [r.reason for r in rejected]
        except BudgetExceeded as exc:
            if candidate is None:
                assessment.summary = "证据分析额度不足，已保存来源与检索日志。"
                raise EvidenceStageError(retrieval, _compatibility(assessment)) from exc
            assessment.gap_details.append(GapDetail(missing="额度不足，未再修复无效发现", cause="validation_failed"))
            break
        except (ValueError, RuntimeError) as exc:
            assessment.rejected_findings = [RejectedFinding(candidate={}, reason=f"结构输出无效：{str(exc)[:500]}")]
            payload["validation_feedback"] = assessment.rejected_findings[0].reason
    if assessment.rejected_findings:
        assessment.gap_details.append(GapDetail(missing=f"{len(assessment.rejected_findings)}项候选未通过原文/引用校验，未进入有效发现", cause="validation_failed"))
    return _compatibility(assessment)


def make_evidence_context(evidence, assessment, *, max_chars_per_source: int) -> dict:
    """Greedily keep whole findings; never retain a claim without its quoted text."""
    if max_chars_per_source < 0:
        raise ValueError("段落预算不能为负")
    lookup = {(e.id, p.paragraph_id): p for e in evidence for p in e.passages}
    ranges, kept, omitted = {}, [], []
    for finding in assessment.findings:
        proposed = dict(ranges)
        valid = True
        for c in finding.citations:
            key = (c.evidence_id, c.paragraph_id)
            p = lookup.get(key)
            if p is None or p.snapshot_hash != c.snapshot_hash or p.text[c.start-p.start:c.end-p.start] != c.quote:
                valid = False; break
            lo, hi = proposed.get(key, (c.start, c.end))
            proposed[key] = (min(lo, c.start), max(hi, c.end))
        totals = {}
        for (eid, _), (lo, hi) in proposed.items():
            totals[eid] = totals.get(eid, 0) + hi-lo
        if valid and all(n <= max_chars_per_source for n in totals.values()):
            kept.append(finding); ranges = proposed
        else:
            omitted.append(finding.id)
    output = []
    for e in evidence:
        selected = []
        source_ranges = [(key, value) for key, value in ranges.items() if key[0] == e.id]
        remaining = max_chars_per_source - sum(hi-lo for _, (lo, hi) in source_ranges)
        for key, (lo, hi) in sorted(source_ranges, key=lambda kv: kv[1][0]):
            p = lookup[key]
            before = min(80, lo-p.start, remaining); remaining -= before
            after = min(80, p.end-hi, remaining); remaining -= after
            start, end = lo-before, hi+after
            selected.append(EvidencePassage(paragraph_id=p.paragraph_id, start=start, end=end,
                text=p.text[start-p.start:end-p.start], snapshot_hash=p.snapshot_hash))
        used_ids = {p.paragraph_id for p in selected}
        for p in e.passages:
            if p.paragraph_id not in used_ids and len(p.text) <= remaining:
                selected.append(p); remaining -= len(p.text)
        data = e.model_dump(mode="json", exclude={"snapshot_path", "content_hash", "passages", "excerpt"})
        selected.sort(key=lambda p: p.start)
        data["passages"] = [p.model_dump() for p in selected]
        data["excerpt"] = "\n\n".join(p.text for p in selected)
        output.append(data)
    limitations = [f"上下文预算不足或段落不匹配，已省略发现 {fid}；不能据此认定该发现没有证据" for fid in omitted]
    visible = assessment.model_copy(deep=True)
    visible.findings, visible.rejected_findings = kept, []
    ids = {f.id for f in kept}
    visible.conflict_details = [c for c in visible.conflict_details if set(c.finding_ids) <= ids]
    if omitted:
        visible.summary = f"本次模型上下文保留{len(kept)}项可追查发现，省略{len(omitted)}项。"
    _compatibility(visible)
    visible.gaps += limitations
    return {"evidence": output, "findings": kept, "assessment": visible, "limitations": limitations}
