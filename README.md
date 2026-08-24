# AI 情报抓取与处理

本项目支持一条可重复执行、可恢复的 AI-only 文字情报链路：

```text
信息源配置
→ 抓取与标准化
→ Stage A 初筛
→ Stage B1 分析、评分、准入
→ Stage C 事件聚合与核验
→ Stage D 人工式二次审核
→ Export 导出
→ 正式发布
```

Stage A 是唯一的时间准入和初筛阶段；低置信度结果保留给后续分析。Stage B1 对每个 Stage A 合格条目执行一次结构化分析，输出主题分类、中文短摘要、关键词、实体和编辑优先级评分。随后本地 deterministic guard 以固定的总分 `60` 和 `audience_relevance>=60` 为下限生成 `active`、`reserve`、`filtered` 候选池：初始 active 目标为 100 条、reserve 默认 20 条；积累 14 期日报后，active 目标按已发布的候选/入选比例动态校准在 60–120 条。Stage C 运行可审计的 Responses agent，按需读取候选、正文和近 3 天历史；Stage D 选择当天最终有序子集。Export 只校验并序列化 Stage D 结果，不再次筛选。

`content_class` 描述来源/信号类型（官方发布、媒体报道、项目/工具、社区线索），`topic_category` 使用橘鸦日报的六类编辑主题（开发生态、模型发布、产品应用、行业动态、技术与洞察、前瞻与传闻）。两者分开保存，UI 和导出会同时展示，避免把第三方媒体报道误读成“官方产品发布”。

## 内容类别与来源归因

| `content_class` | 典型来源 | 处理方式 |
| --- | --- | --- |
| `official_model_company` | 官方模型、公司产品、API 和研究更新 | 参与统一内容筛选；聚合时优先作为事件主来源 |
| `news_media` | 科技媒体、垂直 AI 新闻和分析博客 | 保持媒体归因，重要事实优先补直接来源，不标注为官方发布 |
| `project_tool` | GitHub Release、Product Hunt、AI 工具项目 | 按项目更新参与统一筛选；GitHub 项目可单独生成项目摘要 |
| `community_social` | X、Reddit、RSSHub、论坛 | 作为社区线索参与 AI 分类；输出保留来源归因和风险标记 |

默认主题分类为：`开发生态`、`模型发布`、`产品应用`、`行业动态`、`技术与洞察`、`前瞻与传闻`。主题分类与来源类型是两个独立字段；Stage B1 只输出这六类，不再使用旧的模型、产品、项目、行业、论文、教程、观点分类。

来源只保留两层分类：`source_group` 表示可追溯的具体来源组，`content_class` 由来源组自动归并为四种粗粒度类型。`transport` 仅负责抓取路由，不参与内容分类。导出和 UI 保留 `source_id`、`source_name`、`source_group`、`content_class`、`transport` 与来源链接；X 官方账号统一通过 `source_group=x_official` 归因。

## 抓取来源

