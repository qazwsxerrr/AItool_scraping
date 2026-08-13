# AI 情报抓取与处理

本项目当前实现数据抓取、确定性处理和结果展示。旧版链路仍保持兼容：

```text
source registry -> fetch -> process -> export
```

V3 日报链路以事件（`events`）为选入单元，按以下顺序运行：

```text
source registry -> fetch + source health -> enrich -> triage
-> cluster -> compose -> publication gates -> daily export
```

`intel_items` 是来源信号；`documents` 保存最小内容快照；`events` 汇聚同一事件的多个来源；`daily_editions` 和 `daily_event_entries` 保存每日选稿、门禁结果和渲染快照。各阶段均可单独运行，重复执行使用幂等 upsert，不需要网页请求触发。

## 内容分流

| content_class | 适用内容 | 确定性筛选 | 处理语义 |
| --- | --- | --- | --- |
| `official_model_company` | 官方模型、模型卡、公司产品和 API 更新 | 最近 30 天 + 发布/模型/API/版本/价格关键词 | 一个官方直链成功才是 `verified`，否则 `needs_review` |
| `project_tool` | GitHub Trending/Search、Product Hunt、AI 工具 | GitHub Trending 使用 daily/weekly 周期 Star；Search 使用最近 7 天 push 且 `stars > 100`；Product Hunt 按时间和热度 | metadata 驱动的 `hotspot`，不要求第三方证据 |
| `community_social` | X、Reddit、RSSHub、论坛讨论 | 最近 7 天 + 关键词或互动信号 | `discovery_only`，不能单独形成高可信结论 |

来源配置位于 `app/config/source_registry.yaml`，唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`。Feed 细节放在 `feed.format`（`rss`/`atom`）和 `feed.adapter`（`generic`/`producthunt`）中，GitHub 细节放在 `github.mode`（`search`/`releases`/`trending`）及其选项中；不再使用 `type`、`collector_type` 或 `parser_type`。

## 数据抓取途径

当前 registry 共 67 条来源定义（63 条启用，4 条停用），实现方式如下：

| transport | 具体途径 | 实现方式 | 典型来源 |
| --- | --- | --- | --- |
| `feed` | 原生 RSS | 共享 HTTP 客户端获取 RSS，通用 feed parser 标准化 | OpenAI、Google Research、OpenRouter、NVIDIA、AWS、Hugging Face、LINUX DO |
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
| 官方博客 | 6 | `openai_news`、`google_deepmind_blog`、`huggingface_blog`、`openrouter_blog`、`nvidia_ai_blog`、`aws_machine_learning_blog`，原生 RSS |
| 官方研究 | 1 | `google_research_blog`，原生 RSS |
| Product Hunt | 1 | `producthunt_feed`，Atom + `producthunt` adapter |
| LINUX DO | 2 | `linux_do_top`、`linux_do_hot`，原生 RSS |
| Reddit LocalLLaMA | 14 | new/hot/top 及 9 个主题搜索，Atom |
| RSSHub X | 26 | 21 个账号路由 + 5 个搜索路由，RSSHub 模板 URL |
| RSSHub Anthropic | 3 | news/research/engineering 路由，RSSHub 模板 URL |
| GitHub | 10 | Trending daily/weekly、6 个 topic Search、Ollama/Transformers Releases |

合计 63 条启用来源；另有 4 条 registry 定义默认停用。RSSHub 组在未设置
`RSSHUB_BASE_URL` 时按条目跳过，不影响其他 34 条非模板来源。

## 代码边界

```text
app/
├─ config/                 # Settings 和 source registry
├─ collectors/             # 按 transport 拆分的 Feed/RSSHub/GitHub 请求与字段映射
├─ parsers/                # feed 解析
├─ domain/                 # DTO、来源策略、确定性筛选、轻量核实
├─ ai/                     # 一条目一次结构化 AI 分析
├─ storage/                # v2 ORM、Repository、数据库和 UI 只读查询
├─ jobs/                   # v2 intel 与 V3 daily 阶段编排
├─ github/report.py        # 按日期生成 GitHub Trending Markdown 报告
├─ storage/github_reader.py # 从标准 intel_items.jsonl 读取 GitHub metadata
├─ pipeline/normalize.py   # 无数据库依赖的基础文本/URL 标准化工具
└─ web/                    # 现有 FastAPI/Jinja UI，当前阶段不改
```

旧 claim、evidence、recommendation 阶段和对应客户端、表模型、脚本已经移除。数据库只由当前 ORM metadata 初始化；本阶段不提供迁移框架，也不对旧数据库做兼容迁移。若本地数据库来自旧版本，请先备份 SQLite 文件，再在停机窗口删除旧文件并运行 `python -m app.main fetch-only` 或 `python -m app.main run-daily`，由 `init_db()` 重建完整 schema。实现和测试期间使用临时隔离 SQLite 路径，不会删除仓库当前 `data/` 数据库。

## 数据表

- `sources`：来源、调度间隔、内容类别和 JSON 策略。
- `fetch_attempts`：每次请求的状态、HTTP 状态、传输方式、重试、字节数和错误。
- `intel_runs`：一次 `run-once` 或单阶段执行的汇总状态。
- `intel_items`：统一条目、canonical URL、指标、原始 payload、内容 hash 和选择状态。
- `ai_item_reviews`：每条最多一条结构化模型结果，保留原始响应。
- `item_verifications`：官方直链、项目 metadata 或社区 discovery 的轻量结果。
- `documents`、`triage_reviews`：V3 最小文档快照、确定性预筛和结构化 triage 原始响应。
- `events`、`event_evidence`、`cluster_decisions`：事件身份、来源证据关系和 exact/fuzzy 聚类判断。
- `event_editorial_reviews`、`daily_editions`、`daily_event_entries`：带证据引用的事件文案、发布门禁和每日顺序快照。

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
HTTP_PROXY=http://127.0.0.1:2080
HTTPS_PROXY=http://127.0.0.1:2080
NO_PROXY=127.0.0.1,localhost,::1
RSSHUB_BASE_URL=http://127.0.0.1:1200
RSSHUB_PORT=1200
PROXY_URI=http://127.0.0.1:2080
GITHUB_TOKEN=your-github-token
AI_REVIEW_API_URL=https://api.deepseek.com
AI_REVIEW_API_KEY=your-key
AI_REVIEW_MODEL=deepseek-v4-flash
AI_REVIEW_API_STYLE=openai_chat
AI_REVIEW_TIMEOUT_SECONDS=30
```

