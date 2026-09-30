# Agent 1–2 实际验证记录

## 已执行结果

本机最终工作树验证：**105 个 Python 后端/包装测试通过，11 个 Chromium 浏览器测试通过，TypeScript/Vite 生产构建通过**。无跳过测试；Python 汇总保留两条原依赖警告。重新执行过锁定依赖安装和 npm ci，具体版本与锁文件哈希见 `validation-artifacts/environment.json`。

浏览器十项使用拦截 API 的固定响应测试交互，一项不拦截后端响应，实际访问临时本机 FastAPI、SQLite 和 LangGraph，走完固定教学案例。测试服务器仅在环回地址启动，测试结束自动终止自己的进程。

固定题对评估保留两个同事件的中性/引导性输入：两者均是预制教学响应，`semantic_review=not_performed`。真实模型、在线搜索和人工语义评估**尚未执行**，不得把以下矩阵中的结构/流程通过写成真实效果已证明。

## 可复跑命令

```bash
uv sync --locked --group browser
uv run pytest -q
(cd frontend && npm ci && npm run build)
uv run --group browser python -m playwright install chromium
uv run --group browser pytest frontend/tests/test_agent12_browser.py -q
uv run python eval/agent12.py --mode fixture --cases examples/agent12/neutral-leading-pairs.json --output fixture-eval.json
git diff --check
```

源码交付通过 `scripts/package_agent12.py` 生成。该脚本的三个自动化测试已实际验证：排除运行数据与凭证、ZIP 每一成员的 SHA-256、在干净测试基线上执行 `git apply --check` 并重建源文件。针对真实项目基线的再次打包/复测结果保存在交付目录 `verification/`，其最终 SHA 以交付 `manifest.json` 为准。

## 26 项设计验收映射

下表均已有自动化测试覆盖并在上述最终汇总中通过；最后一栏限制仍有效。除注明 frontend 外，测试文件在 `backend/tests/`。

| 编号 | 检查内容 | 测试 | 解释边界 |
|---|---|---|---|
| Q01 | 原问题缺规则仍交给分析 | `test_agent12_question.py::test_raw_question_reaches_model_without_resolution_rule` | 固定模型输入契约 |
| Q02 | 含糊问题阻断确认 | `test_agent12_question.py::test_ambiguous_question_blocks_confirmation` | 真实歧义识别语义未测 |
| Q03 | 明确问题不反复分析 | `test_agent12_question.py::test_clear_question_needs_one_analysis_and_retry_is_idempotent` | 固定候选流程 |
| Q04 | 中性输入允许零前提 | `test_agent12_question.py::test_neutral_variant_may_have_no_premises` | 真实中性/引导性对照未测 |
| Q05 | 否认前提不再作为依据 | `test_agent12_evidence.py::test_rejected_premise_never_enters_valid_findings` | 另测混合目标查询整条排除 |
| Q06 | 待核查不当用户H条件 | `test_agent12_integration.py::test_to_verify_is_not_added_as_user_condition` | 两种treatment参数化 |
| Q07 | 版本冲突和输入修改失效 | `test_agent12_storage.py::test_revision_compare_and_swap` | 另有浏览器编辑失效测试 |
| Q08 | 重复确认幂等 | `test_agent12_storage.py::test_same_confirmation_is_idempotent` | 不同决定不能覆盖 |
| E01 | 三方向进入十条预算 | `test_agent12_retrieval.py::test_three_query_buckets_survive_ten_source_limit` | 不是正反数量配额 |
| E02 | 去重保留查询和日期 | `test_agent12_retrieval.py::test_dedup_keeps_all_query_ids_and_alias_dates` | 明确/疑似同源分开 |
| E03 | 选择后部相关段落 | `test_agent12_provenance.py::test_late_relevant_passage_selected` | 轻量词项选择，未作检索质量基准 |
| E04 | 原文截断有标记 | `test_agent12_provenance.py::test_snapshot_cap_and_truncation_flag` | 浏览器亦展示摘要/截断限制 |
| E05 | 引用/快照/P/E校验 | `test_agent12_evidence.py::test_each_finding_requires_valid_quote_and_active_premise` | 四种引用错误参数化 |
| E06 | 一源支持/挑战不同前提 | `test_agent12_evidence.py::test_one_source_supports_and_challenges_different_premises` | 真实关系判断质量未测 |
| E07 | 不信客户端历史冻结声明 | `test_agent12_provenance.py::test_forged_historical_snapshot_not_trusted` | 历史练习明确标记 |
| E08 | 未来结果不是缺口 | `test_agent12_evidence.py::test_future_resolution_is_not_evidence_gap` | 另测计划事件时间；真实模型措辞未测 |
| E09 | 部分/全部失败保留日志 | `test_agent12_retrieval.py::test_partial_and_total_failure_keep_logs` | 恢复不重置搜索预算 |
| E10 | 冲突比较口径 | `test_agent12_evidence.py::test_conflict_scope_differences_remain_visible` | 人工原文与关系核查未做 |
| I01 | 确认内容进入真实图输入 | `test_agent12_integration.py::test_confirmed_request_controls_graph_input` | 捕获节点payload |
| I02 | 重用来源重新分析 | `test_agent12_integration.py::test_reuse_reassesses_without_mutating_parent` | 父记录保持相等 |
| I03 | 重启/恢复保留日志和预算 | `test_agent12_integration.py::test_failed_retrieval_logs_survive_resume` | 另测刷新、持久化计数与时间并集 |
| I04 | 旧运行兼容 | `test_agent12_contracts.py::test_legacy_record_loads_without_agent12_fields` | 原结算、导出、回放测试仍在 |
| I05 | 共享调用预算/有限修复 | `test_agent12_budget.py::test_preparation_reduces_runtime_allowance` | 失败token保持未知 |
| U01 | 实际本机完整教学流程 | `frontend/tests/test_agent12_browser.py::test_real_backend_fixed_teaching_flow` | 不拦截API；模型仍固定 |
| U02 | 没密钥不伪造真实分析 | `test_agent12_demo.py::test_demo_rejects_arbitrary_question` | 另有浏览器missing-key验证 |
| S01 | 安全路径和转义打包 | `test_agent12_provenance.py::test_path_and_symlink_escape_rejected` | 另有HTML转义/包排密钥/补丁检查 |

## 审核与历史失败

整分支自检发现六项此前测试未覆盖的边界，各先加入失败测试再修复，详见 `review.md`。没有独立审核工具或另一位审核者参与；不称“第三方代码审查通过”。

原始基线的日期相关测试最初 28 通过、1 失败，固定该测试自身时间后 29 通过。该修复只影响测试，不削弱生产历史证据检查。新增功能均保留分阶段本地提交和红/绿验证日志；交付目录保留汇总证据，不把示例输出当研究指标。

## 日志

`final-sync.log`、`final-backend.log`、`final-frontend.log`、`final-browser.log`、`final-eval.log` 和 `t12-red/green.log` 为本次实际命令输出，绝对工作树路径已替换成 `<checkout>`。`fixture-eval.json` 包含全部固定案例、输入、输出、耗时及未做语义检查的标记。

详细限制见 [limitations.md](limitations.md)。