来源配置位于 `app/config/source_registry.yaml`。唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`；Feed 细节在 `feed` 下，GitHub 细节在 `github` 下。日报主链路已停用普通 arXiv 聚合源、GitHub Trending 和 GitHub Search 新项目源；当前只保留明确仓库的 GitHub Releases。后续新项目发现可在独立专栏中重新启用，不与日报事件池混跑。

当前 registry 在配置 `RSSHUB_BASE_URL` 后有 69 个启用来源（具体数量以 YAML 为准）：

| `transport` | 当前数量 | 主要来源组 | 抓取方式与内容 |
| --- | ---: | --- | --- |
| `feed` | 20 | `official_blog`、`official_research`、`tech_media`、`hacker_news`、`producthunt`、`linux_do`、`reddit_fixed` | HTTP 获取 RSS/Atom，统一解析为条目；普通 arXiv 聚合源当前禁用。个别公开源可在 registry 中显式 `bypass_proxy`，避免依赖进程级 `NO_PROXY`。 |
| `github` | 3 | `github_release` | 只跟踪 Claude Code、Ollama、Transformers 等明确仓库的 Release；Trending 与 Topic Search 当前禁用。 |
| `rsshub` | 46 | `x_official`、`x_social`，以及 Anthropic、DeepSeek RSSHub 路由 | 访问本地 RSSHub 输出的 RSS/Atom；X 官方账号通过 `source_group=x_official` 归因，不绕过 AI 筛选。 |

如果没有配置 `RSSHUB_BASE_URL`，46 个 RSSHub 模板会被安全跳过，CLI 会显示 `Registry skipped`；这不会阻断其它 Feed 和 GitHub Release 来源。单个来源失败只记录在 `fetch_attempts`、来源健康状态、日报 manifest 和日报警告中，不会中断整个批次或阻止发布。

日常抓取不传 `--limit-per-source` 时使用各来源的 `default_limit`；该选项只用于人工调试时统一覆盖所有来源限额，例如 `--limit-per-source 30`。

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

`draft.db` 和正式数据库是权威数据。`pipeline run` 完成后会从 `draft.db` 生成 `output/intel/draft/YYYY-MM-DD/` 下的 Markdown、JSONL 和 manifest，供人工审核；这些文件只是展示投影，修改它们不会改变发布结果。`pipeline export` 发布前会重新从 `draft.db` 生成结构化记录，再更新正式数据库和 `output/daily/YYYY-MM-DD/`。

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
AI_REVIEW_TIMEOUT_SECONDS=30
AI_REVIEW_CONCURRENCY=4
AI_REVIEW_CATEGORIES=开发生态,模型发布,产品应用,行业动态,技术与洞察,前瞻与传闻
AI_STAGE_B_RESERVE_LIMIT=20
AI_STAGE_C_TIMEOUT_SECONDS=120
AI_STAGE_C_AGENT_MAX_TURNS=32
AI_STAGE_C_AGENT_MAX_TOOL_CALLS=120
AI_STAGE_C_AGENT_MAX_WEB_SEARCHES=16
AI_STAGE_D_MAX_WEB_SEARCHES=6
TAVILY_API_KEY=
TAVILY_API_URL=https://api.tavily.com
TAVILY_TIMEOUT_SECONDS=30
```

Stage C 每次 Responses 请求默认超时 `120` 秒，独立于 Stage A/B 使用的 `AI_REVIEW_TIMEOUT_SECONDS`；可通过 `AI_STAGE_C_TIMEOUT_SECONDS` 继续调大。按默认 `32` turns 估算，Stage C 任务租约约为 66 分钟。其默认预算为 `32` turns、`120` 次本地工具调用和 `16` 次 Tavily 搜索；显式配置更大的非负整数会原样生效。`AI_STAGE_C_AGENT_MAX_WEB_SEARCHES=0` 和 `AI_STAGE_D_MAX_WEB_SEARCHES=0` 分别关闭 C、D 搜索。

AI 推理调用统一使用 OpenAI Responses 语义；`AI_REVIEW_API_URL` 可以是 `/v1` 基址或完整的 `/v1/responses` 地址。C、D 的网页核验不再依赖 provider hosted search，而是由本地 `TAVILY_API_KEY` 调用 Tavily。搜索不设域名白名单，返回的 URL、摘要、查询和绑定事件/claim 会保存到 agent step 与 evidence 审计记录。Stage C 对每个 `needs_review` 草稿分别核验；搜索后仍无法解决、搜索不可用或预算耗尽时保留待审状态，由 Stage D 终审决定去留。

Stage B 的本地准入门槛固定为：`b1_priority>=60`，且 `audience_relevance>=60`。这两个业务规则不由 `.env` 配置，也不再由 Stage C 决定。Stage B 将合格项按来源/主题多样性形成 active 候选池（初始目标 100 条、14 期后动态 60–120 条）和最多 20 条 reserve；Stage C 从这两个持久化集合按需取数，按主体、动作、对象、版本/阶段和时间锚点区分同一事件、后续进展与同主题事件。历史旧闻只比较当前日报日期之前三个自然日内的已发布最终日报；草稿、搜索结果和更早日报不扩展去重窗口。Stage C 使用 `candidate`、`needs_review`、`rejected` 三态保存完整审计结果，只把前两态交给 Stage D；Stage D 在 `max_selected` 上限内完成人工式二次审核和有序子集选择。

