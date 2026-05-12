# Tavily 证据核实链路优化方案

## 1. 背景

当前项目已经在原有 RSS 初筛流程之后，新增了以下智能核实链路：

```text
ai-review
→ claim-extract
→ evidence-search
→ ai-verify
→ recommendation-export
```

其中 `evidence-search` 阶段引入 Tavily 搜索，用于在调用模型本身没有联网搜索能力的情况下，补充外部证据，辅助判断 AI 工具、AI 资讯、MCP、workflow、skill、模型发布等信息的真实性和推荐价值。

这次改动的方向是正确的：项目已经从“RSS + 规则预筛 + AI 初筛”升级为“RSS + AI claim 抽取 + Tavily 搜索证据 + AI verify 多维评分”。

但当前实现仍处于第一版。需要注意：

```text
Tavily search result ≠ verified evidence
搜索相关性分数 ≠ 事实可信度
模型抽取 URL ≠ URL 真实存在
有外链 ≠ 外链支持 claim
```

因此，下一阶段重点不是继续堆更多 prompt，而是把 Tavily evidence 链路做成更稳定、更可重跑、更可审计、更接近事实核实的工程系统。

---

## 2. 当前实现评价

### 2.1 已完成的有效改进

当前实现已经具备以下能力：

```text
1. 从 AI 初筛结果中抽取结构化 claim。
2. 从 claim 中识别 entity_name、entity_type、official_url、github_url、huggingface_url、producthunt_url。
3. 使用 Tavily 对 entity_name 进行搜索。
4. 将搜索结果保存为 evidence_items。
5. 让 AI verify 基于 candidate、claim、evidence_items、source_quality 输出多维评分。
6. 使用本地 finalize_verification 逻辑重新计算 final_score。
7. 对无证据、hard negative、spam_risk 做强制降分。
8. 导出推荐 Markdown / JSONL。
```

这说明系统已经初步具备“外部证据辅助推荐”的能力。

### 2.2 当前主要不足

当前版本还存在几个关键不足：

```text
1. evidence-search 的完成状态判断过粗。
2. Tavily score 被直接转成 confidence，容易混淆搜索相关性和证据可信度。
3. claim 抽取出的 URL 没有被真实验证。
4. direct evidence 只保存 URL，没有抓取页面内容。
5. GitHub / Hugging Face 没有专门 verifier。
6. source quality 字段已经进入 SourceConfig，但没有完整持久化到 Source 表。
7. recommendation-export 默认包含 rejected 内容，适合审阅但不适合用户推荐。
8. 缺少 search cache、重试状态、失败恢复和运行统计。
9. 还没有 canonical entity 聚合，重复推荐问题仍未解决。
```

---

## 3. 优化目标

下一阶段优化目标：

```text
1. 让 evidence-search 可以可靠重跑，避免半完成状态。
2. 明确区分“搜索发现结果”和“已验证证据”。
3. 对 direct URL、GitHub repo、Hugging Face model、官方文档做真实验证。
4. 在 AI verify 之前增加规则化 evidence classify 层。
5. 降低模型幻觉 URL 和 Tavily 搜索误报对 final_score 的影响。
6. 降低 API 成本，增加 Tavily search cache。
7. 输出面向用户的推荐结果时默认只保留 final_keep=true 的项目。
8. 后续支持 canonical entity 聚合和多来源证据汇总。
```

---

## 4. 推荐的新证据链路

当前链路：

```text
claim-extract
→ evidence-search
→ ai-verify
```

建议升级为：

```text
claim-extract
→ evidence-discover
→ evidence-fetch
→ evidence-classify
→ ai-verify
→ recommendation-export
```

各阶段职责：

| 阶段 | 作用 |
|---|---|
| claim-extract | 从候选内容中抽取实体、链接和主要 claim |
| evidence-discover | 使用 direct URL、Tavily、搜索源发现可能证据 URL |
| evidence-fetch | 实际访问 URL，获取状态码、标题、正文、元数据 |
| evidence-classify | 判断该证据 support / contradict / neutral / unknown |
| ai-verify | 基于候选、claim、已分类证据进行最终多维评分 |
| recommendation-export | 输出最终推荐或审阅文件 |

核心思想：

```text
Tavily 只负责 discover，不负责最终 fact verification。
```

---

## 5. P0：增加任务状态机，解决半完成问题

### 5.1 当前问题

当前 `evidence-search` 是否已处理，主要依赖某个 claim 是否已经存在 `evidence_items`。这会导致半完成风险：

