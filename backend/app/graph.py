"""Explicit SAO workflow. Each node owns a single stage output."""
from concurrent.futures import ThreadPoolExecutor
from math import ceil
import re
import time
from typing import TypedDict
from uuid import uuid4
from langgraph.graph import StateGraph, START, END
from .schemas import (QuestionSpec, QuestionAnalysis, Evidence, EvidenceAssessment, EvidenceOnlyAudit,
                      WorldState, ActorAction, SimulationStep, Review, ReviewIssue, Forecast,
                      EvidenceOnlyForecast, RunRecord, utcnow)
from .sources import online_search
from .llm import BudgetExceeded, ModelClient
from .demo import demo_output
from .review_policy import apply_review_policy
from . import config


class FlowState(TypedDict, total=False):
    question: dict
    question_analysis: dict
    evidence: list[dict]
    evidence_assessment: dict
    world: dict
    actions: list[dict]
    simulation: list[dict]
    review: dict
    review_policy_audit: dict
    forecast: dict


def evidence_for_model(items: list[Evidence], limit: int = 2400) -> list[dict]:
    """Keep model context bounded while retaining complete evidence in snapshots."""
    return [{**e.model_dump(mode="json", exclude={"snapshot_path", "content_hash"}),
             "excerpt": e.excerpt[:limit]} for e in items]


def available_at_cutoff(evidence: Evidence, as_of) -> bool:
    """Allow dated exercise material without pretending it was fetched at the cutoff."""
    if evidence.retrieved_at <= as_of:
        return True
    return (evidence.source_type == "exercise" and evidence.published_at is not None
            and evidence.published_at <= as_of
            and (evidence.updated_at is None or evidence.updated_at <= as_of)
            and (evidence.event_at is None or evidence.event_at <= as_of))


def mistakes_future_outcome_for_missing_evidence(text: str, question: QuestionSpec,
                                                 *, assume_missing: bool = False) -> bool:
    """Catch the common error of demanding observations from the forecast period."""
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


def market_price_context(question: QuestionSpec, evidence: list[Evidence]) -> dict | None:
    """Describe dated price coverage without treating recent momentum as a base rate."""
    if question.mode != "binary" or question.resolve_by is None or not re.search(
        r"指数|股价|股市|股票|A股|ETF|收盘点位|收盘价|期货|汇率", question.question, re.I
    ):
        return None
    horizon_days = max(1, ceil((question.resolve_by - question.as_of).total_seconds() / 86400))
    price_items = [item for item in evidence if re.search(
        r"收盘|收于|涨幅|跌幅|日涨|成交额|振幅|点位|盘中最高|盘中最低", item.excerpt
    ) and item.event_at is not None and item.event_at <= question.as_of]
    return {
        "forecast_horizon_days": horizon_days,
        "dated_price_observation_days": len({item.event_at.date() for item in price_items}),
        "price_source_groups": len({item.source_group for item in price_items}),
        "note": "这些是证据包中明确标注事件日的价格资料数量，不代表完整历史价格序列或经验基准率。",
    }


def trace_for_model(state: FlowState) -> dict:
    """Keep the causal trace and references without repeating verbose agent prose."""
    action_keys = {"id", "actor_id", "round", "action", "evidence_ids", "assumption_ids", "conditions"}
    step_keys = {"id", "round", "summary", "state_changes", "conflicts", "unresolved", "evidence_ids", "assumption_ids"}
    return {
        "actions": [{key: value for key, value in action.items() if key in action_keys} for action in state["actions"]],
        "simulation": [{key: value for key, value in step.items() if key in step_keys} for step in state["simulation"]],
    }


def check_ids(ids: list[str], valid: set[str], label: str):
    missing = set(ids) - valid
    if missing:
        raise ValueError(f"{label}引用不存在：{', '.join(sorted(missing))}")


def canonical_ids(values: list[str], valid: set[str]) -> list[str]:
    """Accept an ID wrapped in explanatory prose, never create a new reference."""
    normalized = []
    for value in values:
        matches = ([value] if value in valid else sorted(
            (item for item in valid if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(item)}(?![A-Za-z0-9_-])", value)),
            key=value.find))
        normalized.extend(matches or [value])
    return list(dict.fromkeys(normalized))


