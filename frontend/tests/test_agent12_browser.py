import json
import re
from copy import deepcopy
from playwright.sync_api import expect

QUESTION = {"question": "青岚项目能否按期发布正式版？", "mode": "scenario", "as_of": "2026-09-30T08:00:00Z",
    "resolve_by": None, "resolution_rule": "", "resolution_source": None, "user_assumptions": []}
FRAME = {"schema_version": 1, "draft_id": "draft_fixture", "revision": 1, "raw_question": QUESTION["question"],
    "proposed_spec": QUESTION, "inputs": [], "clarifications": [], "premises": [{"id": "P001", "content": "测试已完成",
    "origin": "user_explicit", "source_input_id": "I001", "original_span": "测试已完成", "rationale": "需要核查实际范围",
    "user_review": "pending", "treatment": "to_verify", "replaces_id": None}], "alternative_directions": ["还需核查兼容性"],
    "retrieval_plan": [], "status": "ready_for_confirmation", "analysis_record": {"validation_mode": "fixture"}, "demo_case_id": None}
RUN = {"run_id": "run_fixture", "question": {"id": "Q1", "outcomes": ["是","否"], **QUESTION}, "question_origin": "confirmed",
    "question_framing": FRAME, "confirmation_id": "confirm_fixture", "parent_run_id": None, "question_version": 1,
    "evidence_mode": "import", "demo": False, "status": "scenario_only", "stage": "done", "stage_outputs": {},
    "question_analysis": None, "evidence": [], "evidence_assessment": None, "world": None, "actions": [], "simulation": [],
    "review": None, "forecast": None, "settlement": None, "model": "fixture", "started_at": QUESTION["as_of"], "finished_at": QUESTION["as_of"],
    "usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}, "errors": []}


def routes(page, *, missing_key=False, runs=None, framing=None, passages=None):
    captured = {"runs": [], "confirmations": [], "analyses": []}
    frame = deepcopy(framing or FRAME)
    def handler(route):
        path = route.request.url.split("/api", 1)[1].split("?", 1)[0]
        method = route.request.method
        status = 200
        if path == "/health":
            data = {"ok": True, "model_configured": not missing_key, "search_configured": True, "model": "fixture"}
        elif path == "/examples": data = {"presets": []}
        elif path == "/settlements/summary": data = {"settled_count": 0, "scored_count": 0, "average_brier": None}
        elif path == "/questions/analyze":
            captured["analyses"].append(route.request.post_data_json)
            data = frame if not missing_key else {"detail": "未配置模型密钥，不能生成真实分析"}
            status = 200 if not missing_key else 503
        elif path.endswith("/confirm"):
            captured["confirmations"].append(route.request.post_data_json)
            data = {"confirmation_id": "confirm_fixture", "framing": frame, "question": RUN["question"], "revision": frame["revision"]}
        elif path == "/questions/draft_fixture": data = {"framing": frame, "confirmation": None}
        elif path == "/runs" and method == "POST":
            captured["runs"].append(route.request.post_data_json)
            data = {"run_id": "run_fixture", "status": "queued"}; status = 202
        elif path == "/runs": data = deepcopy(runs or [])
        elif path.endswith("/passages"): data = passages
        elif path == "/runs/run_fixture": data = deepcopy((runs or [RUN])[0])
        else: data = {"detail": "unknown test route"}; status = 404
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))
    page.route("**/api/**", handler)
    return captured


def prepare(page, app_url):
    page.goto(app_url)
    page.get_by_placeholder("例如：某产品能否在 12 月 20 日前发布正式版？").fill(QUESTION["question"])
    page.get_by_role("button", name="开放情景分析").click()
    page.get_by_role("button", name="分析问题", exact=True).click()


def test_question_confirmation_flow(page, app_url):
    captured = routes(page)
    prepare(page, app_url)
    expect(page.get_by_role("button", name="确认并继续", exact=True)).to_be_disabled()
    expect(page.get_by_text("待核查前提，不是已证实事实", exact=True)).to_be_visible()
    page.get_by_label("P001 前提处理").select_option("to_verify")
    page.get_by_role("button", name="确认并继续", exact=True).click()
    expect(page.get_by_text("问题已确认", exact=True)).to_be_visible()
    page.get_by_role("button", name="在线检索", exact=True).click()
    page.get_by_role("button", name=re.compile("^开始预测")).click()
    expect(page.get_by_text("run_fixture", exact=True).first).to_be_visible()
    assert captured["runs"][0]["confirmation_id"] == "confirm_fixture"
    assert "question" not in captured["runs"][0]


