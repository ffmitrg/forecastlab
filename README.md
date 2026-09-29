# ForecastLab

基于证据溯源与多主体推演的课程级预测工作台。这个仓库实现了所附 [工程计划](docs/agent-framework-plan-v1.html) 的核心 Demo：统一问题格式、证据包或在线检索、LangGraph 状态图、主体独立行动、两轮环境推进、审查、主观概率、SQLite 回放与报告导出。

**状态说明：**教学演示使用明确标注的虚构材料与固定输出，用于无密钥联调。真实预测需要模型服务 Key；在线检索另需 Tavily Key。当前没有真实实验结果，也不声称概率已校准。

## 快速启动

环境：macOS/Linux、Python 3.12、`uv`、Node.js 20.19+/22.12+、npm。

```bash
uv sync --locked
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.api:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>，点击“运行教学演示”。它会经过同一个 LangGraph 流程，显示 3 个主体、2 轮行动、审查、引用和回放。首次运行会在 `data/` 创建 SQLite 与各阶段 JSON 快照。

首页另有科技、体育、公共事件三个**问题预设**。它们只填写问题和结算规则；需要真实证据与模型密钥才能生成新预测。

前端开发模式可另开终端运行 `cd frontend && npm run dev`，Vite 将 `/api` 代理到 8000 端口。后端修改后需重启服务。

## Docker 部署

服务器安装 Docker 和 Docker Compose 后，在项目根目录放置仅服务器可读的 `.env`，运行：

```bash
docker compose up -d --build
docker compose ps
```

容器使用 `uv` 从锁文件安装 Python 依赖，Node 构建前端；SQLite 数据和证据快照保存在宿主机 `data/`。服务监听服务器的 8000 端口，可在浏览器打开 `http://服务器公网 IP:8000/`。需要停止服务时运行 `docker compose down`；这不会删除 `data/`。更新代码后执行 `git pull --ff-only && docker compose up -d --build`。

若服务器已有 Python 3.12，也可以用 `uv` 安装依赖并用 systemd 运行，不需要在服务器构建前端：

```bash
uv sync --locked --no-dev
# 将本地 frontend/dist/ 上传到服务器的 frontend/dist/
sudo useradd --system --home-dir /opt/forecastlab --shell /usr/sbin/nologin forecastlab
sudo chown forecastlab:forecastlab .env data
sudo chmod 600 .env
sudo chmod 700 data
sudo cp deploy/forecastlab.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forecastlab
```

systemd 服务使用独立的 `forecastlab` 用户。若该用户已存在，跳过 `useradd`。服务同样监听服务器的 8000 端口，可直接通过公网 IP 在浏览器访问。

## 启用真实运行

1. 将 `.env.example` 复制为 `.env`，填写 `QWEN_API_KEY`、`QWEN_BASE_URL` 和 `QWEN_MODEL`。项目使用 OpenAI 兼容接口调用模型；已有 DeepSeek 官方接口配置也可继续使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。两组同时设置时，优先使用 `QWEN_*`。
2. 若使用在线检索，另填 `TAVILY_API_KEY`。没有检索 Key 时，导入 JSON 证据包即可。
3. 输入可结算的二元问题、信息截至时间、截止时间和结算规则。开放问题改选“情景分析”，其概率始终为 `null`。
4. 导入证据包或选择在线检索，提交运行。单进程同一时间只接受一个运行；页面每 2 秒查询进度。失败或中断后可从已保存的阶段继续，不重复执行成功的阶段。

## 结果结算与评分

二元预测的结算时间到达后，可在“结果与历史”中录入实际结果、可核查来源和观测值。系统保留原概率，另存结算记录，并计算二元 Brier 分数 `(P(是) - 实际是的取值)^2`；越低越好。没有有效概率的运行仍可记录结果，但不纳入评分。页面会展示已结算数量和平均分；少量案例的平均分不能证明概率已校准。

市场价格预测会提示模型核对预测跨度与证据包中有日期的价格资料覆盖，避免把一两天涨势直接外推到月末。历史练习资料如在预测截点之后才取回，会标记“历史回看·非盲测”。一次结果不应用来回写事前概率；完整案例见[科创 50 历史预测回看](docs/market-postmortem-2026-07.md)。

导入证据包是 JSON 数组，或包含 `evidence` 数组的对象。最小条目：

```json
[
  {
    "title": "来源标题",
    "source_url": "https://example.org/actual-source",
    "publisher": "发布方",
    "published_at": "2026-09-01T00:00:00Z",
    "excerpt": "从原始来源保存的支持判断的原文片段。"
  }
]
```

把示例网址和内容替换为真实来源。后端会分配 `E001...` 编号、抓取/导入时间和 SHA-256 内容哈希。严格盲回测需要在预测截点前冻结的证据快照；事后找到、可证明发表于截点前的资料只能标为 `source_type: "exercise"`，属于有回看偏差风险的历史练习。若来源只有搜索摘要，请在导入材料中明确注明；在线 Tavily 返回没有正文时会自动标为 `snippet_only`。

