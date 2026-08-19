# AI 情报抓取与处理

本项目支持一条可重复执行、可恢复的 AI-only 文字情报链路：

```text
source registry
→ fetch（抓取、解析、标准化、去重、来源健康记录）
→ Stage A screen（确定性初筛与轻量 AI 筛选）
→ Stage B analyze（结构化分析、实体与评分）
→ Stage C cluster（固定 reference time 的事件聚类）
→ Stage D（日报主编选择、故事簇与展示标题）
→ export（成功后发布为该日期唯一日报）
→ UI（首页、搜索、全部动态只读展示）
```

AI review 对每个候选条目执行一次结构化分析，输出 `keep`、来源类型 `content_class`、编辑主题 `topic_category`、中文摘要、理由、风险标记和置信度。AI 结果是编辑分析输出，不是来源背书；完整 pipeline 会继续由 Stage C 判断真实事件身份，并由独立的 Stage D 编辑 skill 选择当天的日报组合。

`content_class` 描述来源/信号类型（官方发布、媒体报道、项目/工具、社区线索），`topic_category` 描述内容主题（模型、产品、行业、论文、教程、观点）。两者分开保存，UI 和导出会同时展示，避免把第三方媒体报道误读成“官方产品发布”。

## 内容类别与来源归因

| `content_class` | 典型来源 | 处理方式 |
| --- | --- | --- |
| `official_model_company` | 官方模型、公司产品、API 和研究更新 | 按来源身份、时间窗口和关键词筛选，再交给 AI 分类和摘要 |
| `news_media` | 科技媒体、垂直 AI 新闻和分析博客 | 保持来源归因，按较短时效窗口和来源级关键词筛选，不标注为官方发布 |
| `project_tool` | GitHub、Product Hunt、AI 工具项目 | 按项目指标和时间窗口筛选；GitHub 项目可生成一次项目摘要 |
| `community_social` | X、Reddit、RSSHub、论坛 | 作为社区线索参与 AI 分类；输出保留来源归因和风险标记 |

默认主题分类由 `AI_REVIEW_CATEGORIES` 控制：`模型`、`产品`、`行业`、`论文`、`教程`、`观点`。主题分类与来源类型是两个独立字段；如需更细粒度（例如“安全与治理”“开源项目”），可直接在 `.env` 中替换这组标签。

导出和 UI 保留 `source_id`、`source_name`、`source_group`、`source_subtype`、`source_role`、`transport`、`tier` 与 `x_official` 等来源字段。X 官方账号可通过 `source_group=x_official`、`source_role=official` 和 `x_official=true` 归因；这些字段只描述来源身份。

## 抓取来源