def test_edit_invalidates_confirmation(page, app_url):
    routes(page); prepare(page, app_url)
    page.get_by_label("P001 前提处理").select_option("to_verify")
    page.get_by_role("button", name="确认并继续", exact=True).click()
    expect(page.get_by_text("问题已确认", exact=True)).to_be_visible()
    page.get_by_placeholder("例如：某产品能否在 12 月 20 日前发布正式版？").fill("修改了范围，另一个版本能否发布？")
    expect(page.get_by_role("button", name=re.compile("^开始预测"))).to_be_disabled()
    expect(page.get_by_text("内容已修改，请重新分析后确认", exact=True)).to_be_visible()


def test_refresh_loads_saved_draft(page, app_url):
    routes(page)
    page.add_init_script("localStorage.setItem('forecastlab.agent12.draft_id','draft_fixture')")
    page.goto(app_url)
    expect(page.get_by_text("需要核查实际范围", exact=True)).to_be_visible()
    expect(page.get_by_placeholder("例如：某产品能否在 12 月 20 日前发布正式版？")).to_have_value(QUESTION["question"])
    page.reload()
    expect(page.get_by_text("需要核查实际范围", exact=True)).to_be_visible()


def test_missing_key_does_not_create_fake_analysis(page, app_url):
    routes(page, missing_key=True); prepare(page, app_url)
    expect(page.get_by_text("未配置模型密钥，不能生成真实分析", exact=True)).to_be_visible()
    expect(page.get_by_text("需要核查实际范围", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^开始预测"))).to_be_disabled()


SOURCE_TEXT = "说明😀：计划🙂延期，不代表项目取消。"

def evidence_run():
    run = deepcopy(RUN)
    run["question_framing"]["premises"].append({**run["question_framing"]["premises"][0], "id": "P002", "content": "另一项前提"})
    def evidence(eid, content, kind):
        return {"id": eid, "source_url": "https://example.org/" + "long-path-"*20, "file_id": None, "title": "来源 " + eid,
            "publisher": "样例来源", "published_at": None, "updated_at": None, "retrieved_at": QUESTION["as_of"], "event_at": None,
            "excerpt": content, "claim": "", "snapshot_path": "sources-v1/server-owned.json", "content_hash": "abc", "snapshot_hash": "fixture-hash",
            "source_type": "snippet_only" if kind == "snippet" else "secondary", "source_group": "root-1", "date_status": "unknown", "conflict_group": None,
            "content_kind": kind, "content_truncated": True, "source_kind": "unknown", "source_kind_basis": "无法确定是否一手来源",
            "source_group_basis": "明确转载标记，尚待人工核查", "possible_same_source": [], "aliases": [],
            "date_basis": {"retrieved_at": "后端实际取得时间"}, "availability": "unverified", "event_status": "planned"}
    run["evidence"] = [evidence("E001", SOURCE_TEXT, "snippet"), evidence("E002", "测试已完成", "body")]
    c1 = {"evidence_id": "E001", "snapshot_hash": "fixture-hash", "paragraph_id": "B000001", "quote": "计划🙂延期", "start": 4, "end": 9}
    c2 = {"evidence_id": "E002", "snapshot_hash": "fixture-hash", "paragraph_id": "B000001", "quote": "测试已完成", "start": 0, "end": 5}
    run["evidence_assessment"] = {"summary": "固定样例中的两项发现", "evidence_ids": ["E001", "E002"], "conflicts": [], "gaps": [],
        "findings": [{"id": "F001", "target_premise_ids": ["P001"], "claim": "计划延期的迹象", "relation": "challenges", "citations": [c1], "limitation": "只有摘要，不能断言结果"},
                     {"id": "F002", "target_premise_ids": ["P002"], "claim": "另一项有效判断", "relation": "supports", "citations": [c2], "limitation": "测试范围有待核查"}],
        "conflict_details": [], "gap_details": [], "retrieval_log": [], "exclusions": [],
        "rejected_findings": [{"candidate": {"claim": "不应进入有效结果的伪造内容"}, "reason": "引文未出现在原文"}]}
    return run


def inspect_view(page, app_url, run=None):
    run = run or evidence_run()
    routes(page, runs=[run], passages={"evidence_id": "E001", "text": SOURCE_TEXT, "snapshot_hash": "fixture-hash", "content_truncated": True,
        "passages": [{"paragraph_id": "B000001", "text": SOURCE_TEXT, "start": 0, "end": len(SOURCE_TEXT), "snapshot_hash": "fixture-hash"}]})
    page.goto(app_url)
    page.get_by_role("button", name=re.compile("02.*证据与模型")).click()


def test_filter_findings_by_premise_and_relation(page, app_url):
    inspect_view(page, app_url)
    page.get_by_label("按前提筛选").select_option("P001")
    expect(page.get_by_test_id("valid-findings").get_by_text("计划延期的迹象", exact=True)).to_be_visible()
    expect(page.get_by_test_id("valid-findings").get_by_text("另一项有效判断", exact=True)).to_have_count(0)
    page.get_by_label("按前提筛选").select_option("")
    page.get_by_label("按关系筛选").select_option("supports")
    expect(page.get_by_test_id("valid-findings").get_by_text("另一项有效判断", exact=True)).to_be_visible()
    expect(page.get_by_test_id("valid-findings").get_by_text("计划延期的迹象", exact=True)).to_have_count(0)


def test_quote_highlight_keeps_emoji_offsets(page, app_url):
    inspect_view(page, app_url)
    page.get_by_role("button", name="查看 E001 原文", exact=True).click()
    expect(page.locator("mark")).to_have_text("计划🙂延期")
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("button", name="查看 E001 原文", exact=True)).to_be_focused()


def test_rejected_findings_not_in_valid_results(page, app_url):
    inspect_view(page, app_url)
    expect(page.get_by_test_id("valid-findings").get_by_text("不应进入有效结果的伪造内容", exact=True)).to_have_count(0)
    expect(page.get_by_text("校验未通过", exact=True)).to_be_visible()
    page.get_by_text("校验未通过", exact=True).click()
    expect(page.get_by_text("引文未出现在原文", exact=True)).to_be_visible()


def test_legacy_record_has_no_fabricated_framing(page, app_url):
    run = deepcopy(RUN); run["question_framing"] = None; run["question_origin"] = "legacy_direct"
    inspect_view(page, app_url, run)
    expect(page.get_by_text("旧版记录未包含问题理解/逐项发现", exact=True)).to_be_visible()
    expect(page.get_by_test_id("valid-findings")).to_have_count(0)


def test_source_limitations_visible(page, app_url):
    inspect_view(page, app_url)
    expect(page.get_by_text("只有搜索摘要，未取得正文", exact=True).first).to_be_visible()
    page.get_by_role("button", name="查看 E001 原文", exact=True).click()
    expect(page.get_by_role("dialog").get_by_text("发布时间未知", exact=True)).to_be_visible()
    expect(page.get_by_role("dialog").get_by_text("正文已截断，不是完整原文", exact=True)).to_be_visible()
    expect(page.get_by_text("明确转载标记，尚待人工核查", exact=True)).to_be_visible()


def test_mobile_findings_and_drawer_fit_viewport(page, app_url):
    page.set_viewport_size({"width": 390, "height": 844})
    inspect_view(page, app_url)
    page.get_by_role("button", name="查看 E001 原文", exact=True).click()
    expect(page.locator("mark")).to_have_text("计划🙂延期")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="关闭详情", exact=True).click()
    expect(page.get_by_role("button", name="查看 E001 原文", exact=True)).to_be_focused()