当前兼容接口配置使用 `qwen3.8-flash`，结构化 JSON 调用会关闭该模型的思考模式以缩短等待时间；实际可用模型以对应服务提供方的模型列表为准。`QWEN_MODEL` 可切换模型。首次接入应使用少量问题确认账户权限、模型参数和账单。运行上限由 `.env` 中的 `FORECASTLAB_MAX_CALLS`（默认 18）与 `FORECASTLAB_MAX_SECONDS`（默认 300）控制。运行详情会记录各阶段耗时；失败时显示中断阶段和原因，可从该阶段继续。

## 工作流与边界

```text
QuestionSpec → QuestionAnalysis → Evidence[] + EvidenceAssessment
             → WorldState + ActorProfile[]
             → {ActorAction × 3} → SimulationStep S1
             → {ActorAction × 3} → SimulationStep S2
             → Review → Forecast
```

- 模型只生成结构化判断候选；URL、证据编号、内容哈希和运行状态由代码管理。模型读取长度受控的证据节选，完整检索内容保存在本地快照中。
- 同一轮的主体读取同一个父状态；环境在收齐行动后统一推进。模拟结果保持 `M/S` 身份，不会变成 `E` 类外部证据。
- 代码核对引用 ID、时间截点、父状态和概率；审查 Agent 核对内容支持度。完整推演被阻断后，只有独立证据审查通过才允许 `evidence_only` 概率，否则不输出概率。
- 所有角色共用同一个已配置模型，不等于独立专家；概率是主观判断，未经校准。
- 在线检索由 Tavily 提供，模型本身不承担互联网搜索。首版没有账号、GPU、断点自动续算和全网持续监控。

## API

启动后访问 `/docs` 查看 OpenAPI。主要接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/questions/parse` | 检查可结算目标，返回缺失字段 |
| POST | `/api/runs` | 创建运行，返回 202 与 `run_id` |
| POST | `/api/runs/{id}/resume` | 从失败、中断或部分完成运行的首个未完成阶段继续 |
| GET | `/api/runs` | 历史列表 |
| GET | `/api/runs/{id}` | 阶段、运行记录与结果 |
| GET | `/api/runs/{id}/evidence` | 来源详情 |
| POST | `/api/runs/{id}/settlement` | 截止后记录实际结果与来源，计算二元 Brier |
| GET | `/api/settlements/summary` | 已结算数量与平均评分 |
| GET | `/api/runs/{id}/export?format=html\|json` | 导出报告或原始记录 |
| GET | `/api/health` | 密钥配置状态，不返回密钥 |

`POST /api/runs` 的请求体包含 `question`（`QuestionSpec`）、`evidence_mode`（`import` / `online` / `reuse` / `demo`）、`evidence`（导入时必填）和可选 `parent_run_id`。`reuse` 必须提供父运行 ID，后端沿用已保存的证据快照；修改条件后重新提交会产生新 ID，历史记录不被覆盖。

## 测试与评估

```bash
uv run pytest -q
cd frontend && npm run build
```

测试覆盖完整演示、引用与概率约束、时间截点、服务重启标记，以及 HTML 导出转义。`examples/classroom-demo.json` 是**教学虚构情境**，不能用于真实预测质量评估。

审查模块另有固定案例的改前/改后对照，可用 `uv run python eval/review_policy.py` 复现，或加 `--case D03` 查看“未来发布计划被误删”的例子。改动范围、验证结果和审查日志位置见 [Agent 6 改进说明](docs/agent6-review-improvement.md)。

实际实验应先冻结问题、提示词、模型、证据包和预算。`eval/baseline.py` 用**同一问题与证据包**做一次单 Agent 模型调用：

```bash
uv run python eval/baseline.py path/to/frozen-pack.json --output baseline.json
```

结算后的结果表使用 `[{"id":"Q1","category":"tech","outcome":1,"full_p":0.6,"baseline_p":0.5}]` 格式，缺失概率填 `null`。评分脚本同时输出成功覆盖率、成功样本 Brier 与将拒答按 0.5 回退的全样本 Brier：

```bash
uv run python eval/score.py path/to/settled-results.json
```

请保留失败/拒答、引用人工抽查、耗时与 token 记录；历史问题要说明模型可能记住答案。仓库不附带虚构的实验得分。

## 目录

```text
backend/app/       数据契约、证据入口、兼容接口适配、状态图、API、SQLite
backend/tests/     契约与端到端测试
frontend/src/      React 四视图工作台
examples/          教学演示数据
eval/              单 Agent 基线与 Brier/覆盖率脚本
docs/              原始工程计划与课程交付模板
```

## 项目材料与披露

本工程根据用户提供的 v0.1 计划搭建。课程要求、Decitron 相关描述和参考资料仍需小组在最终提交前逐项核对。Codex 参与了代码、测试、页面及文档起草；正式报告中的 LLM Usage Statement 应补充后续真实使用情况和人工核查记录。