### `.env` 参数说明

项目只读取根目录的 `.env`；请先复制 `.env.example`，再填入本机配置。
`.env` 已被 Git 忽略，真实 token、API key 和代理地址不要写入 README 或提交到仓库。

| 参数 | 必填 | 默认值/示例 | 用途 |
| --- | --- | --- | --- |
| `DATABASE_URL` | 否 | `sqlite:///./data/ai_tool_intel.db` | SQLite 或其他 SQLAlchemy 数据库连接串。 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `20` | Python 外部请求超时时间，单位为秒。 |
| `REQUEST_RETRIES` | 否 | `2` | 单个来源的重试次数。 |
| `USER_AGENT` | 否 | `AItool_scraping/0.1 (+https://example.local)` | Python HTTP 请求的 User-Agent。 |
| `HTTP_PROXY` | 否 | 例如 `http://127.0.0.1:2080` | Reddit、LINUX DO、GitHub 等外部 Python 请求使用的 HTTP 代理。 |
| `HTTPS_PROXY` | 否 | 例如 `http://127.0.0.1:2080` | 外部 HTTPS 请求使用的代理；通常与 `HTTP_PROXY` 保持一致。 |
| `ALL_PROXY` | 否 | 留空 | httpx 可用的全协议代理兜底配置。 |
| `NO_PROXY` | 否 | `127.0.0.1,localhost,::1` | 不走代理的地址列表；应包含本地 RSSHub 地址。 |
| `RSSHUB_BASE_URL` | X/RSSHub 必填 | `http://127.0.0.1:1200` | Python 访问 RSSHub 的基础地址；为空时跳过 RSSHub 模板来源。 |
| `RSSHUB_PORT` | 启动本地 RSSHub 时必填 | `1200` | `scripts/start_rsshub.sh` 传给 Node RSSHub 的监听端口，必须与 `RSSHUB_BASE_URL` 的端口一致。 |
| `PROXY_URI` | X 使用外部代理时填写 | 例如 `http://127.0.0.1:2080` | RSSHub Node 进程访问 X 等外部服务时使用的代理；留空表示 RSSHub 直连。 |
| `TWITTER_AUTH_TOKEN` | X 推荐 | 留空 | RSSHub 的 Web API 认证 token；配置后优先走 RSSHub Web API 路径。 |
| `TWITTER_CONSUMER_KEY` | OAuth 备用 | 留空 | X OAuth 1.0a consumer key。 |
| `TWITTER_CONSUMER_SECRET` | OAuth 备用 | 留空 | X OAuth 1.0a consumer secret。 |
| `TWITTER_ACCESS_TOKEN` | OAuth 备用 | 留空 | X OAuth 1.0a access token。 |
| `TWITTER_ACCESS_SECRET` | OAuth 备用 | 留空 | X OAuth 1.0a access secret。四个 OAuth 参数需要成组配置。 |
| `GITHUB_API_BASE_URL` | 否 | `https://api.github.com` | GitHub API 地址。 |
| `GITHUB_TOKEN` | GitHub API 推荐 | 留空 | GitHub API token。 |
| `GITHUB_API_TOKEN` | 否 | 留空 | `GITHUB_TOKEN` 为空时使用的兼容别名。 |
| `GITHUB_API_VERSION` | 否 | `2022-11-28` | GitHub API 版本请求头。 |
| `GITHUB_TIMEOUT_SECONDS` | 否 | `20` | GitHub API 请求超时时间，单位为秒。 |
| `AI_REVIEW_API_URL` | 否 | 留空 | AI review 服务地址；为空时不调用 AI review。 |
| `AI_REVIEW_API_KEY` | 启用 AI review 时必填 | 留空 | AI review 服务密钥。 |
| `AI_REVIEW_MODEL` | 启用 AI review 时填写 | 留空 | AI review 使用的模型名。 |
| `AI_REVIEW_API_STYLE` | 否 | `generic_json` | AI review API 协议风格，例如 `openai_chat`。 |
| `AI_REVIEW_TIMEOUT_SECONDS` | 否 | `30` | AI review 请求超时时间，单位为秒。 |