```text
1. 某个 claim 开始 evidence-search。
2. 先插入 direct evidence，例如 source_url、github_url。
3. 后续 Tavily 搜索超时或失败。
4. 该 claim 已经有 evidence_items。
5. 下次重跑时被误认为已处理完成。
6. Tavily 部分永远不会补齐。
```

### 5.2 建议新增字段

在 `ExtractedClaim` 中增加：

```python
class ExtractedClaim(Base):
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    evidence_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

状态取值：

```text
pending      尚未搜索
searching    正在搜索
partial      部分成功，但存在失败 query
completed    所有计划 query 已完成
failed       全部失败或超过最大重试次数
```

### 5.3 查询逻辑

`list_pending_for_evidence_search` 应改成：

```text
evidence_status in ('pending', 'partial', 'failed')
and evidence_attempts < MAX_ATTEMPTS
```

而不是：

```text
没有 evidence_items
```

### 5.4 Job 行为

伪代码：

```python
for claim in claims:
    mark_evidence_status(claim.id, "searching")
    try:
        run_direct_evidence_seed()
        run_tavily_queries()
        if query_failures:
            mark_evidence_status(claim.id, "partial", error=...)
        else:
            mark_evidence_status(claim.id, "completed")
    except Exception as exc:
        mark_evidence_status(claim.id, "failed", error=str(exc))
```

### 5.5 验收标准

```text
1. Tavily 某个 query 失败后，下次运行可以继续补查。
2. direct evidence 成功但 Tavily 失败时，状态为 partial，而不是 completed。
3. 超过最大重试次数后状态变为 failed。
4. recommendation-export 可以显示 evidence_status，便于审阅。
```

---

## 6. P0：拆分 retrieval_score 和 evidence_confidence

### 6.1 当前问题

Tavily 返回的 `score` 更接近搜索相关性，而不是事实可信度。当前将其转为 `confidence`，容易让后续 AI 误解为“这条证据可信”。

### 6.2 建议字段调整

将 `EvidenceItem` 改为：

```python
class EvidenceItem(Base):
    retrieval_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supports_claim: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
```

含义：

| 字段 | 含义 |
|---|---|
| retrieval_score | Tavily 或搜索源认为该结果和 query 的相关性 |
| evidence_confidence | 系统判断该页面作为证据的可信度 |
| supports_claim | support / contradict / neutral / unknown |

### 6.3 默认策略

Tavily search 结果：

```text
retrieval_score = Tavily score
supports_claim = unknown
evidence_confidence = 初始较低，例如 30-50
```

direct URL：

```text
retrieval_score = 100
evidence_confidence = 20-50，等待 fetch 验证
supports_claim = unknown
```

经过 evidence-fetch 和 evidence-classify 后再更新：

```text
evidence_confidence = 70-95
supports_claim = support / contradict / neutral
```

---

## 7. P0：验证 direct URL，防止模型幻觉链接

### 7.1 当前问题

claim 抽取模型可能生成不存在的 official_url、github_url、huggingface_url。当前这些 URL 会直接进入 direct evidence seed，并被赋予较高初始 confidence。

### 7.2 建议新增 URL 验证逻辑

新增：

```text
url_validation_status
```

取值：

```text
unchecked
reachable
unreachable
redirected
forbidden
timeout
invalid
```

可以先在 `EvidenceItem` 中增加字段：

```python
http_status: Mapped[int | None]
final_url: Mapped[str | None]
url_validation_status: Mapped[str]
fetched_title: Mapped[str | None]
fetched_text_preview: Mapped[str | None]
```

### 7.3 验证规则

```text
1. 2xx：reachable。
2. 3xx：记录 final_url，状态 redirected。
3. 403 / 401：forbidden，但不一定无效。
4. 404 / 410：unreachable，强负信号。
5. timeout：timeout，可重试。
6. URL parse 失败：invalid。
```

### 7.4 对评分影响

```text
unreachable official_url: risk_flags += broken_primary_link
unreachable github_url: risk_flags += broken_primary_link
invalid model generated URL: risk_flags += hallucinated_url
forbidden: 不直接 hard drop，但降低 confidence
```

---

## 8. P1：新增 evidence-fetch 阶段

### 8.1 目标

将 evidence 从“发现 URL”升级为“抓取真实页面内容”。

新增脚本：

```bash
python scripts/run_evidence_fetch_once.py --limit 50
```

新增 CLI：

```bash
python -m app.main evidence-fetch --limit 50
```

### 8.2 普通网页抓取字段

```python
class EvidenceItem(Base):
    http_status: int | None
    final_url: str | None
    fetched_title: str | None
    fetched_description: str | None
    fetched_text_preview: str | None
    fetch_status: str
    fetch_error: str | None
    fetched_at: datetime | None
