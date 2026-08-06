**Updated Plan**Turn 019fd03d

# AI 情报抓取与处理重构方案

## 摘要

将当前“所有来源进入一条超长核实流水线”的实现，重构为：

```text
source registry
→ 按来源策略抓取
→ 统一标准化
→ 确定性筛选
→ 每条一次 AI 分类/摘要/风险分析
→ 按内容类别执行轻量核实
→ 生成数据库、JSONL、Markdown
```

数据侧负责抓取、确定性筛选和导出；现有 UI 只读取数据库和导出结果，不在请求中运行 collector 或 AI。GitHub 热点报告按日期保存，数据库不建立历史 Star 快照表。

技术选型确定为：

- Python 3.12
- `httpx`：统一 HTTP 客户端和连接复用
- `feedparser`：RSS/Atom 解析
- GitHub REST API：项目、Release 和最近 7 天 AI topic 候选
- GitHub Trending HTML：daily/weekly 周期新增 Star
- Pydantic：来源配置、AI 响应和内部 DTO 校验
- SQLAlchemy + SQLite：单机定时场景
- 同步单进程 Job，暂不引入 Celery、Redis、消息队列或微服务

## 1. 内容分流策略

### 1.1 `official_model_company`

适用于：

- 新模型发布
- 模型权重、模型卡、版本升级
- AI 公司官方产品、API、价格、能力变化
- 官方技术博客和产品公告

筛选方式：

- 来源 registry 配置时间窗口和关键词
- 优先官方 RSS、官方博客、官方 Release、官方文档
- 默认抓取最近 30 天，按发布时间排序
- AI 分析后保留明确的发布或变化信号

核实方式：

- 只要求一个官方直链
- 官方博客、产品文档、模型卡、官方 GitHub Release 均可
- 直链请求成功并能支持标题/发布主体/版本等基本信息，即标记 `verified`
- 找不到官方直链标记 `needs_review`，不进入强推荐
- 默认不执行 Tavily 多轮搜索，不拆 claim/evidence 多阶段

### 1.2 `project_tool`

适用于：

- GitHub 项目
- AI 工具、Agent、MCP、Skill、工作流
- Product Hunt 项目
- 带 GitHub 地址的产品工具

筛选方式按来源类型独立配置：

- GitHub：
  - Trending HTML daily/weekly 的周期新增 Star > 0
  - Search API 命中六个 AI topic，最近 7 天有 `pushed_at` 且累计 Star > 100
  - 按周期新增 Star、累计 Star 降序
  - 额外记录 forks、语言、topic、release、README 完整度
- Product Hunt：
  - 按 votes、评论、发布时间和增长信号排序
- 带 GitHub 链接的产品：
  - 解析 canonical `owner/repo`
  - 以 GitHub Star 和活跃度作为主要热度指标

核实方式：

- GitHub metadata 可直接证明仓库存在、Star、Fork、最近 push、License、Release 等事实
- README 和产品描述标记为“项目自述”，不自动当作真实性结论
- 不要求第三方验证后才展示
- 风险只记录 archived、fork、缺少 README、缺少 License、补全失败等确定性信号
- 最终状态使用 `hotspot`，不伪装成已核实推荐

### 1.3 `community_social`

适用于：

- X
- Reddit
- RSSHub 搜索
- LINUX DO
- 其他社区讨论

筛选方式：

- 按时间、互动量、来源优先级和关键词筛选
- 只作为发现线索
- 不以社区内容单独产生高可信结论

处理方式：

- AI 可生成摘要和风险提示
- 如果内容中发现 GitHub、官网或模型卡链接，则转化为 `project_tool` 或 `official_model_company` 候选
- 没有直链时保持 `discovery_only`
- 不执行复杂证据搜索和 claim 核实

## 2. 新的数据模型

放弃当前复杂阶段表之间的强耦合，重建为少量核心实体。

### `sources`

保存来源和策略：

```text
id
name
type
url
enabled
fetch_interval
content_class
collector_type
selection_policy_json
verification_policy_json
priority
```

示例：

```yaml
content_class: project_tool
selection_policy:
  mode: github_active_high_star
  pushed_days: 30
  min_stars: 100
  sort: stars
  limit: 30
verification_policy:
  mode: metadata_only
```

### `fetch_attempts`

保存每次来源请求：

```text
id
run_id
source_id
started_at
finished_at
status
http_status
transport
request_url
response_bytes
items_fetched
items_inserted
items_skipped
retry_count
error_code
error_message
```