Stage B1 的 `b1_priority` 只衡量内容价值，由本地 guard 按 `audience_relevance`（AI 主体相关性）45%、`impact_scope`（已确认影响范围）25%、`independent_news_value`（事件级独立新闻价值）20%、`material_change` 5% 和 `specificity` 5% 重算。来源身份、AI 把握度和时间新鲜度不参与该分数：来源归因来自 source registry，Stage A 按日报日期前一天 00:00（Asia/Shanghai）做本地时间筛选。

`AI_REVIEW_CONCURRENCY` 取值为 `1..4`，默认 `4`，表示 Stage A/B provider 请求的并发上限。
Stage D 与 Stage A/B/C 共用 `AI_REVIEW_*` provider 配置；它使用独立提示词和兼容的选择输出 schema，不需要额外的推理模型配置。Stage D 会优先搜索 `needs_review`、`uncertain`、`repeat` 等争议事件，再按预算检查其他事件；搜索证据随事件输入终审模型。由于 Stage D 一次提交完整事件池，实际请求超时下限为 120 秒。

真实 token、API key、Cookie 和代理地址只放在本地 `.env`，不要写入 README 或提交到 Git。Product Hunt 使用公开 Atom feed，不需要额外 token。

## CLI

`fetch` / `fetch-only` 是诊断命令，可用 `--source`、`--class` 缩小范围；它们不会替换正式日报。正式日报命令始终使用全部当前启用来源：