def canonicalize_forecast_ids(forecast: Forecast, evidence: list[Evidence], world: WorldState,
                              simulation: list[SimulationStep]) -> None:
    evidence_ids = {item.id for item in evidence}
    assumption_ids = {item.id for item in world.assumptions}
    simulation_ids = {item.id for item in simulation}
    for claim in forecast.supporting + forecast.opposing:
        claim.evidence_ids = canonical_ids(claim.evidence_ids, evidence_ids)
        claim.assumption_ids = canonical_ids(claim.assumption_ids, assumption_ids)
        claim.simulation_ids = canonical_ids(claim.simulation_ids, simulation_ids)
    forecast.key_assumptions = canonical_ids(forecast.key_assumptions, assumption_ids)


def validate_forecast(forecast: Forecast, question: QuestionSpec, evidence: list[Evidence], world: WorldState,
                      simulation: list[SimulationStep], review: Review, *, require_probability: bool = False):
    evidence_ids = {e.id for e in evidence}
    assumption_ids = {a.id for a in world.assumptions}
    simulation_ids = {s.id for s in simulation}
    evidence_only = review.probability_basis == "evidence_only"
    for claim in forecast.supporting + forecast.opposing:
        check_ids(claim.evidence_ids, evidence_ids, "报告证据")
        check_ids(claim.assumption_ids, assumption_ids, "报告假设")
        check_ids(claim.simulation_ids, simulation_ids, "报告模拟")
        if not (claim.evidence_ids or claim.assumption_ids or claim.simulation_ids):
            raise ValueError("报告主张没有可展开的依据")
        if evidence_only and (claim.assumption_ids or claim.simulation_ids):
            raise ValueError("仅依据证据的概率不能引用假设或模拟")
    check_ids(forecast.key_assumptions, assumption_ids, "关键假设")
    if evidence_only and forecast.key_assumptions:
        raise ValueError("仅依据证据的概率不能依赖建模假设")
    if question.mode == "scenario" or (review.status == "blocked" and not evidence_only) or not evidence:
        forecast.probabilities = None
        forecast.status = "scenario_only" if question.mode == "scenario" else "insufficient_evidence"
    if require_probability and forecast.probabilities is None:
        raise ValueError("事前证据复审允许主观概率，报告仍未给出概率")
    if forecast.probabilities is not None:
        if set(forecast.probabilities) != set(question.outcomes):
            raise ValueError("概率结果选项与问题不一致")
        if any(p < 0 or p > 1 for p in forecast.probabilities.values()) or abs(sum(forecast.probabilities.values()) - 1) > .001:
            raise ValueError("概率须在 0–1 且合计为 1")
    elif forecast.status == "completed":
        forecast.status = "insufficient_evidence"
    if evidence_only and forecast.probabilities is not None:
        forecast.status = "completed"
        forecast.probability_basis = "evidence_only"


def repair_forecast(forecast: Forecast, question: QuestionSpec, evidence: list[Evidence], world: WorldState,
                    simulation: list[SimulationStep], review: Review, reason: str) -> Forecast:
    """Conservatively retain only traceable claims after model correction fails."""
    evidence_ids = {item.id for item in evidence}
    assumption_ids = {item.id for item in world.assumptions}
    simulation_ids = {item.id for item in simulation}
    removed = 0
    for name in ("supporting", "opposing"):
        valid_claims = []
        for claim in getattr(forecast, name):
            claim.evidence_ids = [item for item in claim.evidence_ids if item in evidence_ids]
            claim.assumption_ids = [item for item in claim.assumption_ids if item in assumption_ids]
            claim.simulation_ids = [item for item in claim.simulation_ids if item in simulation_ids]
            if claim.evidence_ids or claim.assumption_ids or claim.simulation_ids:
                valid_claims.append(claim)
            else:
                removed += 1
        setattr(forecast, name, valid_claims)
    forecast.key_assumptions = [item for item in forecast.key_assumptions if item in assumption_ids]
    forecast.probabilities = None
    forecast.status = ("scenario_only" if question.mode == "scenario" else
                       "insufficient_evidence" if not evidence or (review.status == "blocked" and review.probability_basis != "evidence_only")
                       else "partial")
    forecast.conclusion = "报告生成未通过结构校验，已保存可追溯的依据，但本次未形成概率；请查看下方错误。"
    forecast.limitations.append(f"自动报告的完整性校验未通过（{reason[:160]}），概率已省略。")
    if removed:
        forecast.limitations.append(f"已移除 {removed} 条无法追溯的主张。")
    validate_forecast(forecast, question, evidence, world, simulation, review)
    return forecast