状态：

```text
running | success | not_modified | failed | skipped
```

### `intel_items`

保存统一内容：

```text
id
source_id
external_id
canonical_url
title
summary
content_text
published_at
captured_at
content_class
metrics_json
raw_payload_json
content_hash
status
```

`metrics_json` 统一承载：

```json
{
  "stars": 1200,
  "forks": 100,
  "votes": 500,
  "comments": 30,
  "pushed_at": "...",
  "trending": {
    "weekly": {"rank": 1, "stars_since": 250},
    "daily": {"rank": 3, "stars_since": 80}
  },
  "search_topics": ["llm"]
}
```

GitHub 项目优先使用 `github_repo:<owner/repo>` 或 canonical URL 去重；来自 Trending HTML 和 Search API 的指标合并到同一条记录。

### `ai_item_reviews`

每条候选最多一条当前分析结果：

```text
id
item_id
model
prompt_version
keep
content_class
summary_cn
reason
risk_flags_json
needs_verification
official_url
confidence
raw_response_json
status
error_message
created_at
updated_at
```

### `item_verifications`

只对需要核实的条目写入：

```text
id
item_id
mode
status
verification_url
source_domain
http_status
title
content_preview
supports_basic_fact
risk_flags_json
reason
checked_at
```

`mode`：

```text
official_direct_link | metadata_only | discovery_only
```

`status`：

```text
verified | needs_review | failed | skipped
```

不再保留当前 `claim_extract → evidence_search → evidence_fetch → evidence_classify → claim_verify → ai_verify` 作为默认处理链。

## 3. 代码结构

建议将数据侧整理为以下边界：

```text
app/
├─ config/
│  ├─ settings.py
│  └─ source_registry.yaml
├─ collectors/
│  ├─ base.py
│  ├─ rss.py
│  ├─ rsshub.py
│  ├─ github.py
│  └─ producthunt.py
├─ domain/
│  ├─ models.py
│  ├─ policies.py
│  ├─ scoring.py
│  └─ verification.py
├─ ai/
│  ├─ client.py
│  ├─ schemas.py
│  └─ prompts.py
├─ storage/
│  ├─ models.py
│  ├─ repository.py
│  └─ db.py
├─ jobs/
│  ├─ fetch_job.py
│  ├─ process_job.py
│  └─ export_job.py
└─ main.py
```

### Collector 接口

所有 collector 统一实现：

```python
collect(source: SourceSpec, limit: int) -> FetchBatch
```

`FetchBatch` 包含：

```text
items
http_status
final_url
response_bytes
retry_count
transport
error
```

GitHub、RSS、RSSHub、Product Hunt 只负责抓取和字段映射，不包含 AI、评分或数据库写入。

### 三个主 Job

#### `fetch_job`

职责：

- 读取 registry
- 判断 `fetch_interval`
- 复用一个 `httpx.Client`
- 执行 source collector
- 按 source 事务写入 `intel_items` 和 `fetch_attempts`
- 单个 source 失败不影响其他 source

#### `process_job`

职责：

1. 按 source policy 确定性筛选
2. 仅对 `official_model_company` 和 `community_social` 保留项执行一次 AI 调用
3. GitHub 项目/Release 只使用 stars、forks、pushed_at 等 metadata，不调用 AI
4. 根据 `content_class` 决定是否执行轻量核实
5. 按需写入 `ai_item_reviews` 和 `item_verifications`

AI 单条响应固定返回：

```json
{
  "keep": true,
  "content_class": "project_tool",
  "summary_cn": "...",
  "reason": "...",
  "risk_flags": [],
  "needs_verification": false,
  "official_url": null,
  "confidence": 86
}
```

本地强约束：

- `content_class` 以 source registry 为准，AI 不能擅自改变来源类别
- 分数限制在 0-100
- AI 生成的 URL 只能作为候选，必须通过 URL/域名校验
- AI 失败时保留原始条目，状态为 `ai_failed`，不静默丢弃

#### `export_job`

职责：

- 导出保留项目 JSONL
- 导出 Markdown 日报
- 输出分类统计、失败统计和待核实列表
- 不重新抓取、不调用 AI、不执行复杂业务逻辑

## 4. 筛选和排序

### GitHub 项目/工具

默认策略：