```bash
python -m app.main fetch [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main fetch-only [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main source-health [--source SOURCE_ID]
python -m app.main run-once [--limit N|--fetch-limit N] [--edition-date YYYY-MM-DD] [--publish]  # pipeline run 兼容别名

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
cd /mnt/d/ai_code/ai_vibecode/AItool_scraping
PYTHON=.venv/bin/python

# 首次安装后，或明确删除旧库后，按当前 ORM 创建新数据库
$PYTHON scripts/init_db.py

# 使用 X/RSSHub 来源时先启动本地 RSSHub；不使用 RSSHub 可跳过
bash scripts/start_rsshub.sh

# 查看来源健康状态和最近一次抓取结果
$PYTHON -m app.main source-health

# 正式全量入口。完成 A-D 后生成可审核的 draft Markdown，但不更新正式数据库。
$PYTHON -m app.main pipeline run \
  --limit 20 \
  --edition-date 2026-08-18 \
  --output-dir output/intel

# 查看 draft 状态和审核文件路径
$PYTHON -m app.main pipeline status \
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
3. `stage-b1` 完成后，本地规则把条目写成可审计的 `active` / `reserve` / `filtered` 候选准入记录；评分阈值、工作台容量和 reserve 都在 B 生效。`stage-c` 是状态化 Responses agent：它读取 active 候选、正文和前三个自然日内的已发布最终日报，按主体、动作、对象、版本/阶段和时间锚点聚合事件；对影响核心事实的不确定项调用本地 Tavily 搜索，并把结果绑定到具体草稿和 claim。模型可在证据支持后收窄表述；核验仍无法解决时保留 `needs_review`。本地代码校验 active 覆盖、精确身份不可拆分，并复核 `repeat`/`updated` 与实质变化证据。超出 agent 预算时，未覆盖的候选会变为 `needs_review` 事件而不是被丢弃。
4. `stage-d` 做人工式最终复审：读取 Stage C 成功任务中的 `candidate_event_ids`，其中包含 `candidate` 和 `needs_review`，不包含保留在 C 审计池中的 `rejected`。Stage D 优先对争议事件执行 Tavily 核验，再通过独立 skill 返回 `selected=[{event_id, reason_code, reason}]`。数组顺序就是展示顺序；未选事件由可审事件集合与入选集合之差推导。Stage D 不改写标题或摘要，也不重新聚合事件。
5. A-D 完成后，`pipeline run` / `pipeline resume` 从 `draft.db` 生成 `output/intel/draft/YYYY-MM-DD/intel_digest.md`、`intel_items.jsonl` 和 `manifest.json`。`pipeline status --edition-date ...` 会显示 draft 状态和审核文件路径。构建期间 UI/API 始终读取旧的已发布日报；它们不读取 draft 文件或 draft 数据库。
6. `export` 是明确的批准动作：发布逻辑仍以 `draft.db` 为准，不解析审核 Markdown；它重新生成结构化结果和公开文件，把 `draft.db` 提升为 `audit.db`、原子替换 `output/daily/YYYY-MM-DD/`，并在正式库内替换该日期的最终日报条目。单个来源抓取失败只写入 `source_warnings` 和日报提示，不再阻止发布；真正的阶段/导出失败仍会恢复旧 audit 和旧日报，失败 draft 留在原处，可按日期检查、重试或恢复。
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

`pipeline run` 是正式的日期级全量重建入口，默认停在可审计、可查看 Markdown 的 draft；`run-once` 作为兼容别名注册到同一个 CLI 函数，不维护独立流程，并继续接受旧的 `--fetch-limit` 参数名。使用 `--publish` 或单独执行 `pipeline export --edition-date ...` 才会替换正式数据库和公开日报。`fetch-only` 只抓取并输出标准化条目及来源归因，不调用 AI，也不代表正式日报。

Stage A/B 对每条 AI provider 任务执行瞬态错误自动重试：首次调用失败后最多再重试 5 次（最多 6 次 provider 调用）。429、5xx、timeout 和 rate-limit 属于可重试错误；永久性 4xx、鉴权失败和 schema 错误不会重复请求。达到上限后 draft 保留为失败状态，旧日报不被替换，修复后可使用同一 `edition_date` 恢复。

默认数量策略为：抓取层使用 `source_registry.yaml` 中每个来源自己的 `default_limit`，Stage A/B 处理当前完整 build；B 只让本地 guard 总分不低于 60 且 AI 主体相关性不低于 60 的条目进入 C 工作台，初始 active 上限为 100、reserve 为 20，14 期后 active 动态控制在 60–120；日报最终导出最多 30 条。`pipeline run --limit` 仅用于人工统一覆盖每来源抓取量；导出阶段的显式 `--limit` 可以覆盖默认日报数量。

draft 审核产物默认写入 `output/intel/draft/YYYY-MM-DD/`，正式发布产物默认写入 `output/daily/YYYY-MM-DD/`；两者文件结构一致：

- `intel_items.jsonl`：AI 选择结果与来源归因。
- `intel_digest.md`：分类、状态、指标、风险和链接摘要。
- `manifest.json`：日报日期、公开状态、完整筛选漏斗、阶段状态/失败原因和文件校验信息；不包含内部 build ID。

独立 GitHub 项目抓取仍保留 stars、forks、topics 和 README 摘要等能力；正式日报当前只启用 Release 路由，Trending 周期指标仅在对应项目专栏重新启用后产生。AI 不替代项目指标筛选，也不会生成未提供的增长数据。

本地 RSSHub 启动：

```bash
bash scripts/start_rsshub.sh
```

## Web UI

FastAPI/Jinja UI 只读取数据库和已生成报告，展示主题分类、来源归因、AI 摘要、风险和选择状态；首页按主题分组，卡片同时显示来源名称、来源组、内容类型、transport 和原文链接，并提供“来源目录”页。请求过程中不会运行 collector、AI、搜索或其他处理任务。

## 验证

```bash
TMPDIR=/tmp python -m pytest -q
python -m compileall -q app scripts
python -m app.main --help
```