def test_real_backend_fixed_teaching_flow(page, app_url):
    # No page.route: this test exercises the actual backend, SQLite, graph and UI.
    page.goto(app_url)
    page.get_by_role("button", name="体验问题与证据新流程", exact=True).click()
    page.get_by_role("button", name="分析问题", exact=True).click()
    expect(page.get_by_text("需要补充信息", exact=True)).to_be_visible()
    page.get_by_label("这里的发布是可下载的正式版，还是测试版？（需补充）").fill("可下载的正式版")
    page.get_by_role("button", name="提交补充并重新分析", exact=True).click()
    expect(page.get_by_text("请核对系统理解", exact=True)).to_be_visible()
    page.get_by_label("P001 前提处理").select_option("to_verify")
    page.get_by_label("P002 前提处理").select_option("to_verify")
    page.get_by_role("button", name="确认并继续", exact=True).click()
    expect(page.get_by_text("问题已确认", exact=True)).to_be_visible()
    page.get_by_role("button", name=re.compile("^开始预测")).click()
    expect(page.get_by_text("已完成", exact=True).first).to_be_visible(timeout=10000)
    page.get_by_role("button", name=re.compile("02.*证据与模型")).click()
    expect(page.get_by_test_id("valid-findings")).to_be_visible()
    page.get_by_role("button", name="查看 E002 原文", exact=True).click()
    expect(page.locator("mark")).to_have_text("两个高优先级兼容问题")
    expect(page.get_by_role("dialog").get_by_text("教学虚构材料", exact=True)).to_be_visible()