代理和端口的关系固定如下：

- Reddit、LINUX DO、GitHub 等外部来源由 Python 使用 `HTTP_PROXY`/`HTTPS_PROXY`；
  Reddit 默认不追加 `raw_json=1`。
- RSSHub/X 的 Python 请求直接访问 `RSSHUB_BASE_URL`，不继承外部 Python 代理；
  RSSHub Node 进程自身使用 `PROXY_URI` 访问 X。
- 本地启动时只执行 `bash scripts/start_rsshub.sh`。脚本只读取根 `.env` 的
  `RSSHUB_PORT` 和 `PROXY_URI`，不读取其他目录的 `.env`，也不使用 Docker。

未配置 `RSSHUB_BASE_URL` 时，依赖模板 URL 的来源会被跳过并记录原因。Product Hunt 始终使用公开 Atom feed，不读取或发送 API token。

本地 RSSHub 当前需要 Node.js 22.22.2+ 或 24.15.0+。首次运行前在
`../RSSHub` 执行 `corepack pnpm install --frozen-lockfile && corepack pnpm build`。

## 运行

逐阶段运行：

```bash
python -m app.main fetch --class project_tool
python -m app.main process --class project_tool --limit 100
python -m app.main export --output-dir output/intel
```

第一阶段抓取与导出可单独运行，不会调用 process、AI、evidence、triage、cluster、compose 或推荐阶段：

```bash
python -m app.main fetch-only --source openai_news --force --output-dir output/fetch
```

`fetch-only` 写出 `fetch_items.json`、`fetch_items.jsonl` 和 `fetch_items.md`。每条记录都包含
`source_id`、来源名称、`transport`、`source_group`、`source_subtype`、`tier`、`role`；X 官方账号的
`x_official` 为 `true`，`x_social`/`x_search` 保持发现性质并标为 `false`。抓取失败按来源隔离，单一来源失败不会中断批次。

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

### V3 日报命令

```bash
python -m app.main source-health [--source SOURCE_ID]
python -m app.main enrich [--source SOURCE_ID] [--limit N] [--force]
python -m app.main triage [--source SOURCE_ID] [--limit N] [--force]
python -m app.main cluster [--limit N] [--force]
python -m app.main compose [--limit N] [--force]
python -m app.main daily-export [--date YYYY-MM-DD] [--output-dir output/daily] [--force]
python -m app.main run-daily [--date YYYY-MM-DD] [--limit N] [--force]
```

`source-health` 只读输出来源状态、连续失败次数、错误码和下一次可抓取时间。`fetch` 对 RSS/Atom 来源会发送已保存的 `ETag` / `Last-Modified` 条件请求；HTTP 304 视为成功并推进冷却，403/429 等失败按来源隔离并指数退避。

来源治理字段位于 `app/config/source_registry.yaml`，包括 `tier`、`topic_scopes`、`primary_eligible` 和 `citation_policy`。每日配额、窗口、来源组上限和各 section 目标位于 `app/config/daily_profile.yaml`；修改后由严格模型校验，GitHub 还有跨子组 aggregate cap。

本地 RSSHub 启动入口：

```bash
bash scripts/start_rsshub.sh
```

数据脚本入口：

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

V3 `daily-export` 默认写入：

- 发布日报：`output/daily/YYYY/MM/YYYY-MM-DD.md`
- 发布审计：`output/daily/YYYY/MM/YYYY-MM-DD.events.jsonl`
- 门禁未通过：`output/daily/YYYY/MM/YYYY-MM-DD.draft.md`
- 阻塞/待处理审计：`output/daily/YYYY/MM/YYYY-MM-DD.pending.jsonl`

同一天已有 `published` edition 时，默认不会覆盖；只有传入 `--force` 才会重算。低信息日会保留明确的 machine-readable gate failures，不会用社交或噪声条目填充缺口。

## 验证

```bash
RSSHUB_BASE_URL= uv run --extra test pytest -q
uv run python -m compileall -q app scripts
uv run python -m app.main --help
```

UI 只读取数据库和已生成的 `intel_items.jsonl`；本阶段不在请求中执行 collector、AI 或核实任务。
