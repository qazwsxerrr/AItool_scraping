# AI 情报抓取与处理

本项目支持一条可重复执行、可恢复的 AI-only 文字情报链路：

```text
source registry
→ fetch（抓取、解析、标准化、去重、来源健康记录）
→ Stage A screen（确定性初筛与轻量 AI 筛选）
→ Stage B analyze（短摘要、关键词、主题分类与编辑优先级评分）
→ Stage C aggregate（按本地评分门槛输入并一次 AI 调用聚合，同时判断近期重复/更新）
→ Stage D select（仅从 Stage C 候选中选择有序子集）
→ draft 审计工作区（按日期保留完整抓取与 A-D 决策）
→ export / approval（成功后发布为该日期唯一日报）
→ UI（首页、搜索、全部动态只读展示）
```

Stage B1 对每个通过 Stage A 的条目执行一次结构化分析，只输出来源归因、主题分类、中文短摘要、关键词、实体和编辑优先级评分。它不做事件角色路由、事实抽取、论文证据判断、风险标记或自评置信度；完整 pipeline 会继续由 Stage C 一次性聚合新闻主线，并由独立的 Stage D 编辑 skill 选择当天的日报组合。

`content_class` 描述来源/信号类型（官方发布、媒体报道、项目/工具、社区线索），`topic_category` 使用橘鸦日报的六类编辑主题（开发生态、模型发布、产品应用、行业动态、技术与洞察、前瞻与传闻）。两者分开保存，UI 和导出会同时展示，避免把第三方媒体报道误读成“官方产品发布”。

## 内容类别与来源归因

| `content_class` | 典型来源 | 处理方式 |
| --- | --- | --- |
| `official_model_company` | 官方模型、公司产品、API 和研究更新 | 按来源身份、时间窗口和关键词筛选，再交给 AI 分类和摘要 |
| `news_media` | 科技媒体、垂直 AI 新闻和分析博客 | 保持来源归因，按较短时效窗口和来源级关键词筛选，不标注为官方发布 |
| `project_tool` | GitHub Release、Product Hunt、AI 工具项目 | 按项目更新和时间窗口筛选；GitHub 项目可单独生成项目摘要 |
| `community_social` | X、Reddit、RSSHub、论坛 | 作为社区线索参与 AI 分类；输出保留来源归因和风险标记 |

默认主题分类为：`开发生态`、`模型发布`、`产品应用`、`行业动态`、`技术与洞察`、`前瞻与传闻`。主题分类与来源类型是两个独立字段；Stage B1 只输出这六类，不再使用旧的模型、产品、项目、行业、论文、教程、观点分类。

导出和 UI 保留 `source_id`、`source_name`、`source_group`、`source_subtype`、`source_role`、`transport`、`tier` 与 `x_official` 等来源字段。X 官方账号可通过 `source_group=x_official`、`source_role=official` 和 `x_official=true` 归因；这些字段只描述来源身份。

## 抓取来源

来源配置位于 `app/config/source_registry.yaml`。唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`；Feed 细节在 `feed` 下，GitHub 细节在 `github` 下。日报主链路已停用普通 arXiv 聚合源、GitHub Trending 和 GitHub Search 新项目源；当前只保留明确仓库的 GitHub Releases。后续新项目发现可在独立专栏中重新启用，不与日报事件池混跑。

当前 registry 在配置 `RSSHUB_BASE_URL` 后有 79 个启用来源（具体数量以 YAML 为准）：

| `transport` | 当前数量 | 主要来源组 | 抓取方式与内容 |
| --- | ---: | --- | --- |
| `feed` | 33 | `official_blog`、`official_research`、`tech_media`、`hacker_news`、`producthunt`、`linux_do`、`reddit_fixed` | HTTP 获取 RSS/Atom，统一解析为条目；普通 arXiv 聚合源当前禁用。个别公开源可在 registry 中显式 `bypass_proxy`，避免依赖进程级 `NO_PROXY`。 |
| `github` | 3 | `github_release` | 只跟踪 Claude Code、Ollama、Transformers 等明确仓库的 Release；Trending 与 Topic Search 当前禁用。 |
| `rsshub` | 43 | `x_official`、`x_social`，以及 Anthropic RSSHub 路由 | 访问本地 RSSHub 输出的 RSS/Atom；X 官方账号保留 `x_official=true` 等来源归因，不绕过 AI 筛选。 |

如果没有配置 `RSSHUB_BASE_URL`，43 个 RSSHub 模板会被安全跳过，CLI 会显示 `Registry skipped`；这不会阻断其它 Feed 和 GitHub Release 来源。单个来源失败也只记录在 `fetch_attempts` 和来源健康状态中，不会中断整个批次。

本地 RSSHub 的 X 认证路径只有 `TWITTER_AUTH_TOKEN`。`scripts/start_rsshub.sh` 会保留该 token 和 `PROXY_URI`，并显式移除 OAuth 与第三方 X API 变量；脚本在默认 Node 不受支持时会优先尝试 NVM Node 24，再回退到 Node 22。

## 数据分层与审计工作区

日报以 `edition_date`（`YYYY-MM-DD`，Asia/Shanghai）作为唯一业务标识。`run_id` 仍存在于审计 SQLite 内部，用于外键、阶段任务和恢复；它不会出现在 CLI、UI、导出文件或正式日报标识中。

正式数据库（默认 `data/ai_tool_intel.db`）只承载日期级最终日报：

- `daily_editions`：每个日期一份已发布日报的状态和发布时间。
- `daily_edition_report_entries`：最终入选事件、来源归因和展示字段；历史 UI 与跨日报去重只读取这里。

完整抓取、筛选和 provider 审计不写入正式日报库，而是放在与正式库同级的日期工作区：

```text
data/
├── ai_tool_intel.db
└── editions/
    └── 2026-08-18/
        ├── draft.db  # 当前待发布/失败可恢复的完整构建
        └── audit.db  # 最近一次成功发布的完整构建审计