```text
Trending HTML daily/weekly 的周期新增 Star > 0
OR Search API 命中 6 个 AI topic 且 pushed_days <= 7 AND stars > 100
ORDER BY Trending 周期新增 Star、累计 Star DESC
```

Search API 的候选 topic 为：`llm`、`ai-agent`、`rag`、`vector-database`、`large-language-model`、`machine-learning`。

GitHub Trending 使用 GitHub 页面原生的 `stars today` / `stars this week` 信号；项目当前不建立历史 Star 快照，不把累计 Star 差值伪装成周增长。

后续如需更细粒度评分可增加：

```text
activity_score =
  最近 7 天 push       40%
  最近 release          20%
  累计 Star             30%
  Fork                  10%
```

GitHub 项目不使用“是否新发布”作为硬条件。

### 官方模型/公司

默认策略：

```text
发布时间 <= 30 天
AND 标题/描述命中模型、API、发布、版本、价格、公司等关键词
```

按：

```text
发布时间
来源优先级
是否包含官方直链
```

排序。

### 社区来源

默认策略：

```text
时间 <= 7 天
AND 互动量或关键词命中
```

仅生成线索，不进入强推荐。

## 5. CLI 和运行方式

保留三个清晰入口：

```bash
python -m app.main fetch
python -m app.main process
python -m app.main export
```

提供一个日常入口：

```bash
python -m app.main run-once
```

`run-once` 顺序固定：

```text
fetch → process → export
```

支持：

```bash
--source SOURCE_ID
--class official_model_company|project_tool|community_social
--limit N
--force
--dry-run
```

默认单进程、顺序写入 SQLite，保证事务简单；HTTP 请求使用共享 client。AI 失败按条记录，不回滚整批。

## 6. 删除和保留

### 删除或停用

以下阶段不再作为默认流程：

```text
claim_extract_job
evidence_search_job
evidence_fetch_job
evidence_classify_job
claim_verify_job
ai_verify_job
recommendation_write_job
```

相关代码可以先移动到 `app/legacy/`，完成新链路验证后删除。

### 保留

```text
RSS/Atom/RSSHub/GitHub API/Trending collectors
source registry
normalize 基础逻辑
AI provider 配置
GitHub metadata reader（`app/storage/github_reader.py`）
数据库 Repository 思路
JSONL/Markdown export
GitHub Trending date-scoped Markdown report
```

GitHub 项目仍直接使用标准 `intel_items.jsonl` 的 metadata，不调用 AI 评分；`export` 同时生成 `output/github-trending/YYYY/MM/YYYYMMDD.md`，现有 `/github` 页面读取合并后的 Trending/Search 指标。

## 7. 测试和验收标准

必须覆盖：

- RSS、Atom、RSSHub、GitHub API、GitHub Trending HTML、Product Hunt 的统一 collector 接口
- source policy 的三类分流
- GitHub Trending 周期 Star、Search API 最近 push + Star 阈值
- Product Hunt votes/时间排序
- 社区内容标记 `discovery_only`
- 官方直链成功、404、错误域名、超时
- AI 合法 JSON、缺字段、非法分数、模型超时
- AI 单条失败不影响其他条目
- 重复抓取不产生重复 `intel_items`
- `run-once` 可重复执行
- JSONL/Markdown 字段完整
- dry-run 不写数据库

验收目标：

```text
1. 默认流程最多 3 个处理阶段。
2. 每条候选最多 1 次 AI 调用。
3. GitHub 项目不因没有第三方证据而被丢弃。
4. 模型/公司内容没有官方直链时不会标记 verified。
5. 社区内容不会单独进入强推荐。
6. 任一来源失败不会中断整次运行。
7. 每次抓取、AI 调用和核实都有可查询状态。
8. 旧数据库可直接删除并由新 schema 初始化。
```

## 8. 实施顺序

1. 新建简化后的 domain DTO、registry policy 和统一 collector 接口。
2. 重写 `fetch_job` 和 `process_job`，接入 RSS、GitHub API、GitHub Trending HTML、官方 RSS。
3. 接入 Product Hunt 和 RSSHub/X/Reddit 的 `community_social` 策略。
4. 接入单条 AI 结构化响应和本地 guard。
5. 实现官方直链轻量核实。
6. 实现 JSONL/Markdown export。
7. 用现有真实 source registry 做全量回归。
8. 删除旧 evidence/claim 多阶段默认入口和无效配置。
9. 保持 UI 只读边界，并展示合并后的 GitHub Trending/Search 指标和日期报告入口。