```

### 8.3 抓取策略

```text
1. 使用 httpx GET，开启 follow_redirects。
2. 设置 User-Agent。
3. 限制正文大小，例如最多读取 512KB。
4. 抽取 title、meta description、正文前 2000-4000 字。
5. 对 HTML 做基本清洗。
6. 对 PDF / binary / image 暂时只记录 content-type。
```

### 8.4 安全与稳定性

```text
1. 设置 timeout。
2. 限制最大响应体大小。
3. 禁止访问内网地址，避免 SSRF 风险。
4. 失败可重试。
5. 403/429 不直接判为虚假，只作为弱证据。
```

---

## 9. P1：新增 GitHub 专用 verifier

### 9.1 为什么需要

AI 工具、MCP、workflow、skill 很多最终落到 GitHub。仅靠 Tavily 摘要无法判断仓库质量。需要专门核实。

### 9.2 新增脚本

```bash
python scripts/run_github_verify_once.py --limit 50
```

或者合并到 evidence-fetch：

```text
如果 evidence_type == github_repo，则走 GitHub verifier。
```

### 9.3 需要抓取的 GitHub 字段

```text
repo_exists
owner
repo
description
stars
forks
open_issues
archived
disabled
private
license
default_branch
created_at
updated_at
pushed_at
readme_exists
readme_preview
release_count
topics
languages
```

### 9.4 质量判断

强正信号：

```text
1. README 存在且包含 install / usage / quickstart。
2. 最近 30-90 天有 commit。
3. 有 license。
4. 有 release 或 tagged version。
5. MCP 项目包含 mcp/server/config/install 等关键词。
6. workflow 项目包含 workflow/example/template 等关键词。
```

负信号：

```text
1. 仓库不存在。
2. 仓库为空。
3. archived。
4. README 极短。
5. 声称开源但没有 license。
6. 最近长期无更新。
7. 只有营销 README，没有代码。
```

### 9.5 输出建议

在 `EvidenceItem.raw_payload` 中保存：

```json
{
  "provider": "github",
  "repo_exists": true,
  "stars": 123,
  "license": "MIT",
  "readme_exists": true,
  "pushed_at": "2026-05-10T00:00:00Z",
  "quality_flags": ["readme_exists", "recent_commit", "has_license"],
  "risk_flags": []
}
```

并更新：

```text
evidence_type = github_repo
supports_claim = support / contradict / neutral
evidence_confidence = 0-100
```

---

## 10. P1：新增 Hugging Face 专用 verifier

### 10.1 为什么需要

对于 model_release、open weights、GGUF、benchmark 相关内容，仅靠搜索摘要很容易误判。需要确认模型页和权重文件是否真实存在。

### 10.2 需要抓取字段

```text
model_exists
model_id
author
pipeline_tag
tags
license
likes
downloads
last_modified
card_exists
files_count
has_safetensors
has_gguf
has_config
is_gated
```

### 10.3 正负信号

强正信号：

```text
1. 模型页存在。
2. 有模型卡。
3. 有实际权重文件。
4. 有 license。
5. 最近更新。
6. 与 claim 中模型名一致。
```

负信号：

```text
1. 模型页不存在。
2. 只有模型卡，没有权重。
3. claim 说开源权重，但页面是 gated 或无文件。
4. 模型名不匹配。
```

---

## 11. P1：evidence-classify 规则层

### 11.1 目标

在 AI verify 前先用 deterministic rules 给证据打标签，减少模型负担和随机性。

新增脚本：

```bash
python scripts/run_evidence_classify_once.py --limit 100
```

### 11.2 分类输出

```text
supports_claim = support | contradict | neutral | unknown
evidence_confidence = 0-100
risk_flags = [...]
quality_flags = [...]
```

### 11.3 示例规则

GitHub：

```text
repo_exists=false → contradict, confidence=90, risk=broken_github_repo
readme_exists=true + install keyword → support, confidence+=20
license missing + claim says open source → contradict/weak, risk=no_license
archived=true → neutral/contradict, risk=archived_repo
```

Hugging Face：

```text
model_exists=false → contradict
has weights + license → support
claim says open weights but is_gated=true → contradict or weak support
```

普通网页：

```text
status 404 → contradict
official domain + title/body contains entity_name → support
Product Hunt only → neutral unless有官网/GitHub外链
forum only → neutral
```

---

## 12. P1：Tavily 查询缓存

### 12.1 目的

避免重复搜索，降低 API 成本，提高可重跑性。

### 12.2 新增表：`search_cache_items`

```python
class SearchCacheItem(Base):
    __tablename__ = "search_cache_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

