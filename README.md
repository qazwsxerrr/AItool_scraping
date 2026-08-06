# AI 情报抓取与处理

本项目当前实现数据抓取、确定性处理和结果展示。唯一的数据处理链路是：

```text
source registry -> fetch -> process -> export
```

## 内容分流

| content_class | 适用内容 | 确定性筛选 | 处理语义 |
| --- | --- | --- | --- |
| `official_model_company` | 官方模型、模型卡、公司产品和 API 更新 | 最近 30 天 + 发布/模型/API/版本/价格关键词 | 一个官方直链成功才是 `verified`，否则 `needs_review` |
| `project_tool` | GitHub Trending/Search、Product Hunt、AI 工具 | GitHub Trending 使用 daily/weekly 周期 Star；Search 使用最近 7 天 push 且 `stars > 100`；Product Hunt 按时间和热度 | metadata 驱动的 `hotspot`，不要求第三方证据 |
| `community_social` | X、Reddit、RSSHub、论坛讨论 | 最近 7 天 + 关键词或互动信号 | `discovery_only`，不能单独形成高可信结论 |

来源配置位于 `app/config/source_registry.yaml`。每个来源可以声明 `content_class`、`collector_type`、`selection_policy` 和 `verification_policy`。

## 代码边界

```text
app/
├─ config/                 # Settings 和 source registry
├─ collectors/             # RSS/Atom/RSSHub/GitHub API/Trending/Product Hunt 请求与字段映射
├─ parsers/                # feed 解析
├─ domain/                 # DTO、来源策略、确定性筛选、轻量核实
├─ ai/                     # 一条目一次结构化 AI 分析
├─ storage/                # v2 ORM、Repository、数据库和 UI 只读查询
├─ jobs/                   # intel fetch/process/export/run 编排
├─ github/report.py        # 按日期生成 GitHub Trending Markdown 报告
├─ storage/github_reader.py # 从标准 intel_items.jsonl 读取 GitHub metadata
├─ pipeline/normalize.py   # 无数据库依赖的基础文本/URL 标准化工具
└─ web/                    # 现有 FastAPI/Jinja UI，当前阶段不改
```

旧 claim、evidence、recommendation 阶段和对应客户端、表模型、脚本已经移除。数据库只由 v2 schema 初始化；历史数据库不迁移，删除后重新运行即可创建新 schema。

## 数据表

- `sources`：来源、调度间隔、内容类别和 JSON 策略。
- `fetch_attempts`：每次请求的状态、HTTP 状态、传输方式、重试、字节数和错误。
- `intel_runs`：一次 `run-once` 或单阶段执行的汇总状态。
- `intel_items`：统一条目、canonical URL、指标、原始 payload、内容 hash 和选择状态。
- `ai_item_reviews`：每条最多一条结构化模型结果，保留原始响应。
- `item_verifications`：官方直链、项目 metadata 或社区 discovery 的轻量结果。

GitHub 项目使用稳定的 `github_repo:*` 标识或 canonical URL 去重；指标保存在 `metrics_json`，包括累计 stars、周期新增 Star、forks、pushed_at、Trending rank 和 Search topic。当前只保留最新合并指标，不建立历史 Star 快照表。

## 安装

```bash
uv sync --extra test
```

也可以使用 Python 3.12 的虚拟环境安装项目依赖：

```bash
python -m pip install -e ".[test]"
```

## 配置

复制 `.env.example` 为 `.env`。最小配置：

```env
DATABASE_URL=sqlite:///./data/ai_tool_intel.db
```

常用可选配置：

```env
RSSHUB_BASE_URL=https://rsshub.example.com
GITHUB_TOKEN=your-github-token
PRODUCTHUNT_API_TOKEN=your-producthunt-developer-token
AI_REVIEW_API_URL=https://api.deepseek.com
AI_REVIEW_API_KEY=your-key
AI_REVIEW_MODEL=deepseek-v4-flash
AI_REVIEW_API_STYLE=openai_chat
AI_REVIEW_TIMEOUT_SECONDS=30
```

未配置 `RSSHUB_BASE_URL` 时，依赖模板 URL 的来源会被跳过并记录原因。未配置 Product Hunt token 时使用公开 Atom，并透明标记热度字段不可用。

## 运行

逐阶段运行：

```bash
python -m app.main fetch --class project_tool
python -m app.main process --class project_tool --limit 100
python -m app.main export --output-dir output/intel
```

日常入口：

```bash
python -m app.main run-once --limit 100
```

常用参数：

- `--source SOURCE_ID`：只处理一个来源。
- `--class official_model_company|project_tool|community_social`：只处理一个内容类别。
- `--limit N`：限制当前阶段条目数。
- `--force`：忽略抓取冷却并重新处理已有条目。
- `--dry-run`：使用临时 SQLite 和内存输出，不写目标数据库或输出目录。

脚本入口只保留：

```bash
python scripts/init_db.py
python scripts/run_fetch_once.py
python scripts/run_intel_once.py
```

## 导出

`export` 默认写入 `output/intel/`：

- `intel_items.jsonl`：状态为 `verified`、`hotspot` 或 `discovery_only` 的条目。
- `intel_pending.jsonl`：`needs_review`、`ai_failed` 或尚未分析的条目。
- `intel_digest.md`：分类、状态、指标、风险和链接摘要。
- `output/github-trending/YYYY/MM/YYYYMMDD.md`：GitHub Trending daily/weekly 和 Search API 补充候选报告。

GitHub 项目不执行 AI 评分。Trending HTML 的 `stars today`、`stars this week` 会以原始周期指标写入报告；Search API 只作为最近活跃的候选补充，不生成虚假的周增长。

## 验证

```bash
RSSHUB_BASE_URL= uv run --extra test pytest -q
uv run python -m compileall -q app scripts
uv run python -m app.main --help
```

UI 只读取数据库和已生成的 `intel_items.jsonl`；本阶段不在请求中执行 collector、AI 或核实任务。