来源配置位于 `app/config/source_registry.yaml`。唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`；Feed 细节在 `feed` 下，GitHub 细节在 `github` 下。当前保留原生 RSS/Atom、RSSHub、GitHub Trending/Search/Releases 和 Product Hunt Atom 采集器。

当前 registry 在配置 `RSSHUB_BASE_URL` 后有 83 个启用来源（具体数量以 YAML 为准）：

| `transport` | 当前数量 | 主要来源组 | 抓取方式与内容 |
| --- | ---: | --- | --- |
| `feed` | 31 | `official_blog`、`official_research`、`tech_media`、`hacker_news`、`producthunt`、`linux_do`、`reddit_fixed` | HTTP 获取 RSS/Atom，统一解析为条目；包括官方博客/研究、AI 垂直媒体、IT之家、Hacker News、Product Hunt、LINUX DO 和 LocalLLaMA Reddit Feed。个别公开源可在 registry 中显式 `bypass_proxy`，避免依赖进程级 `NO_PROXY`。 |
| `github` | 11 | `github_trending`、`github_search`、`github_release` | 使用 GitHub API 或 Trending 页面抓取项目、Topic 搜索和 Release；保留 stars、forks、topics、Trending 周期等项目指标。 |
| `rsshub` | 41 | 31 个 `x_official`、`x_social`、`x_search`，以及 Anthropic RSSHub 路由 | 访问本地 RSSHub 输出的 RSS/Atom；X 官方账号保留 `x_official=true` 等来源归因，不绕过 AI 筛选。 |

如果没有配置 `RSSHUB_BASE_URL`，41 个 RSSHub 模板会被安全跳过，CLI 会显示 `Registry skipped`；这不会阻断其它 Feed 和 GitHub 来源。单个来源失败也只记录在 `fetch_attempts` 和来源健康状态中，不会中断整个批次。

本地 RSSHub 的 X 认证路径只有 `TWITTER_AUTH_TOKEN`。`scripts/start_rsshub.sh` 会保留该 token 和 `PROXY_URI`，并显式移除 OAuth 与第三方 X API 变量；脚本在默认 Node 不受支持时会优先尝试 NVM Node 24，再回退到 Node 22。

## 数据模型

日报以 `edition_date`（`YYYY-MM-DD`，Asia/Shanghai）作为唯一业务标识。正式日报每次都会创建一个仅内部使用的临时 build；成功发布后会物理删除该 build 的原始资讯、A/B/C/D 结果、事件、阶段任务和 provider 尝试，只保留最终日报。

- `daily_editions`：每个日期一份最终日报的状态和发布时间。
- `daily_edition_report_entries`：最终入选事件、来源归因和展示字段；历史 UI 与跨日报去重只读取这里。
- `intel_runs`：临时 build 的内部外键，CLI、UI、导出和日报文件都不展示它。
- `intel_items`、AI 投影、事件、阶段任务：仅在当前 draft 存在；成功发布后删除。
- `sources`：来源配置与健康状态，会保留以供下一次抓取。

旧数据库启动时会迁移每个过去日期最后一份可发布日报到日期级最终日报，然后清理旧原始/中间数据。

## 安装与配置

```bash
uv sync --extra test
# 或
python -m pip install -e ".[test]"
```

复制 `.env.example` 为 `.env`，至少配置：

```env
DATABASE_URL=sqlite:///./data/ai_tool_intel.db
```

常用配置：

```env
RSSHUB_BASE_URL=http://127.0.0.1:1200
RSSHUB_PORT=1200
TWITTER_AUTH_TOKEN=
PROXY_URI=http://127.0.0.1:2080
GITHUB_TOKEN=
AI_REVIEW_API_URL=
AI_REVIEW_API_KEY=
AI_REVIEW_MODEL=
AI_REVIEW_API_STYLE=generic_json
AI_REVIEW_TIMEOUT_SECONDS=30
AI_REVIEW_CONCURRENCY=4
AI_REVIEW_CATEGORIES=模型,产品,行业,论文,教程,观点
AI_REVIEW_CATEGORY_MODE=ai
AI_STAGE_D_API_URL=
AI_STAGE_D_API_KEY=
AI_STAGE_D_MODEL=
AI_STAGE_D_API_STYLE=generic_json
AI_STAGE_D_TIMEOUT_SECONDS=120
AI_STAGE_D_RETRIES=2
```

`AI_REVIEW_CONCURRENCY` 取值为 `1..4`，默认 `4`，表示 Stage A/B provider 请求的并发上限。

真实 token、API key、Cookie 和代理地址只放在本地 `.env`，不要写入 README 或提交到 Git。Product Hunt 使用公开 Atom feed，不需要额外 token。

## CLI

`fetch` / `fetch-only` 是诊断命令，可用 `--source`、`--class` 缩小范围；它们不会替换正式日报。正式日报命令始终使用全部当前启用来源：

```bash
python -m app.main fetch [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main fetch-only [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main ai-review [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main export [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N]
python -m app.main run-once [--limit N] [--force] [--edition-date YYYY-MM-DD]
python -m app.main source-health [--source SOURCE_ID]

# 正式的日期级可恢复链路
python -m app.main pipeline run [--limit N] [--force] [--output-dir DIR] [--edition-date YYYY-MM-DD]
python -m app.main pipeline start [--limit N] [--force] [--edition-date YYYY-MM-DD]
python -m app.main pipeline stage-a --edition-date YYYY-MM-DD
python -m app.main pipeline stage-b --edition-date YYYY-MM-DD
python -m app.main pipeline stage-c --edition-date YYYY-MM-DD
python -m app.main pipeline stage-d --edition-date YYYY-MM-DD
python -m app.main pipeline export --edition-date YYYY-MM-DD
python -m app.main pipeline status --edition-date YYYY-MM-DD
```

## 完整执行指令

以下命令在 WSL 项目根目录执行。`Settings.from_env()` 会自动读取当前目录的 `.env`；`--force` 只用于忽略来源冷却时间，日常定时运行可以去掉。

```bash
cd /mnt/d/ai_code/ai_vibecode/aitool_scraping
PYTHON=.venv/bin/python

# 首次安装后，或明确删除旧库后，按当前 ORM 创建新数据库
$PYTHON scripts/init_db.py

# 使用 X/RSSHub 来源时先启动本地 RSSHub；不使用 RSSHub 可跳过
bash scripts/start_rsshub.sh

# 查看来源健康状态和最近一次抓取结果
$PYTHON -m app.main source-health

# 兼容入口：与 pipeline run 使用同一套日期级全量重建规则
$PYTHON -m app.main run-once \
  --limit 20 \
  --force \
  --output-dir output/intel

# 推荐：一次完成正式可恢复链路。对外唯一标识是日报日期。
$PYTHON -m app.main pipeline run \
  --limit 20 \
  --force \
  --output-dir output/intel

# 诊断或运维恢复时，仍用日报日期定位；start 创建一个新的完整 draft。
# 同一天再次 pipeline run 会删除此前 draft，重新抓取全部启用来源；只有成功后才整体替换该日期日报。
$PYTHON -m app.main pipeline start --limit 20 --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-a --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-b --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-c --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-d --edition-date 2026-08-18
$PYTHON -m app.main pipeline export --edition-date 2026-08-18

# 只抓取并检查原始/标准化结果，不调用 AI；这是诊断命令，不会创建正式 pipeline run
$PYTHON -m app.main fetch-only \
  --source x_account_openai \
  --limit 5 \
  --force \
  --output-dir output/fetch

# Stage B 失败后的安全恢复：只重试 Stage B，不会重新调用 Stage A
$PYTHON -m app.main pipeline retry --edition-date 2026-08-18 --stage stage-b
# 或按依赖顺序恢复所有当前可执行的下游阶段（默认不 fetch）
$PYTHON -m app.main pipeline resume --edition-date 2026-08-18
```

单个来源或来源类别只用于诊断，不会替换正式日报：

```bash
# 单个 X 官方账号（RSSHub 必须已启动且 RSSHUB_BASE_URL 已配置）
$PYTHON -m app.main fetch-only \
  --source x_account_openai \
  --limit 5 \
  --force \
  --output-dir output/fetch-openai

# 只处理官方模型/公司来源
$PYTHON -m app.main fetch-only \
  --class official_model_company \
  --limit 20 \
  --force \
  --output-dir output/fetch-official

# 查询单个来源的健康状态
$PYTHON -m app.main source-health --source x_account_openai
```

各阶段的职责和门禁如下：

1. 正式日报的 `fetch` 从 registry 载入全部当前启用来源，强制完整请求（不使用 HTTP 304 条件请求），把本次响应视为当天完整集合；每个条目只在当前临时 build 内去重。
2. `ai-review` 先执行来源级确定性初筛，再对保留条目调用结构化 AI。AI 返回 `keep`、`content_class`、`topic_category`、`summary_cn`、理由、风险标记和置信度；无关内容 `keep=false`，AI 失败或尚未处理的内容保留在数据库的阶段审计中，但不进入最终导出。主题列表由 `AI_REVIEW_CATEGORIES` 配置，`AI_REVIEW_CATEGORY_MODE=source` 可在模型不可用时使用来源规则回退。
3. `stage-c` 只处理真实世界事件身份：URL/external ID 是精确锚点，标题只是弱候选信号；摘要、实体和关键词用于候选召回，模糊组交由窄域 AI resolver 分区，失败时保守拆分。
4. `stage-d` 只处理日报编辑：先应用论文证据硬门槛，再由独立编辑 skill 对本轮 Stage C canonical events 做全局选择、故事簇归并、展示顺序和中文展示标题。它不使用 topic/source/content/repeat 的本地配额，也不要求凑满 30 条；社区线索可被选中，但展示层会强制标注待核实。
5. `export` 先写入临时目录；只有所有来源和 AI 阶段完整成功时，才原子替换 `output/daily/YYYY-MM-DD/`，写入最终日报条目并删除临时 build。失败或 `partial` 不会触碰旧日报。
6. 同日重新抓取不合并上午结果：下午响应中不存在的资讯、被移除来源的资讯及其派生事件，会在下午成功发布后从当天日报与数据库临时数据中消失。历史日期只保留其最终日报。
7. UI（`/`、`/search`、`/all`、`/github`）只读日期级最终日报或已生成报告，不在请求中执行抓取或 AI；首页、搜索和“本期精选”默认只展示当前日期的最终入选事件。

启动本地 UI：

```bash
$PYTHON -m uvicorn app.web.app:app --host 127.0.0.1 --port 8000
```

`run-once` 是 `pipeline run` 的兼容 facade，两者使用相同的日期级全量重建和成功后替换规则。`fetch-only` 只抓取并输出标准化条目及来源归因，不调用 AI，也不代表正式日报。

Stage A/B 对每条 AI provider 任务执行瞬态错误自动重试：首次调用失败后最多再重试 5 次（最多 6 次 provider 调用）。429、5xx、timeout 和 rate-limit 属于可重试错误；永久性 4xx、鉴权失败和 schema 错误不会重复请求。达到上限后任务记录 `provider_retry_exhausted` 并转为终结失败，成功任务仍会继续进入下游阶段，整次 run 以 `partial` 保留审计状态。

默认数量策略为：每个来源抓取 20 条，Stage A/B 处理当前完整 build，日报默认导出 30 条。`run-once --limit`（或 `--fetch-limit`）只控制每来源抓取量；Stage D 始终对本轮通过论文门槛的完整事件池做编辑选择；导出阶段的显式 `--limit` 可以覆盖默认日报数量。

- `ai_review_candidates.jsonl`：AI 选择且分析成功的候选。
- `ai_review_audit.jsonl`：过滤、拒绝和 AI 失败等审计记录。
- `ai_review_digest.md`：候选的中文摘要和来源信息。

`export` 的日报产物默认写入 `output/daily/YYYY-MM-DD/`：

- `intel_items.jsonl`：AI 选择结果与来源归因。
- `intel_digest.md`：分类、状态、指标、风险和链接摘要。
- `manifest.json`：日报日期、公开状态、完整筛选漏斗、阶段状态/失败原因和文件校验信息；不包含内部执行 ID 或快照键。

为兼容既有脚本，完整成功的日报仍会同步最新副本到 `output/intel/`；UI 的主数据源是日期级最终日报，而不是临时 build。

GitHub 项目保留抓取到的 stars、forks、Trending 周期指标、topics 和 README 摘要；AI 不替代项目指标筛选，也不会生成未提供的增长数据。

本地 RSSHub 启动：

```bash
bash scripts/start_rsshub.sh
```

## Web UI

FastAPI/Jinja UI 只读取数据库和已生成报告，展示主题分类、来源归因、AI 摘要、风险和选择状态；首页按主题分组，卡片同时显示来源名称、来源组、transport、tier 和原文链接，并提供“来源目录”页。请求过程中不会运行 collector、AI、搜索或其他处理任务。

## 验证

```bash
TMPDIR=/tmp python -m pytest -q
python -m compileall -q app scripts
python -m app.main --help
```
