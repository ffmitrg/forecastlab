# Agent 1–2 交接说明

## 范围与版本

基线：`bb4d339a9fe94c016e751998943fc0f366b6ea0a`。本次改造是原项目上的增量实现，不是整个 ForecastLab 的原创实现。Agent 1–2 是 Zhang Kaiqi、Wang Kongtao、Tang Naisheng 三人的共同职责，本交付不重新分配三人内部工作，也不代写个人独占贡献。

新增问题澄清、候选前提、不可覆盖的确认版本、带核查目标的检索、逐项发现、原文定位和来源状态。Agent 3–7 的主体策略、模拟轮次和概率算法未重写；仅添加输入适配、用户情景条件映射与必要的预算/来源检查。

## 运行与演示

```bash
# 在项目根目录，先安装锁定依赖并构建
uv sync --locked
(cd frontend && npm ci && npm run build)
uv run uvicorn app.api:app --app-dir backend --host 127.0.0.1 --port 8765
```

也可执行 `bash scripts/start-local.sh`。该脚本优先使用 PATH 中的 uv；在本次本地交付目录中也识别相邻 `../tooling/bin/uv`。`REBUILD=1 bash scripts/start-local.sh` 会重新构建。需要 Python 3.12 和项目 README 指定的 Node.js 环境。`.env` 只放在自己的后端运行环境；没有密钥也可体验固定教学流程。

打开首页，点击“体验问题与证据新流程”，再点击“分析问题”。对发布口径的澄清填写“可下载的正式版”。分别选择 P001、P002 的处理方式，点击“确认并继续”，再点击“开始预测”。在“证据与模型”按前提筛选，点击“查看 E002 原文”，应看到“两个高优先级兼容问题”的原文高亮。

这条教学路径使用虚构材料和固定响应；只有软件流程、状态保存和引用校验是真的执行。不能用它证明真实模型识别能力或预测准确率。原“运行教学演示”仍保留，用于兼容旧流程。

## 对接格式

新运行保留 `QuestionSpec`，增加 `question_framing`，其中包含原始问题、规范化候选、用户输入版本、候选前提、检索任务和确认结果。P 前提、E 外部来源、F 证据发现、H 建模条件、M/S 模拟记录各有身份。

`EvidenceAssessment` 原字段 `summary/evidence_ids/conflicts/gaps` 继续存在；新增 `findings/conflict_details/gap_details/retrieval_log/rejected_findings/exclusions`。旧数组由结构化结果生成，便于原界面与下游继续读取。

每项 `EvidenceFinding` 至少包括 `id/target_premise_ids/claim/relation/citations/limitation`。引文包括 `evidence_id/snapshot_hash/paragraph_id/quote/start/end`；偏移是 Unicode 码点，浏览器必须使用 `Array.from(text)`，不能直接用 JavaScript UTF-16 下标。

前提 `treatment=to_verify` 不进入用户指定的 H 条件。只有用户选择 `scenario_condition` 时才创建对应 H，并记录 `premise_assumption_map`。这确认的是情景条件，不是事实真值。被否认的前提不进入有效发现；含有被否认目标的检索任务整条丢弃，必要时用规范化问题作中性检索。

世界建模、审查、报告收到活动问题理解和经过校验的发现。压缩上下文时，发现及其引文片段一起保留或一起省略，并记录省略限制；不能保留判断却删除支撑原文。F/P 不代替最终 E/H/M/S 引用。

## 兼容与修订

旧运行加载时新字段使用空默认值，页面明确显示“旧版记录未包含问题理解/逐项发现”。旧创建接口仍可传 `question`，标记 `legacy_direct`，不假称经过新确认流程。新页面只传服务端 `confirmation_id`，不能同时传另一份 `question`。

草稿每次分析产生新 revision。确认采用数据库事务与版本比较；相同决定重复确认返回同一记录，不同决定必须新版本。修改问题、时间、规则或前提决定后，前端旧确认失效；新运行插入时还会再次原子核验确认未过期。旧运行的回放、结算与导出仍可使用当时冻结的确认副本。

`reuse` 复制来源快照并重新分析，不复制上一次发现，不修改父运行。教学运行的来源不能复用为真实运行的证据。固定教学条件修订另建新教学运行。

## 来源与恢复

新来源存为 `sources-v1/<uuid>.json` 并登记到 `source-index.sqlite3`；源码包不含这些用户运行数据。服务端同时核对正文哈希与快照文件哈希，拒绝任意路径、符号链接和未登记文件。客户端导入的 `snapshot_path/retrieved_at/date_status` 不能作为历史冻结证明。

正文最多保存 200,000 码点，截断必须标记。模型原文预算按相关段落选择，不再只看开头。网络返回最多 8 MiB，单请求 25 秒，每运行最多一个批次、最多三个查询、最终最多十条在线来源；导入最多二十条。URL/正文精确去重保留查询归属与别名元数据。明确转载关系可分组；相似文本只标疑似，域名相同不等于同一源头。

每次模型请求发出前先写持久化预约。每草稿最多六次准备请求；关联准备与运行共十八次，格式/引用修复均计入。新 Agent 1–2 每次分析最多初次加一次修复；旧角色传输重试兼容原逻辑，但受总调用预算限制。未知 token 使用量保持未知。活动时间预算为 300 秒，用户等待编辑时间不计入；恢复时不重置预算，已测量并行请求按时间区间并集累计。

已保存的检索结果在恢复时复用。某次检索批次全部失败或在返回前中断，不在同一运行中重复联网消耗新查询；保留错误与日志，用户需创建新运行重新取证。不要把“从失败阶段继续”理解成无限重试检索。

## 代码入口

| 文件 | 职责 |
|---|---|
| `backend/app/schemas.py` | 兼容原类型的新版契约 |
| `question_service.py`、`agents/question.py` | 分析、澄清、前提与确认 |
| `storage.py`、`llm.py` | 原子版本、预约与预算 |
| `sources.py`、`provenance.py` | 检索、去重、快照、原文位置 |
| `agents/evidence.py` | 发现、冲突、缺口和上下文保护 |
| `graph.py`、`api.py` | 下游适配和服务入口 |
| `frontend/src/components/` | 确认界面、证据查看及相关状态 |
| `agent12_demo.py`、`eval/agent12.py` | 严格固定样例与显式真实评估入口 |

详见 [接口](api.md)、[验证记录](validation.md)、[局限](limitations.md)、[LLM 使用说明](llm-usage.md)。
