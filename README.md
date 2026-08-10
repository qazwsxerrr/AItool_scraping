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

来源配置位于 `app/config/source_registry.yaml`，唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`。Feed 细节放在 `feed.format`（`rss`/`atom`）和 `feed.adapter`（`generic`/`producthunt`）中，GitHub 细节放在 `github.mode`（`search`/`releases`/`trending`）及其选项中；不再使用 `type`、`collector_type` 或 `parser_type`。

## 数据抓取途径

当前 registry 共 63 条来源定义（59 条启用，4 条停用），实现方式如下：

| transport | 具体途径 | 实现方式 | 典型来源 |
| --- | --- | --- | --- |
| `feed` | 原生 RSS | 共享 HTTP 客户端获取 RSS，通用 feed parser 标准化 | OpenAI、Google DeepMind、Hugging Face、LINUX DO |
| `feed` | 原生 Atom | 同一 feed parser 解析 Atom | Reddit LocalLLaMA |
| `feed` | Product Hunt Atom | Atom feed + `producthunt` adapter；先从 feed 提取，发现 GitHub 链接时可复用 GitHub metadata enrichment | Product Hunt |
| `rsshub` | RSSHub 配置路由 | `${RSSHUB_BASE_URL}` 解析模板 URL，响应仍按 RSS/Atom 解析 | X 账号、X 搜索、Anthropic 路由 |
| `github` | Trending HTML | GitHub Trending daily/weekly 页面解析周期 Star | GitHub Trending Daily/Weekly |
| `github` | REST Search API | `/search/repositories` 按 query、sort、order 和 pushed window 查询 | topic:llm、topic:rag 等 |
| `github` | REST Releases API | 仓库 releases endpoint 读取近期版本 | Ollama、Transformers |

未知 `transport` 或不匹配的嵌套选项会被配置校验拒绝，不会静默降级到 feed。

registry 的来源组清单（启用时）：

| 来源组 | 数量 | 配置/路由范围 |
| --- | ---: | --- |
| 官方博客 | 3 | `openai_news`、`google_deepmind_blog`、`huggingface_blog`，原生 RSS |
| Product Hunt | 1 | `producthunt_feed`，Atom + `producthunt` adapter |
| LINUX DO | 2 | `linux_do_top`、`linux_do_hot`，原生 RSS |
| Reddit LocalLLaMA | 14 | new/hot/top 及 9 个主题搜索，Atom |
| RSSHub X | 26 | 21 个账号路由 + 5 个搜索路由，RSSHub 模板 URL |
| RSSHub Anthropic | 3 | news/research/engineering 路由，RSSHub 模板 URL |
| GitHub | 10 | Trending daily/weekly、6 个 topic Search、Ollama/Transformers Releases |

合计 59 条启用来源；另有 4 条 registry 定义默认停用。RSSHub 组在未设置
`RSSHUB_BASE_URL` 时按条目跳过，不影响其他 30 条非模板来源。

## 代码边界

```text
app/
├─ config/                 # Settings 和 source registry
├─ collectors/             # 按 transport 拆分的 Feed/RSSHub/GitHub 请求与字段映射
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
AI_REVIEW_API_URL=https://api.deepseek.com
AI_REVIEW_API_KEY=your-key
AI_REVIEW_MODEL=deepseek-v4-flash
AI_REVIEW_API_STYLE=openai_chat
AI_REVIEW_TIMEOUT_SECONDS=30
```

未配置 `RSSHUB_BASE_URL` 时，依赖模板 URL 的来源会被跳过并记录原因。Product Hunt 始终使用公开 Atom feed，不读取或发送 API token。

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

GitHub 项目先按累计 Star、Trending daily/weekly 周期 Star、Fork 等已抓取指标做确定性筛选，AI 不参与保留决策。选中的唯一仓库最多执行一次项目摘要调用，生成项目介绍、主要能力、适用场景和风险提示；Trending HTML 的 `stars today`、`stars this week` 仍以原始周期指标写入报告，Search API 不生成虚假的周增长。

## 验证

```bash
RSSHUB_BASE_URL= uv run --extra test pytest -q
uv run python -m compileall -q app scripts
uv run python -m app.main --help
```

UI 只读取数据库和已生成的 `intel_items.jsonl`；本阶段不在请求中执行 collector、AI 或核实任务。