STAGE_NODES = (
    ("question", "define_question"), ("evidence", "retrieve"), ("world", "model_world"),
    ("simulation", "simulate"), ("review", "audit"), ("forecast", "synthesize"),
)


def build_graph(record: RunRecord, imported: list[Evidence], model: ModelClient | None, data_dir, *, start_at: str = "define_question"):
    def ask(role, payload, schema, instructions, *, actor_id=None, round_number=1):
        if record.demo:
            return schema.model_validate(demo_output(role, actor_id, round_number))
        return model.complete(role, payload, schema, instructions)

    def question_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        if not record.demo and record.evidence_mode in {"import", "reuse"}:
            # Search terms are only consumed by online retrieval; the user has
            # already supplied both the resolved question and the evidence here.
            analysis = QuestionAnalysis(normalized_question=question.question,
                                        search_queries=[question.question[:400]])
            return {"question_analysis": analysis.model_dump(mode="json")}
        analysis = ask("question", {"question": question.model_dump(mode="json")}, QuestionAnalysis,
                       "用户已确认预测目标和结算规则。用一句话规范化问题，给最多 3 个适合寻找原始资料的检索词；不要自行更改日期或结算条件。")
        analysis.search_queries = [q[:400] for q in analysis.search_queries[:3]]
        return {"question_analysis": analysis.model_dump(mode="json")}

    def evidence_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        if record.evidence_mode == "online":
            queries = QuestionAnalysis.model_validate(state["question_analysis"]).search_queries
            items = online_search(question, data_dir, queries)
        else:
            items = imported
        assessment = ask("evidence", {"question": state["question"], "evidence": evidence_for_model(items)}, EvidenceAssessment,
                         "归纳资料冲突和缺口，只引用实际存在的证据编号。搜索摘要不是全文证据；不可编造新来源。"
                         "缺口只列信息截至时间当时可能取得却未提供的资料；未来结算结果尚未发生是预测对象，不是证据缺口。")
        check_ids(assessment.evidence_ids, {e.id for e in items}, "证据评估")
        assessment.gaps = [gap for gap in assessment.gaps
                           if not mistakes_future_outcome_for_missing_evidence(gap, question, assume_missing=True)]
        if mistakes_future_outcome_for_missing_evidence(assessment.summary, question):
            assessment.summary = (f"已整理 {len(items)} 条截至信息日的资料；预测期结果尚未发生，"
                                  "应通过有保留的概率表达不确定性。")
        return {"evidence": [e.model_dump(mode="json") for e in items], "evidence_assessment": assessment.model_dump(mode="json")}

    def world_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        evidence = [Evidence.model_validate(x) for x in state["evidence"]]
        world = ask("world", {"question": question.model_dump(mode="json"), "evidence": evidence_for_model(evidence, 1600), "evidence_assessment": state["evidence_assessment"]}, WorldState,
                    "只用证据编号引用事实；不确定的动机必须写为 assumption。若无战略主体，可留空 actors 并说明原因。主体最多 3 个。"
                    "信息截至时间之后的事件均未发生，计划发布日期不能写成实际发布日期。"
                    "市场价格问题可以没有战略主体；预测期行情未知是需要预测的目标，不能据此认定无法预测。")
        world.actors = world.actors[:3]
        if len({a.id for a in world.actors}) != len(world.actors):
            raise ValueError("主体 ID 重复")
        if len({a.id for a in world.assumptions}) != len(world.assumptions):
            raise ValueError("假设 ID 重复")
        for content in question.user_assumptions:
            next_number = 1
            existing = {a.id for a in world.assumptions}
            while f"H{next_number:03}" in existing:
                next_number += 1
            from .schemas import Assumption
            world.assumptions.append(Assumption(id=f"H{next_number:03}", created_by="user", content=content, rationale="用户在创建运行时提供"))
        check_ids(world.evidence_refs, {e.id for e in evidence}, "世界状态")
        for actor in world.actors:
            check_ids(actor.visible_evidence_ids, {e.id for e in evidence}, "主体画像")
        valid_parents = {e.id for e in evidence} | {a.id for a in world.assumptions}
        for assumption in world.assumptions:
            check_ids(assumption.parent_ids, valid_parents, "假设")
        return {"world": world.model_dump(mode="json")}

    def simulation_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        world = WorldState.model_validate(state["world"])
        evidence = [Evidence.model_validate(x) for x in state["evidence"]]
        if not world.actors:
            return {"actions": [], "simulation": []}
        actions, steps = [], []
        current = world.model_dump(mode="json")
        for round_number in (1, 2):
            parent = round_number - 1
            def actor_call(actor):
                visible = evidence_for_model([e for e in evidence if e.id in actor.visible_evidence_ids], 1000)
                action = ask("actor", {"question": state["question"], "actor": actor.model_dump(), "state": current, "visible_evidence": visible, "round": round_number}, ActorAction,
                             "只代表这个主体做一个可能行动。信息截至时间之后的行动只能是假设情景，不可说成真实事实；计划日期不是实际发布日期。只引用给你的证据编号。", actor_id=actor.id, round_number=round_number)
                action.id = f"M{round_number}-{actor.id}"
                action.created_by = actor.id
                action.actor_id = actor.id
                action.round = round_number
                action.parent_state = parent
                action.parent_ids = [f"S{parent}"]
                action.kind = "simulation"
                check_ids(action.evidence_ids, {e["id"] for e in visible}, "主体行动")
                check_ids(action.assumption_ids, {a.id for a in world.assumptions}, "主体行动假设")
                return action
            with ThreadPoolExecutor(max_workers=min(3, len(world.actors))) as pool:
                round_actions = list(pool.map(actor_call, world.actors))
            actions.extend(round_actions)
            allowed_variables = set(current.get("variables", {}))
            step_payload = {"question": state["question"], "state": current, "actions": [a.model_dump(mode="json") for a in round_actions],
                            "round": round_number, "allowed_variable_keys": sorted(allowed_variables)}
            step = ask("environment", step_payload, SimulationStep,
                       f"联合处理全部行动；保留冲突与条件。当前信息截至时间为 {question.as_of.isoformat()}。"
                       "此后状态只能用‘若...则...’的条件式描述，绝不能把计划或模拟结果写成已发生的历史事实。"
                       "state_changes 的键只能从 allowed_variable_keys 中选择，不得新增变量名；"
                       "如需提出新维度，请写在 summary 或 unresolved 中。", round_number=round_number)
            unknown_variables = set(step.state_changes) - allowed_variables
            if unknown_variables:
                step.state_changes = {key: value for key, value in step.state_changes.items() if key in allowed_variables}
                step.unresolved.append(f"模型提出未定义变量 {', '.join(sorted(unknown_variables))}；未写入状态。")
            step.id = f"S{round_number}"
            step.parent_ids = [a.id for a in round_actions]
            step.round = round_number
            step.parent_state = parent
            step.next_state = round_number
            step.kind = "simulation"
            check_ids(step.evidence_ids, {e.id for e in evidence}, "环境推进")
            check_ids(step.assumption_ids, {a.id for a in world.assumptions}, "环境推进假设")
            steps.append(step)
            current = {**current, "state_version": round_number, "variables": {**current.get("variables", {}), **step.state_changes}, "simulation_summary": step.summary}
        return {"actions": [a.model_dump(mode="json") for a in actions], "simulation": [s.model_dump(mode="json") for s in steps]}

    def review_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        evidence = [Evidence.model_validate(x) for x in state["evidence"]]
        world = WorldState.model_validate(state["world"])
        review = ask("review", {"question": state["question"], "evidence": evidence_for_model(evidence, 1200), "evidence_assessment": state["evidence_assessment"], "world": state["world"], **trace_for_model(state)}, Review,
                     "检查给定证据节选是否支持关键判断、遗漏反证和模拟跳步。最多列 5 条关键问题，每条不超过 80 字；严重问题用 blocked。"
                     "信息截至日之后的结果未知是预测对象，不得要求未来证据来证明结果；可指出截至日当时缺少的资料。"
                     "affected_ids 可引用已有证据、假设、主体、行动或模拟编号，不能新造编号或证据。")
        review, policy_audit = apply_review_policy(review, question)
        future_gap_found = policy_audit["removed_future_demands"]
        if future_gap_found:
            review.issues.append(ReviewIssue(
                severity="medium", claim="预测期结果未知应由概率表达",
                explanation="已移除仅要求未来实际结果的意见；是否能估计概率仍由证据审查决定。"))
        if not evidence:
            review.status = "blocked"
            review.issues.append(ReviewIssue(severity="high", claim="证据包为空", explanation="没有可核查的外部证据，不能给概率。"))
        if all(e.source_type == "snippet_only" for e in evidence) and evidence:
            review.issues.append(ReviewIssue(severity="medium", claim="来源仅有搜索片段", explanation="未取得原文，结论需保留限制。"))
        if any(issue.severity == "high" for issue in review.issues) or review.unsupported_claims:
            review.status = "blocked"
        valid = ({e.id for e in evidence} | {a.id for a in world.assumptions} | {a.id for a in world.actors}
                 | {a["id"] for a in state["actions"]} | {s["id"] for s in state["simulation"]})
        for issue in review.issues:
            issue.affected_ids = canonical_ids(issue.affected_ids, valid)
            check_ids(issue.affected_ids, valid, "审查意见")
        review.probability_basis = "full" if review.status != "blocked" else "none"
        policy_audit["evidence_audit"] = None
        policy_audit["evidence_available_at_cutoff"] = None
        # Preserve the existing escape path for a future-contaminated world.
        # This signal can request a separate evidence audit, never erase an
        # opinion or relax the review status.
        world_future_signal = mistakes_future_outcome_for_missing_evidence(world.summary, question)
        policy_audit["world_future_signal"] = world_future_signal
        if (review.status == "blocked" or future_gap_found or world_future_signal) and question.mode == "binary" and evidence:
            audit = ask("evidence_audit", {"question": state["question"], "evidence": evidence_for_model(evidence, 1200),
                                           "evidence_assessment": state["evidence_assessment"]}, EvidenceOnlyAudit,
                        "仅审查信息截至日已有的外部证据，不使用世界状态、假设、主体行动或模拟结果。"
                        "source_type=exercise 表示用户事后整理的历史练习资料，不代表内容虚构；须有截至日前的发布日期，"
                        "但不是当时冻结的盲回测，应提示回看偏差。"
                        "判断是否足以给一个有保留、未经校准的主观概率。未来结果尚未发生、资料仅有一两个来源或存在延期风险，"
                        "都不是自动阻断理由，应通过不确定的概率表达；若证据本身为空、晚于截至日、无法核查或不支持问题，才设 can_estimate=false。")
            policy_audit["evidence_audit"] = audit.model_dump(mode="json")
            policy_audit["evidence_available_at_cutoff"] = all(available_at_cutoff(e, question.as_of) for e in evidence)
            if audit.can_estimate and policy_audit["evidence_available_at_cutoff"]:
                review.probability_basis = "evidence_only"
            else:
                review.status = "blocked"
                review.probability_basis = "none"
        policy_audit["final_status"] = review.status
        policy_audit["final_probability_basis"] = review.probability_basis
        return {"review": review.model_dump(mode="json"), "review_policy_audit": policy_audit}

    def forecast_node(state: FlowState):
        question = QuestionSpec.model_validate(state["question"])
        evidence = [Evidence.model_validate(x) for x in state["evidence"]]
        world = WorldState.model_validate(state["world"])
        review = Review.model_validate(state["review"])
        evidence_only = review.probability_basis == "evidence_only"
        price_context = market_price_context(question, evidence)
        if evidence_only:
            world = WorldState(summary="仅使用事前外部证据")
            simulation = []
            forecast_payload = {"question": state["question"], "evidence": evidence_for_model(evidence, 900),
                                "valid_evidence_ids": [e.id for e in evidence],
                                "valid_assumption_ids": [], "valid_simulation_ids": []}
            instructions = ("这是二元事件的事前预测，必须给出非 null 的是/否主观概率。"
                            "仅使用给定的截至日外部证据，完全忽略世界建模、审查推断、模拟和未经核实的假设；"
                            "支持与反对的每条主张只能引用有效的 E 编号。"
                            "预测期结果尚未发生是预测目标，不能要求它作为事前证据。"
                            "证据有限时把不确定性体现在接近中性的概率中，并说明来源和回看偏差；不得假装概率已校准。")
            probability_instructions = (f"概率键必须严格为 {question.outcomes}，数值在 0 到 1 且合计为 1，"
                                        "probabilities 不能为 null。")
            forecast_schema = EvidenceOnlyForecast
        else:
            simulation = [SimulationStep.model_validate(x) for x in state["simulation"]]
            forecast_payload = {"question": state["question"], "evidence": evidence_for_model(evidence, 900),
                                "evidence_assessment": state["evidence_assessment"], "world": state["world"],
                                **trace_for_model(state), "review": state["review"],
                                "valid_evidence_ids": [e.id for e in evidence],
                                "valid_assumption_ids": [a.id for a in world.assumptions],
                                "valid_simulation_ids": [s["id"] for s in state["simulation"]]}
            instructions = "只用已给资料与审查过的判断。支持/反对的每条主张必须至少引用一个有效证据、假设或模拟编号；无法引用的主张请删除。"
            probability_instructions = (f"概率键必须严格为 {question.outcomes}，数值在 0 到 1 且合计为 1；"
                                        "概率是未经校准的主观判断；证据不足或开放问题必须用 null。")
            forecast_schema = Forecast
        if price_context:
            forecast_payload["market_price_context"] = price_context
            instructions += ("对于市场价格问题，先比较预测期限与历史价格覆盖：一两日涨势不能直接外推到月末，"
                             "高振幅也可能意味着回撤；宏观指标与指数涨跌之间不能直接画等号。"
                             "没有查到利空消息不是上涨证据。若缺少同期限历史基准率、波动和估值资料，"
                             "不要把微弱证据表达成明显的方向优势；在局限中指出缺少哪些事前资料。"
                             "历史练习绝不可使用信息截至日之后的实际结果。")
        for attempt in range(2):
            forecast = ask("forecast", forecast_payload, forecast_schema,
                           instructions + "语言简洁。" + probability_instructions)
            forecast.probability_basis = "evidence_only" if evidence_only else "full"
            canonicalize_forecast_ids(forecast, evidence, world, simulation)
            if evidence_only:
                # The evidence-only report cannot acquire new model assumptions.
                # Keep evidence-backed prose, but remove references to the excluded branch.
                forecast.key_assumptions = []
                for claim in forecast.supporting + forecast.opposing:
                    claim.assumption_ids = []
                    claim.simulation_ids = []
                forecast.supporting = [claim for claim in forecast.supporting if claim.evidence_ids]
                forecast.opposing = [claim for claim in forecast.opposing if claim.evidence_ids]
                forecast.limitations = [item for item in forecast.limitations
                                        if not mistakes_future_outcome_for_missing_evidence(item, question)]
            try:
                validate_forecast(forecast, question, evidence, world, simulation, review,
                                  require_probability=evidence_only)
                if evidence_only:
                    forecast.limitations.append("概率仅依据截至日已有证据，未采用模拟行动或建模假设。")
                if (price_context and price_context["forecast_horizon_days"] >= 14
                        and price_context["dated_price_observation_days"] <= 3):
                    forecast.limitations.append(
                        f"证据包中明确标注事件日的价格资料只有 {price_context['dated_price_observation_days']} 个交易日，"
                        f"预测跨度约 {price_context['forecast_horizon_days']} 天；未据此验证同期限历史基准率或波动分布。")
                if any(e.source_type == "exercise" and e.retrieved_at > question.as_of for e in evidence):
                    forecast.limitations.append("历史演练资料是事后按发布日期整理，并非截至日冻结快照；可能存在回看偏差。")
                return {"forecast": forecast.model_dump(mode="json")}
            except ValueError as exc:
                forecast_payload["validation_feedback"] = f"上次报告未通过校验：{exc}。请修正后重新输出完整 JSON。"
        forecast = repair_forecast(forecast, question, evidence, world, simulation, review,
                                   forecast_payload["validation_feedback"])
        if any(e.source_type == "exercise" and e.retrieved_at > question.as_of for e in evidence):
            forecast.limitations.append("历史演练资料是事后按发布日期整理，并非截至日冻结快照；可能存在回看偏差。")
        return {"forecast": forecast.model_dump(mode="json")}

    graph = StateGraph(FlowState)
    for name, fn in (("define_question", question_node), ("retrieve", evidence_node), ("model_world", world_node), ("simulate", simulation_node), ("audit", review_node), ("synthesize", forecast_node)):
        graph.add_node(name, fn)
    graph.add_edge(START, start_at)
    graph.add_edge("define_question", "retrieve")
    graph.add_edge("retrieve", "model_world")
    graph.add_edge("model_world", "simulate")
    graph.add_edge("simulate", "audit")
    graph.add_edge("audit", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


def execute(record: RunRecord, imported: list[Evidence], store, *, resume: bool = False):
    model = None
    stage_names = [stage for stage, _ in STAGE_NODES]
    current_stage = stage_names[0]
    stage_started = time.monotonic()
    try:
        model = None if record.demo else ModelClient()
        state: FlowState = {"question": record.question.model_dump(mode="json")}
        start_at = "define_question"
        if resume:
            pending = next(((stage, node) for stage, node in STAGE_NODES if stage not in record.stage_outputs), None)
            if pending is None:
                raise ValueError("所有阶段已有快照，无法继续")
            for stage, _ in STAGE_NODES:
                if stage == pending[0]:
                    break
                state.update(record.stage_outputs[stage])
            start_at = pending[1]
            current_stage = pending[0]
            record.retry_history.extend(record.errors)
            record.errors = []
            record.resume_count += 1
            record.finished_at = None
            if model:
                model.usage = record.usage.copy()
        graph = build_graph(record, imported, model, store.directory, start_at=start_at)
        record.status = "running"
        record.stage = current_stage
        record.failed_stage = None
        store.save(record)
        stage_started = time.monotonic()
        for update in graph.stream(state, stream_mode="updates"):
            node, output = next(iter(update.items()))
            stage = {"define_question": "question", "retrieve": "evidence", "model_world": "world", "simulate": "simulation", "audit": "review", "synthesize": "forecast"}[node]
            record.stage_durations[stage] = round(time.monotonic() - stage_started, 3)
            state.update(output)
            record.stage_outputs[stage] = output
            if "question_analysis" in output:
                record.question_analysis = QuestionAnalysis.model_validate(output["question_analysis"])
            if "evidence" in output:
                record.evidence = [Evidence.model_validate(x) for x in output["evidence"]]
                record.evidence_assessment = EvidenceAssessment.model_validate(output["evidence_assessment"])
            if "world" in output:
                record.world = WorldState.model_validate(output["world"])
            if "actions" in output:
                record.actions = [ActorAction.model_validate(x) for x in output["actions"]]
                record.simulation = [SimulationStep.model_validate(x) for x in output["simulation"]]
            if "review" in output:
                record.review = Review.model_validate(output["review"])
            if "forecast" in output:
                record.forecast = Forecast.model_validate(output["forecast"])
            if model:
                record.usage = model.usage.copy()
                record.model = getattr(model, "actual_model", None) or record.model
            next_index = stage_names.index(stage) + 1
            current_stage = stage_names[next_index] if next_index < len(stage_names) else "done"
            record.stage = current_stage
            store.save(record)
            stage_started = time.monotonic()
        record.status = record.forecast.status
        record.stage = "done"
    except BudgetExceeded as exc:
        record.status = "partial"
        record.stage = "partial"
        record.failed_stage = current_stage
        record.stage_durations[current_stage] = round(time.monotonic() - stage_started, 3)
        record.errors.append(str(exc))
    except Exception as exc:
        record.status = "failed"
        record.stage = "failed"
        record.failed_stage = current_stage
        record.stage_durations[current_stage] = round(time.monotonic() - stage_started, 3)
        record.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
    finally:
        if model:
            record.usage = model.usage.copy()
            record.model = getattr(model, "actual_model", None) or record.model
        record.finished_at = utcnow()
        store.save(record)
