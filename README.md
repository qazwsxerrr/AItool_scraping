# AI 资讯抓取与筛选

项目从配置的信息源抓取 AI 资讯，经过多阶段筛选生成待审核日报，并通过 Web UI 展示已发布结果。以下命令均在项目根目录执行，并默认已完成依赖安装和 `.env` 配置。

## 技术栈

- **后端**：FastAPI + Pydantic + SQLAlchemy + Typer + Rich + Uvicorn
- **LLM / Agent**：OpenAI API（双协议结构化输出：Responses & Chat Completions）+ Stage C 自研 ReAct 智能体（多轮 Tool-Calling）
- **数据抓取与解析**：Feedparser + BeautifulSoup4 + HTTPX（支持 HTTP/2 & SOCKS 代理）
- **外部服务**：Tavily Search API（联网事实核验）+ GitHub REST API（Trending 趋势与 Release 追踪）+ RSSHub
- **前端**：Jinja2（服务端渲染 SSR）+ 现代 CSS3（流式去框化排版）+ 原生 JavaScript
- **数据库与配置**：SQLite + PyYAML（数据源规范与流水线配置管理）

### 各技术栈在项目中的职责划分

| 层次 / 模块 | 使用技术 | 在本项目中的具体作用 |
| :--- | :--- | :--- |
| **后端 API & CLI** | FastAPI, Uvicorn, Typer, Rich | 提供 Web 浏览界面路由与 JSON API，同时通过 Typer + Rich 提供多阶段流水线执行的富文本终端 CLI 工具。 |
| **数据建模与校验** | Pydantic v2 | 严格约束四阶段流水线（Stage A 准入、Stage B1 打分、Stage C 聚合、Stage D 选稿）的输入输出契约。 |
| **持久化存储** | SQLite, SQLAlchemy 2.0 | 维护资讯抓取历史、多阶段审查记录、事件聚类表以及流水线运行审计追踪。 |
| **AI 审查与智能体** | OpenAI API, 自研 Agent 架构 | 支持 Responses 和 Chat Completions 双协议，利用 JSON Schema 强类型结构化输出；Stage C 具备多轮 Tool-Calling、历史对比及争议事实查证能力。 |
| **联网检索与事实核验** | Tavily Search API | Stage C 审查智能体调用 Tavily 进行网络实时搜索与二次交叉信源核验。 |
| **多源资讯抓取** | HTTPX, Feedparser, BeautifulSoup4 | 支持高并发 HTTP/2 抓取与 SOCKS 代理，解析 RSS/Atom 提要，并对网页正文进行块级清洗与提取。 |
| **前端展示** | Jinja2, HTML5, CSS3, Vanilla JS | 采用轻量化 SSR 服务端渲染，配合现代化去框化流式信息流排版，支持多日期折叠、来源归因与事件详情展示。 |
| **规则配置驱动** | PyYAML | 以 `source_registry.yaml` 为核心抓取清单，声明式管理信源分级、路由和抓取参数。 |

## 启动 RSSHub

RSSHub 用于抓取 X 等 RSSHub 来源。默认读取同级目录 `../RSSHub`，也可以用 `RSSHUB_DIR` 指定仓库位置；端口、Token 和代理从项目根目录的 `.env` 读取。

```bash
bash scripts/start_rsshub.sh
```

## 数据抓取与筛选流程

```text
source_registry.yaml 信息源
→ 抓取并标准化
→ Stage A 时间准入与初筛
→ Stage B1 摘要、分类、评分与准入
→ Stage C 事件聚合、去重与事实核验
→ Stage D 最终复审与排序
→ 生成待审核日报
```

```text
Fetch（采集）
  原始条目入库
        │
        ▼
Stage A  screen     条目级 · 便宜 · 硬拒
  时间窗 + AI 初筛
  只留 pass / uncertain
        │
        ▼
Stage B  analyze    条目级 · 分析 + 本地门槛
  摘要 / 关键词 / 实体 / 五维分
  门槛：score≥60 且 audience_relevance≥60
  → active 准入投影（交给 C）
        │
        ▼
Stage C  cluster    事件级 · 聚合 Agent
  合并同事件、对近三期历史、核事实
  publishability: candidate / needs_review / rejected
  → 11 字段事件包
        │
        ▼
Stage D  stage_d    事件级 · 终审编辑
  值不值得报 + 有序子集
  max_selected=30，soft_target=22
        │
        ▼
Export
  按 D 顺序出日报
```

- 抓取：读取 `app/config/source_registry.yaml` 中启用的 Feed、RSSHub 和 GitHub 来源，标准化后写入当日 `draft.db`。
- Stage A：执行时间范围检查和硬性初筛。
- Stage B1：生成摘要、主题、关键词和评分；分数和 AI 主体相关度低于 60 的条目停止，其余全部交给 Stage C。
- Stage C：把相关资讯聚合为事件，结合近期日报去重，并核验争议事实。
- Stage D：从候选事件中选择最终有序子集。

筛选责任（简要）：

| 阶段 | 粒度 | 做什么 |
|---|---|---|
| A | 条目 | 时间窗 + 便宜拒噪（无关/广告/空壳等），`pass`/`uncertain` 进 B |
| B1 | 条目 | 摘要/关键词/实体/评分；`score≥60` 且 `audience_relevance≥60` 准入 C（无配额裁剪） |
| C | 事件 | 同事件聚合、近三期历史去重、事实核验 → `candidate`/`needs_review`/`rejected`；向 D 只交 11 字段事件包 |
| D | 事件 | 终审选有序子集（硬顶 30、软目标 22），不重写 C 的标题/摘要/聚合 |

运行当天的完整抓取与筛选：

```bash
PYTHON=./.venv/bin/python
EDITION_DATE=$(TZ=Asia/Shanghai date +%F)

$PYTHON -m app.main pipeline run \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

筛选结果位于 `output/intel/draft/YYYY-MM-DD/`。检查各阶段和 draft 状态：

```bash
$PYTHON -m app.main pipeline status \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

确认筛选结果后正式发布：

```bash
$PYTHON -m app.main pipeline export \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

## 启动 Web UI

```bash
./.venv/bin/python -m uvicorn app.web.app:app \
  --host 127.0.0.1 \
  --port 8000
```

浏览器访问：<http://127.0.0.1:8000>

## 分阶段运行与断点续跑

基于已有抓取结果分步骤运行 Stage A-D：

```bash
PYTHON=./.venv/bin/python
EDITION_DATE=$(TZ=Asia/Shanghai date +%F)

$PYTHON -m app.main pipeline stage-a \
  --edition-date "$EDITION_DATE" \
  --force && \
$PYTHON -m app.main pipeline stage-b1 \
  --edition-date "$EDITION_DATE" \
  --force && \
$PYTHON -m app.main pipeline stage-c \
  --edition-date "$EDITION_DATE" \
  --force && \
$PYTHON -m app.main pipeline stage-d \
  --edition-date "$EDITION_DATE" \
  --force
```

若流水线中断，使用 `resume` 从断点继续。它会跳过已成功阶段，按顺序运行未完成或可重试阶段，并在完成后生成审核稿：

```bash
$PYTHON -m app.main pipeline resume \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```