```

`draft.db` 和 `audit.db` 都包含原始资讯、`intel_runs`、A/B 结果、Stage C 事件、Stage D 的候选 ID 与有序入选结果、通用阶段任务、attempt 和 provider 响应。Stage D 不再维护独立快照表，也不保存未选事件记录。`draft.db` 仅有一份；同日期重新 `pipeline start` / `pipeline run` 时会从头替换它。成功 `export` 后，`draft.db` 会整体替换 `audit.db`，因此每个日期默认只保留一份最近成功日报的完整审计，不累计旧版本。

旧正式库中的历史 raw / stage 数据不会在启动时自动迁移、删除或改写。包含旧 `intel_event_stage_d_snapshots` 表的 draft/audit 数据库与当前结构不兼容，启动时会明确报错；需要先备份，再显式删除并重建对应工作区。本项目不保留旧 Stage D 兼容读取或自动迁移代码。

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
AI_REVIEW_CATEGORIES=开发生态,模型发布,产品应用,行业动态,技术与洞察,前瞻与传闻
AI_STAGE_C_INPUT_MIN_SCORE=60
```

`AI_STAGE_C_INPUT_MIN_SCORE` 默认 `60`。Stage C 直接读取完成 Stage B1、通过结构性校验、且经本地 guard 后评分不低于该阈值的条目；这里没有独立的 B2 路由层。Stage C 输出 `candidate_event_ids`，Stage D 只在 `max_selected` 上限内选择其中的有序子集。

Stage B1 的 `b1_priority` 只衡量内容价值，由本地 guard 按 `audience_relevance` 25%、`material_change` 25%、`impact_scope` 20%、`independent_news_value` 20% 和 `specificity` 10% 重算。来源身份、AI 把握度和时间新鲜度不参与该分数：来源归因来自 source registry，72 小时窗口由本地 recency policy 处理。

`AI_REVIEW_CONCURRENCY` 取值为 `1..4`，默认 `4`，表示 Stage A/B provider 请求的并发上限。
Stage D 与 Stage A/B/C 共用 `AI_REVIEW_*` provider 配置；它只使用不同的提示词和输出 schema，不需要额外的模型、API URL 或 API key。由于 Stage D 一次提交完整事件池，实际请求超时下限为 120 秒，但仍不引入独立的 provider 配置。

真实 token、API key、Cookie 和代理地址只放在本地 `.env`，不要写入 README 或提交到 Git。Product Hunt 使用公开 Atom feed，不需要额外 token。

## CLI

`fetch` / `fetch-only` 是诊断命令，可用 `--source`、`--class` 缩小范围；它们不会替换正式日报。正式日报命令始终使用全部当前启用来源：

```bash
python -m app.main fetch [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main fetch-only [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main run-once [--limit N] [--edition-date YYYY-MM-DD] [--publish]
python -m app.main source-health [--source SOURCE_ID]

# 正式的日期级可恢复链路
python -m app.main pipeline run [--limit N] [--output-dir DIR] [--edition-date YYYY-MM-DD] [--publish]
python -m app.main pipeline start [--limit N] [--edition-date YYYY-MM-DD]
python -m app.main pipeline stage-a --edition-date YYYY-MM-DD
python -m app.main pipeline stage-b1 --edition-date YYYY-MM-DD
python -m app.main pipeline stage-c --edition-date YYYY-MM-DD
python -m app.main pipeline stage-d --edition-date YYYY-MM-DD
python -m app.main pipeline export --edition-date YYYY-MM-DD
python -m app.main pipeline status --edition-date YYYY-MM-DD
```