唯一约束：

```text
provider + query_hash
```

### 12.3 缓存策略

```text
1. 默认 24 小时内复用相同 query。
2. 官方发布类 query 可缩短到 6 小时。
3. GitHub/HF 专用 API 结果可缓存 1-6 小时。
4. 失败结果短缓存，例如 10 分钟，避免连续打爆 API。
```

---

## 13. P1：recommendation-export 和 audit-export 分离

### 13.1 当前问题

当前 `recommendation-export` 会按 final_keep 排序，但仍可能导出 rejected 或 D 档内容。这适合内部审阅，但不适合面向用户推荐。

### 13.2 建议拆分

```bash
python -m app.main recommendation-export --limit 20
python -m app.main audit-export --limit 100
```

行为：

```text
recommendation-export:
  只导出 final_keep=true 的内容。
  默认不显示 D 档和高风险内容。

audit-export:
  导出所有 verification_items。
  按今日强推荐、值得关注、仅归档、被剔除内容分区。
```

### 13.3 推荐导出筛选条件

默认：

```text
final_keep = true
final_score >= FINAL_REVIEW_MIN_SCORE
credibility_score >= FINAL_REVIEW_MIN_CREDIBILITY
spam_risk_score <= FINAL_REVIEW_MAX_SPAM_RISK
recommendation_level in ('S', 'A', 'B')
```

---

## 14. P2：canonical entity 聚合

### 14.1 为什么需要

同一个工具可能从多个来源进入系统：

```text
Product Hunt
X / RSSHub
Reddit
LINUX DO
GitHub Trending
官方博客
```

如果不聚合，日报会重复推荐同一个工具。

### 14.2 新增表

```python
class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    huggingface_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    producthunt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    best_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
```

```python
class EntityMention(Base):
    __tablename__ = "entity_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("canonical_entities.id"), nullable=False)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    verification_item_id: Mapped[int | None] = mapped_column(ForeignKey("verification_items.id"), nullable=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mention_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

### 14.3 强匹配规则

```text
1. GitHub owner/repo 相同。
2. Hugging Face model id 相同。
3. official canonical URL 相同。
4. Product Hunt slug 相同。
```

### 14.4 弱匹配规则

```text
1. normalized_name 相同。
2. entity_name 高度相似。
3. entity_name + domain 相同。
4. title/body embedding 相似。
```

弱匹配不建议自动合并，应进入人工确认或 AI resolve。

---

## 15. P2：source quality 持久化

### 15.1 当前问题

`SourceConfig` 已经支持：

```text
quality_weight
source_role
spam_risk
requires_verification
```

但 Source 表还没有完整持久化这些字段。当前实际 source quality 更多依赖 group 级硬编码。

### 15.2 建议修改 Source 表

```python
class Source(Base):
    source_group: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    source_subtype: Mapped[str] = mapped_column(String(64), nullable=False, default="fixed")
    quality_weight: Mapped[float | None]
    source_role: Mapped[str | None]
    spam_risk: Mapped[str | None]
    requires_verification: Mapped[bool | None]
```

### 15.3 优先级

计算 source quality 时：

```text
1. 优先使用 Source 表中的 source 级配置。
2. 其次使用 source_group 默认配置。
3. 最后 fallback 到 general。
```

---

## 16. P2：运行编排和可观测性

### 16.1 新增 run-daily

新增：

```bash
python -m app.main run-daily
```

执行：

```text
fetch
→ normalize
→ prefilter
→ ai-review
→ claim-extract
→ evidence-discover
→ evidence-fetch
→ evidence-classify
→ ai-verify
→ recommendation-export
```

### 16.2 新增 pipeline_runs 表

```python
class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int]
    run_type: Mapped[str]
    status: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    stats_json: Mapped[str]
    error: Mapped[str | None]
```

统计字段示例：

```json
{
  "fetched": 120,
  "normalized": 100,
  "prefilter_kept": 38,
  "ai_review_kept": 20,
  "claims_extracted": 18,
  "evidence_discovered": 96,
  "evidence_fetched": 80,
  "verified": 18,
  "final_kept": 7,
  "exported": 7,
  "tavily_queries": 54,
  "ai_calls": 36,
  "failed": 2
}
```

---

## 17. P3：用户反馈闭环

### 17.1 目的

如果项目最终是“推荐给用户”，需要知道推荐是否真的有用。

### 17.2 新增反馈表

```python
class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id: Mapped[int]
    entity_id: Mapped[int | None]
    candidate_item_id: Mapped[int | None]
    action: Mapped[str]  # like / dislike / save / hide / click / report
    reason: Mapped[str | None]
    created_at: Mapped[datetime]
