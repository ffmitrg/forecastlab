"""Agent 1: produce a candidate, then apply deterministic ownership checks."""
from __future__ import annotations
from uuid import uuid4
from pydantic import ValidationError
from ..schemas import (AnalyzeQuestionRequest, FramingCandidate, QuestionFraming, QuestionInput,
    QuestionDraft, QuestionSpec, QuestionClarification, QuestionPremise, RetrievalTask)

PROMPT = """澄清研究对象、时间、地区和判定标准，保留用户原意，不接受其预设结论。
你收到的inputs是用户原话和补充回答。premises只识别这些文本中确有依据的前提，
必须给source_input_id和逐字original_span；中性问题可没有前提。
model_inferred表示从措辞推断，绝不冒充用户明确观点。被用户否认的原有前提不得重新当作用户认可的前提。
把研究问题和用户的解释分开，给少量值得核查的alternative_directions，不机械凑正反数量。
不要自己创造公司、地区、日期或成功阈值。已有as_of、mode、结算日期和规则原样保留，
需要调整就列clarifications。缺少关键对象/口径则提出具体blocking问题；已回答的澄清不要重复。
未来结果尚未发生是研究目标，不是让用户补充的事实。user_assumptions只能照抄用户明确填写的条件。
最多3个检索任务，每个标注purpose和零起始premise_indexes；方向可为initial/challenge/alternative/background。
只输出FramingCandidate，不生成草稿编号、状态、确认记录或外部证据。"""


def question_inputs(request: AnalyzeQuestionRequest, previous: QuestionFraming | None) -> list[QuestionInput]:
    inputs = [i.model_copy(deep=True) for i in previous.inputs] if previous else []
    def add(kind, text):
        inputs.append(QuestionInput(input_id=f"I{len(inputs)+1:03}", kind=kind, text=text))
    if not previous:
        if request.answers:
            raise ValueError("首次分析不能回答不存在的澄清问题")
        add("original", request.question.question)
    elif request.question.question != previous.proposed_spec.question:
        add("revision", request.question.question)
    known = {c.id for c in previous.clarifications} if previous else set()
    for answer in request.answers:
        if answer.clarification_id not in known:
            raise ValueError("澄清回答引用了不存在的问题编号")
        add("answer", answer.answer)
    previous_conditions = set(previous.proposed_spec.user_assumptions) if previous else set()
    for condition in request.question.user_assumptions:
        if condition not in previous_conditions:
            add("answer", condition)
    return inputs


def analyze_question(request: AnalyzeQuestionRequest, previous: QuestionFraming | None, model,
                     *, validation_feedback: str = "") -> FramingCandidate:
    inputs = question_inputs(request, previous)
    payload = {"question": request.question.model_dump(mode="json"),
               "inputs": [i.model_dump() for i in inputs],
               "previous_framing": previous.model_dump(mode="json") if previous else None,
               "answers": [a.model_dump() for a in request.answers],
               "validation_feedback": validation_feedback}
    # One transport attempt here; the service owns the single shared repair attempt.
    return model.complete("question12", payload, FramingCandidate, PROMPT, attempt_limit=1)


def finalize_framing(candidate: FramingCandidate, request: AnalyzeQuestionRequest,
                     previous: QuestionFraming | None) -> QuestionFraming:
    inputs = question_inputs(request, previous)
    texts = {i.input_id: i.text for i in inputs}
    clarifications = [QuestionClarification(id=f"C{i+1:03}", **c.model_dump())
                      for i, c in enumerate(candidate.clarifications)
                      if c.field not in {"future_outcome", "actual_future_result"}]
    proposed = candidate.proposed_spec.model_copy(deep=True)
    # Explicit fields remain user-owned. Suggestions cannot silently overwrite them.
    for name in ("as_of", "mode", "resolve_by", "resolution_rule", "resolution_source", "user_assumptions"):
        supplied = getattr(request.question, name)
        fixed = name in {"as_of", "mode", "user_assumptions"} or supplied not in (None, "", [])
        if fixed and supplied != getattr(proposed, name):
            setattr(proposed, name, supplied)
            if not any(c.field == name for c in clarifications):
                clarifications.append(QuestionClarification(id=f"C{len(clarifications)+1:03}", field=name,
                    question=f"模型建议与您填写的 {name} 不同；目前保留您的值。需要更改请编辑后重新分析。", blocking=True))
    if proposed.mode == "binary":
        for name, prompt in (("resolve_by", "请明确在哪个日期和时区判断结果。"),
                             ("resolution_rule", "什么可核对的情况算是，什么情况算否？")):
            if not getattr(proposed, name) and not any(c.field == name for c in clarifications):
                clarifications.append(QuestionClarification(id=f"C{len(clarifications)+1:03}", field=name, question=prompt))
    valid_spec = True
    try:
        QuestionSpec.model_validate(proposed.model_dump())
    except ValidationError:
        valid_spec = False
        if not clarifications:
            clarifications.append(QuestionClarification(id="C001", field="resolution", question="请核对结算时间晚于信息截点，且规则可以核查。"))
    old = previous.premises if previous else []
    next_number = max([previous.next_premise_number if previous else 1] + [int(p.id[1:])+1 for p in old])
    premises = []
    for c in candidate.premises:
        if c.source_input_id not in texts or c.original_span not in texts[c.source_input_id]:
            raise ValueError("候选前提的原话未出现在指定用户输入中")
        exact = next((p for p in old if (p.content, p.original_span, p.source_input_id, p.origin) ==
                      (c.content, c.original_span, c.source_input_id, c.origin)), None)
        if exact:
            premise = exact.model_copy(deep=True)
        else:
            replaced = next((p for p in old if p.id == c.replaces_id), None) if c.replaces_id else next(
                (p for p in old if p.source_input_id == c.source_input_id and p.original_span == c.original_span), None)
            if c.replaces_id and replaced is None:
                raise ValueError("前提替代关系引用不存在的编号")
            data = c.model_dump(); data["replaces_id"] = replaced.id if replaced else None
            premise = QuestionPremise(id=f"P{next_number:03}", **data)
            next_number += 1
        premises.append(premise)
    tasks = []
    for i, task in enumerate(candidate.retrieval_plan):
        if any(index < 0 or index >= len(premises) for index in task.premise_indexes):
            raise ValueError("检索任务引用了不存在的候选前提")
        targets = list(dict.fromkeys(premises[index].id for index in task.premise_indexes))
        retained_targets = [p for p in targets if next(x for x in premises if x.id == p).user_review != "rejected"]
        if targets and not retained_targets:
            continue
        tasks.append(RetrievalTask(id=f"R{i+1:03}", query=task.query, purpose=task.purpose, target_premise_ids=retained_targets))
    return QuestionFraming(draft_id=request.draft_id or f"draft_{uuid4().hex}", revision=previous.revision+1 if previous else 1,
        raw_question=previous.raw_question if previous else request.question.question, proposed_spec=proposed,
        inputs=inputs, premises=premises, clarifications=clarifications, alternative_directions=candidate.alternative_directions,
        retrieval_plan=tasks, next_premise_number=next_number, demo_case_id=request.demo_case_id,
        status="ready_for_confirmation" if valid_spec and not any(c.blocking and c.status == "open" for c in clarifications) else "needs_clarification")