## 完整执行指令

以下命令在 WSL 项目根目录执行。`Settings.from_env()` 会自动读取当前目录的 `.env`；`--force` 仅用于诊断抓取命令以忽略来源冷却时间。

```bash
cd /mnt/d/ai_code/ai_vibecode/aitool_scraping
PYTHON=.venv/bin/python

# 首次安装后，或明确删除旧库后，按当前 ORM 创建新数据库
$PYTHON scripts/init_db.py

# 使用 X/RSSHub 来源时先启动本地 RSSHub；不使用 RSSHub 可跳过
bash scripts/start_rsshub.sh

# 查看来源健康状态和最近一次抓取结果
$PYTHON -m app.main source-health

# 简化入口：完整重建到 draft；确认后增加 --publish 才会替换正式日报
$PYTHON -m app.main run-once \
  --limit 20 \
  --edition-date 2026-08-18 \
  --output-dir output/intel

# 完整的正式可恢复链路。对外唯一标识是日报日期；默认停在待发布 draft。
$PYTHON -m app.main pipeline run \
  --limit 20 \
  --edition-date 2026-08-18 \
  --output-dir output/intel

# 诊断或运维恢复时，仍用日报日期定位；start 创建一个新的完整 draft。
# 同一天再次 pipeline run 会替换此前 draft.db 并重新抓取全部启用来源；
# 已发布日报、output/daily 和 audit.db 在新 draft 成功批准前不会被改动。
$PYTHON -m app.main pipeline start --limit 20 --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-a --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-b1 --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-c --edition-date 2026-08-18
$PYTHON -m app.main pipeline stage-d --edition-date 2026-08-18
$PYTHON -m app.main pipeline export --edition-date 2026-08-18

# 只抓取并检查原始/标准化结果，不调用 AI；这是诊断命令，不会创建正式日报 build
$PYTHON -m app.main fetch-only \
  --source x_account_openai \
  --limit 5 \
  --force \
  --output-dir output/fetch

# Stage B 失败后的安全恢复：只重试 Stage B，不会重新调用 Stage A
$PYTHON -m app.main pipeline retry --edition-date 2026-08-18 --stage stage-b1
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

各阶段的职责、审计位置和批准门禁如下：

1. 正式日报的 `fetch` 从 registry 载入全部当前启用来源，强制完整请求（不使用 HTTP 304 条件请求），把本次响应视为当天完整集合；每个条目只在当前 `draft.db` 内去重。
2. `stage-a` 先执行来源级确定性初筛，再对保留条目调用结构化 AI；`stage-b1` 对通过 Stage A 的条目做 B1 分析，保存摘要、关键词、主题、实体和经本地 guard 的评分。原始条目、拒绝原因、AI 结果和失败 attempt 都保留在该日期 `draft.db`。
3. `stage-c` 直接从当前 build 的成功 Stage B1 条目中按本地评分门槛和结构性校验构造输入，并把输入、排除原因和最近 3 天已发布日报一次性交给聚合 skill。AI 直接决定事件分组、`primary/duplicate/related` 关系、聚合标题与摘要，以及 `new/repeat/updated` 历史状态。本地代码校验所有输入 `item_id` 恰好出现一次、历史 key 合法，并保存事件、完整来源关系和输入审计。Provider、JSON 或 schema 任一失败都会让 Stage C 直接报错并保留失败审计。
4. `stage-d` 只做最终选稿：读取 Stage C 成功任务中的 `candidate_event_ids`，通过一次独立 skill 请求返回 `selected=[{event_id, reason_code, reason}]`。数组顺序就是展示顺序；未选事件由候选集合与入选集合之差推导。Stage D 不改写标题或摘要，不重新评分、聚合、判断新旧、核实来源，也不生成观察池。Stage C 未完成或候选合同缺失时，Stage D 直接失败；候选为空时不调用 provider。
5. `pipeline status --edition-date ...` 同时显示正式日报状态、当前 draft 状态和 retained audit 路径。构建期间 UI/API 始终读取旧的已发布日报；它们不读取 draft。
6. `export` 是明确的批准动作：只有所有来源和 AI 阶段完整成功时，才把 `draft.db` 提升为 `audit.db`、原子替换 `output/daily/YYYY-MM-DD/`，并在正式库内替换该日期的最终日报条目。任一步失败都会恢复旧 audit 和旧日报；失败 draft 留在原处，可按日期检查、重试或恢复。
7. 同日重新抓取不合并上午结果：下午响应中不存在的资讯、被移除来源的资讯及其派生事件，会在下午成功批准后从当天最终日报和新的 `audit.db` 中消失。新 draft 运行期间，上午已发布的 `audit.db` 仍保留，便于和下午 draft 比对。
8. UI（`/`、`/search`、`/all`、`/github`）只读日期级最终日报或已生成报告，不在请求中执行抓取或 AI；首页、搜索和“本期精选”默认只展示当前日期的最终入选事件。

## 审计工作区与排除追踪

不要把 `audit.db` 当作纯文本日志：它是可查询的、完整的阶段审计快照。成功发布后可通过下面命令定位目录和漏斗；没有 pending draft 时，`pipeline status` 也会读取 retained audit 的阶段摘要。

```bash
$PYTHON -m app.main pipeline status --edition-date 2026-08-18
```

若需要追查一条资讯在哪个阶段被排除，可只读查询对应日期的 `audit.db`。以下示例不会修改数据：

```bash
EDITION=2026-08-18
AUDIT="data/editions/$EDITION/audit.db"