```

### 17.3 反馈用途

```text
1. 用户经常保存 MCP → 提高 MCP 权重。
2. 用户隐藏 Product Hunt 营销工具 → 降低 Product Hunt 权重。
3. 用户不看 benchmark → 降低 benchmark 类召回。
4. 用户喜欢 Claude Code workflow → 提高相关 query 和 category 权重。
```

---

## 18. 代码实现优先级

### P0：稳定 Tavily evidence 链路

```text
1. ExtractedClaim 增加 evidence_status / attempts / error。
2. evidence-search 改为状态机，不再用是否存在 evidence_items 判断完成。
3. 拆分 retrieval_score 和 evidence_confidence。
4. direct URL 先标记为 unchecked，不直接高可信。
5. recommendation-export 默认只导出 final_keep=true。
```

### P1：把“搜索结果”升级为“证据”

```text
1. 新增 evidence-fetch。
2. 新增 evidence-classify。
3. 新增 GitHub verifier。
4. 新增 Hugging Face verifier。
5. 新增 Tavily search cache。
```

### P2：减少重复和提升推荐质量

```text
1. 新增 canonical_entities。
2. 新增 entity_mentions。
3. source quality 持久化。
4. 推荐日报按 entity 输出。
5. 增加 run-daily 和 pipeline_runs。
```

### P3：产品化和个性化

```text
1. 用户反馈。
2. 多频道推荐。
3. 动态 source quality。
4. Telegram / Notion / Web UI 输出。
```

---

## 19. 测试建议

### 19.1 新增单元测试

```text
1. evidence_status 状态流转测试。
2. Tavily 某个 query 失败后可重试测试。
3. direct URL 404 被标记为 broken_primary_link。
4. GitHub 空仓库被标记为 empty_repository。
5. claim URL 幻觉被标记为 hallucinated_url。
6. evidence_confidence 和 retrieval_score 分离测试。
7. recommendation-export 默认只输出 final_keep=true。
8. audit-export 输出 rejected / D 档内容。
```

### 19.2 集成测试样例

准备固定样例：

```text
1. 真实 GitHub MCP server。
2. GitHub repo 不存在。
3. 空 GitHub repo。
4. 声称开源但无 license。
5. Hugging Face 模型存在且有权重。
6. Hugging Face 模型页存在但无权重。
7. Product Hunt 营销页，无官网/仓库。
8. Reddit 求推荐帖。
9. 官方博客模型发布。
10. 旧工具重复转载。
```

### 19.3 质量指标

```text
precision@10：Top 10 推荐人工接受率
duplicate_rate：重复实体比例
spam_rate：垃圾/营销内容比例
unverified_rate：无有效证据但进入推荐区的比例
broken_link_rate：推荐内容中的坏链比例
manual_accept_rate：人工审阅通过率
```

目标：

```text
precision@10 >= 80%
duplicate_rate <= 10%
spam_rate <= 10%
unverified_rate <= 15%
broken_link_rate <= 5%
manual_accept_rate >= 70%
```

---

## 20. 推荐的最终形态

长期来看，系统应形成三层判断：

```text
1. 召回层
   RSS / RSSHub / Reddit / Product Hunt / GitHub Trending / Tavily discovery

2. 核实层
   direct URL fetch / GitHub verifier / Hugging Face verifier / evidence classify / AI verify

3. 推荐层
   canonical entity / 多维评分 / 用户反馈 / 日报或推送
```

最终推荐不应该基于单条 RSS，而应基于 canonical entity：

```text
一个工具实体
+ 多个来源 mention
+ 多条证据
+ 多维评分
+ 用户偏好
= 是否推荐
```

---

## 21. 总结

当前 Tavily 接入是正确方向，但它应该被定位为：

```text
证据发现工具，而不是事实裁判。
```

下一步最重要的是把 evidence 链路从：

```text
Tavily 搜索结果 → AI verify
```

升级为：

```text
Tavily / direct URL 发现
→ URL 真实抓取
→ GitHub / HF 专用验证
→ 规则化 support/contradict 分类
→ AI 基于证据综合判断
→ 本地 final_score 强约束
```

优先完成 P0 和 P1 后，项目会从“能搜索辅助判断”升级为“具有可审计证据链的 AI 工具情报推荐系统”。