# 当前完整构建中，各 item 的最终处理状态/数量
sqlite3 "$AUDIT" \
  'SELECT status, COUNT(*) FROM intel_run_items GROUP BY status ORDER BY status;'

# 用原始 URL 定位 Stage A / B 的决定与原因
sqlite3 "$AUDIT" \
  "SELECT i.id, i.title, i.canonical_url, i.status AS item_status,
          s.decision AS stage_a_decision, s.reason_code AS stage_a_reason_code, s.reason AS stage_a_reason,
          r.status AS stage_b1_status, r.b1_priority
     FROM intel_items AS i
     LEFT JOIN ai_item_screens AS s ON s.item_id = i.id
     LEFT JOIN ai_item_reviews AS r ON r.item_id = i.id
    WHERE i.canonical_url = 'https://example.com/article';"

# 查看 Stage D 的有序入选事件与选稿理由
sqlite3 "$AUDIT" \
  "WITH stage_d_result AS (
       SELECT t.result_json
         FROM intel_run_stage_tasks AS t
         JOIN intel_run_stages AS s ON s.id = t.stage_id
        WHERE s.run_id = (SELECT id FROM intel_runs ORDER BY id DESC LIMIT 1)
          AND s.stage_name = 'stage_d'
          AND t.subject_type = 'run'
          AND t.status = 'succeeded'
        LIMIT 1
   ), selected AS (
       SELECT CAST(j.key AS INTEGER) + 1 AS display_order,
              CAST(json_extract(j.value, '$.event_id') AS INTEGER) AS event_id,
              json_extract(j.value, '$.reason_code') AS reason_code,
              json_extract(j.value, '$.reason') AS reason
         FROM stage_d_result, json_each(stage_d_result.result_json, '$.selected') AS j
   )
   SELECT selected.display_order, e.event_key, e.title,
          selected.reason_code, selected.reason
     FROM selected
     JOIN intel_events AS e ON e.id = selected.event_id
    ORDER BY selected.display_order;"
```

审计库包含原始抓取 payload 和 provider 响应，可能含来源原文或内部错误细节；它只应保存在本地 `data/editions/`，不应提交到 Git 或作为公开日报文件发布。

启动本地 UI：

```bash
$PYTHON -m uvicorn app.web.app:app --host 127.0.0.1 --port 8000
```

`run-once` 和 `pipeline run` 都会执行同一套日期级全量重建，并默认停在可审计的 draft；使用 `--publish` 或单独执行 `pipeline export --edition-date ...` 才会替换正式日报。前者适合一次完成，后者保留明确的阶段恢复入口。`fetch-only` 只抓取并输出标准化条目及来源归因，不调用 AI，也不代表正式日报。

Stage A/B 对每条 AI provider 任务执行瞬态错误自动重试：首次调用失败后最多再重试 5 次（最多 6 次 provider 调用）。429、5xx、timeout 和 rate-limit 属于可重试错误；永久性 4xx、鉴权失败和 schema 错误不会重复请求。达到上限后 draft 保留为失败状态，旧日报不被替换，修复后可使用同一 `edition_date` 恢复。

默认数量策略为：每个来源抓取 20 条，Stage A/B 处理当前完整 build，Stage C 仅聚合本地评分不低于 `AI_STAGE_C_INPUT_MIN_SCORE` 的有效条目，日报默认导出最多 30 条。`run-once --limit`（或 `--fetch-limit`）只控制每来源抓取量；导出阶段的显式 `--limit` 可以覆盖默认日报数量。

`export` 的日报产物默认写入 `output/daily/YYYY-MM-DD/`：

- `intel_items.jsonl`：AI 选择结果与来源归因。
- `intel_digest.md`：分类、状态、指标、风险和链接摘要。
- `manifest.json`：日报日期、公开状态、完整筛选漏斗、阶段状态/失败原因和文件校验信息；不包含内部 build ID。

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
