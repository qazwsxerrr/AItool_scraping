# AI 工具情报系统改进方案

## 1. 背景

当前项目已经实现了一个基础闭环：

```text
RSS / Atom / RSSHub 抓取
→ raw_items 入库
→ normalized_items 标准化
→ candidate_items 规则预筛
→ ai_review_items AI 初筛
→ Markdown / JSONL 人工审阅导出
```

这个流程适合作为低成本召回和初筛层，但还不适合作为最终推荐系统。当前最大问题是：AI 初筛主要基于标题、摘要、正文预览、关键词和来源判断，没有外部搜索核实，也没有证据链。因此系统容易把标题党、营销软文、重复转载、虚假开源、空仓库、社区闲聊等内容误判为高价值信息。

后续目标是将项目从：

```text
RSS 聚合器 + 规则筛选 + AI 初筛
```

升级为：

```text
AI 工具情报采集器 + 外部证据核实器 + 实体聚合器 + 推荐排序系统
```

---

## 2. 总体目标

后续改进重点：

1. 对 RSS 抓取到的信息进行 AI 初筛，但不直接作为最终推荐依据。
2. 对候选内容做外部搜索、页面抓取和证据核实。
3. 剔除虚假信息、标题党、低价值信息、重复内容和垃圾信息。
4. 使用多维评分体系筛出最适合推荐给用户的信息。
5. 对同一个工具、模型、MCP、workflow 或 skill 做实体聚合，避免重复推荐。
6. 为后续 Markdown 日报、Notion、Telegram、Web UI 或 API 输出打基础。

---

## 3. 推荐的新流程

建议升级后的流程：

```text
fetch
→ normalize
→ prefilter
→ ai-review
→ claim-extract
→ evidence-search
→ ai-verify
→ entity-resolve
→ rank
→ export / publish
```

对应数据链路：

```text
raw_items
→ normalized_items
→ candidate_items
→ ai_review_items
→ extracted_claims
→ evidence_items
→ verification_items
→ canonical_entities
→ entity_mentions
→ recommendation_items
```

当前已有的 `raw_items`、`normalized_items`、`candidate_items`、`ai_review_items` 应继续保留。新增层不要替代现有初筛，而是接在 AI 初筛之后。

---

## 4. 核心改进一：claim 抽取层

RSS 标题和摘要通常不完整，系统应先从候选内容中抽取结构化 claim。

例如，RSS 标题：

```text
New MCP server for Claude Code workflow released
```

应抽取为：

```json
{
  "entity_name": "某个 MCP server 名称",
  "entity_type": "mcp",
  "main_claims": [
    "发布了 MCP server",
    "支持 Claude Code workflow",
    "提供安装或配置方式"
  ],
  "official_url": null,
  "github_url": null,
  "release_signal": true,
  "actionable_signal": true,
  "confidence": 70
}
```

### 4.1 新增表：`extracted_claims`

```python
class ExtractedClaim(Base):
    __tablename__ = "extracted_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)

    entity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    official_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    huggingface_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    producthunt_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    claims_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
```

### 4.2 claim 抽取原则

1. 如果无法识别明确工具名、项目名、模型名或对象，降低 confidence。
2. 如果只是泛泛讨论，不生成高置信 claim。
3. 如果出现 GitHub、Hugging Face、Product Hunt、官网链接，单独抽取。
4. 如果标题出现 MCP、workflow、skill、agent，但正文无对应证据，不应高分。

---

## 5. 核心改进二：外部证据核实层

AI 不应只根据 RSS 摘要判断内容价值。应先收集证据，再让 AI 基于证据判断。

需要核实的问题：

1. 这个工具、模型或项目是否真实存在？
2. 是否有官网、GitHub、Hugging Face、文档、Product Hunt 页面？
3. 如果声称开源，是否真的有代码、license、release 或模型权重？
4. 如果声称是 MCP / workflow / skill，是否有安装方式和使用说明？
5. 是否只是营销软文、标题党、转载或重复内容？
6. 是否最近发布或最近更新？
7. 社区反馈是否支持它有实际价值？

### 5.1 新增表：`evidence_items`

```python
class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)

    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    supports_claim: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
```

`suppports_claim` 建议取值：

```text
support | contradict | neutral | unknown
```

`evidence_type` 建议取值：

```text
official_page
github_repo
huggingface_model
producthunt_page
documentation
community_post
search_result
package_registry
unknown
```

### 5.2 证据搜索策略

优先级：

1. RSS 原始 URL。
2. claim 中抽取出的 GitHub / Hugging Face / 官方链接。
3. 搜索：`entity_name + github`。
4. 搜索：`entity_name + documentation`。
5. 搜索：`entity_name + MCP / workflow / skill`。
6. 搜索 Product Hunt、Reddit、LINUX DO、X 等社区补充信号。

### 5.3 证据强弱判断

强证据：

```text
1. 官方网站或官方文档明确介绍该工具/模型/工作流。
2. GitHub 仓库存在，README 完整，有 commit、stars、release 或 issue。
3. Hugging Face 模型页存在，有模型卡、权重文件、license 或下载记录。
4. 文档中有可执行安装步骤、API 示例、MCP 配置或 workflow 文件。
```

弱证据：

```text
1. 只有 Product Hunt 页面。
2. 只有 X 帖子。
3. 只有论坛讨论。
4. 只有转载文章，没有原始来源。
```

负证据：

```text
1. 404 / 仓库不存在。
2. GitHub 仓库为空或只有 README。
3. 声称开源但没有 license / code / weights。
4. 搜索不到工具名或项目名。
5. 多个页面内容高度重复，疑似营销分发。
6. 标题声称发布，但找不到官方发布记录。
```

---

## 6. 核心改进三：AI 证据核实层

新增 AI verification 阶段，让 AI 基于 candidate、claim、evidence_items 输出最终判断。

### 6.1 新增表：`verification_items`

```python
class VerificationItem(Base):
    __tablename__ = "verification_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)

    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_keep: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation_level: Mapped[str] = mapped_column(String(32), nullable=False)

    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usefulness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credibility_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reproducibility_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audience_fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_quality_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spam_risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
```

### 6.2 AI verification 输入

```json
{
  "candidate": {
    "title": "...",
    "url": "...",
    "source_group": "reddit_local_llama",
    "candidate_score": 83,
    "body_preview": "..."
  },
  "extracted_claim": {
    "entity_name": "...",
    "entity_type": "mcp",
    "main_claims": ["..."]
  },
  "evidence_items": [
    {
      "evidence_type": "github_repo",
      "url": "...",
      "title": "...",
      "snippet": "...",
      "supports_claim": "support",
      "confidence": 85
    }
  ]
}
```

### 6.3 AI verification 输出

```json
{
  "verified": true,
  "final_keep": true,
  "final_score": 86,
  "recommendation_level": "A",
  "relevance_score": 90,
  "usefulness_score": 85,
  "credibility_score": 82,
  "novelty_score": 88,
  "reproducibility_score": 75,
  "audience_fit_score": 90,
  "source_quality_score": 78,
  "spam_risk_score": 10,
  "category": "mcp",
  "summary_cn": "这是一个用于 ... 的 MCP server，适合 ...",
  "recommendation_reason": "有 GitHub 仓库和文档，说明较完整，近期发布。",
  "risk_reason": "社区反馈较少，需要后续观察。",
  "evidence_summary": [
    "GitHub 仓库存在且 README 完整",
    "原始 RSS 内容与仓库描述一致",
    "未发现明显虚假宣传"
  ],
  "risk_flags": []
}
```

---

## 7. 最终评分体系

不建议只使用一个 AI 分数。推荐使用多维评分。

| 字段 | 说明 |
|---|---|
| relevance_score | 是否符合 AI 工具、工作流、MCP、skill、API、模型发布等目标 |
| usefulness_score | 是否真的对用户有使用价值 |
| credibility_score | 是否有可靠外部证据支持 |
| novelty_score | 是否新发布、新能力、新工具，而不是旧内容重复出现 |
| reproducibility_score | 是否可复现、可安装、可运行、文档是否明确 |
| audience_fit_score | 是否适合目标用户群体 |
| source_quality_score | 来源质量是否高 |
| spam_risk_score | 垃圾、营销、标题党、虚假、重复的风险 |

第一版推荐公式：

```text
final_score =
  0.20 * relevance_score
+ 0.20 * usefulness_score
+ 0.20 * credibility_score
+ 0.15 * novelty_score
+ 0.10 * reproducibility_score
+ 0.10 * audience_fit_score
+ 0.05 * source_quality_score
- spam_penalty
```

其中：

```text
spam_penalty = max(0, spam_risk_score - 30) * 0.8
```

推荐等级：

```text
S: 90-100，强烈推荐，适合日报顶部
A: 80-89，推荐，适合日报主体
B: 65-79，有价值，但建议人工复核
C: 45-64，弱信号，仅归档
D: 0-44，不推荐
```

最终保留规则第一版：

```text
final_keep = true 条件：
1. final_score >= 75
2. credibility_score >= 60
3. spam_risk_score <= 40
4. evidence_items 数量 >= 1
5. 没有 hard negative flag
```

hard negative flag：

```text
broken_primary_link
fake_open_source_claim
empty_repository
unverifiable_entity
pure_marketing
duplicate_old_news
community_discussion_only
```

---

## 8. 来源质量权重

建议给 `source_registry.yaml` 增加来源质量字段：

```yaml
quality_weight: 0.85
source_role: official | community | launch_platform | social | forum | search | code_hosting
spam_risk: low | medium | high
requires_verification: true
```

示例：

```yaml
- id: openai_news
  source_group: official_blog
  quality_weight: 0.95
  source_role: official
  spam_risk: low
  requires_verification: false

- id: producthunt_feed
  source_group: producthunt
  quality_weight: 0.65
  source_role: launch_platform
  spam_risk: medium
  requires_verification: true

- id: linux_do_hot
  source_group: linux_do
  quality_weight: 0.50
  source_role: forum
  spam_risk: medium
  requires_verification: true

- id: reddit_local_llama_new
  source_group: reddit_local_llama
  quality_weight: 0.55
  source_role: community
  spam_risk: medium
  requires_verification: true

- id: x_search_github_launch
  source_group: x
  quality_weight: 0.45
  source_role: social
  spam_risk: high
  requires_verification: true
```

来源权重用途：

1. 判断是否必须外部核实。
2. 设置初始 source_quality_score。
3. 影响 AI verification 的处理优先级。
4. 无证据时决定降分幅度。

---

## 9. 垃圾和虚假信息过滤

重点剔除：

```text
1. 只有标题，没有真实链接或正文。
2. 只有 Product Hunt / X 宣传，没有官网、文档或仓库。
3. GitHub 仓库为空、无 README、无 license、无最近 commit。
4. 声称开源，但没有实际代码或权重。
5. 声称是 MCP / agent / workflow，但只是蹭关键词。
6. 教程无法复现，只有泛泛介绍。
7. 社区讨论帖，没有明确工具、项目、模型或教程。
8. AI 生成的垃圾列表文章。
9. 重复转载旧工具。
10. 明显广告、返利、推广集合站。
```

风险字段建议：

```json
{
  "is_spam": false,
  "is_clickbait": true,
  "is_affiliate_content": false,
  "is_duplicate_news": false,
  "is_unverifiable": false,
  "is_overclaimed": true,
  "broken_link": false,
  "risk_flags": [
    "overclaimed",
    "weak_evidence"
  ]
}
```

降分规则：

```text
broken_link: -40
unverifiable_entity: -50
empty_github_repo: -45
fake_open_source_claim: -60
pure_marketing: -35
duplicate_old_news: -30
community_discussion_only: -35
weak_evidence: -20
clickbait_title: -15
```

---

## 10. 实体聚合

同一个工具可能来自 Product Hunt、X、Reddit、LINUX DO、GitHub Trending、官方博客等多个来源。如果不聚合，日报会重复推荐。

最终推荐对象应该是实体，而不是单条 RSS。

### 10.1 新增表：`canonical_entities`

```python
class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    huggingface_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    producthunt_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    best_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
```

### 10.2 新增表：`entity_mentions`

```python
class EntityMention(Base):
    __tablename__ = "entity_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("canonical_entities.id"), nullable=False)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mention_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
```

强匹配规则：

```text
1. GitHub repo URL 相同。
2. Hugging Face model URL 相同。
3. 官方 canonical URL 相同。
4. Product Hunt slug 相同。
```

弱匹配规则：

```text
1. 工具名高度相似。
2. 工具名 + 作者相同。
3. 工具名 + 说明语义相似。
4. 多个 URL 指向同一官网。
```

弱匹配需要 AI 或人工确认，不建议自动合并。

---

## 11. 推荐输出设计

最终推荐项建议结构：

```json
{
  "title": "工具名 / 项目名",
  "category": "mcp",
  "score": 86,
  "level": "A",
  "summary_cn": "...",
  "why_recommend": "...",
  "risk_note": "...",
  "links": {
    "official": "...",
    "github": "...",
    "docs": "...",
    "source": "..."
  },
  "evidence_count": 4,
  "first_seen_at": "...",
  "source_mentions": ["Product Hunt", "Reddit", "GitHub"]
}
```

Markdown 日报结构：

```markdown
# AI 工具情报日报 - YYYY-MM-DD

## 今日强推荐

### 1. Example MCP Server

- 分类：MCP
- 推荐分：88 / A
- 一句话：...
- 推荐理由：...
- 风险提示：...
- 链接：GitHub / Docs / 原始来源

## 值得关注

...

## 仅归档

...

## 被剔除的高风险内容

...
```

推荐分类：

```text
1. AI 工具
2. Agent / Workflow
3. MCP / Skill
4. API Proxy / OpenAI-compatible / 2API
5. 模型发布 / 开源权重
6. 教程 / 可复现方案
7. 仅归档观察
```

---

## 12. 新增脚本建议

```bash
python scripts/run_claim_extract_once.py --limit 50
python scripts/run_evidence_search_once.py --limit 30
python scripts/run_ai_verify_once.py --limit 30
python scripts/run_entity_resolve_once.py --limit 100
python scripts/run_recommendation_export_once.py --limit 20
```

对应 CLI：

```bash
python -m app.main claim-extract --limit 50
python -m app.main evidence-search --limit 30
python -m app.main ai-verify --limit 30
python -m app.main entity-resolve --limit 100
python -m app.main recommendation-export --limit 20
python -m app.main run-daily
```

`run-daily` 可串联：

```text
fetch
→ normalize
→ prefilter
→ ai-review
→ claim-extract
→ evidence-search
→ ai-verify
→ entity-resolve
→ recommendation-export
```

---

## 13. 配置项建议

`.env` 建议新增：

```env
ENABLE_CLAIM_EXTRACT=true
ENABLE_EVIDENCE_SEARCH=true
ENABLE_AI_VERIFY=true
ENABLE_ENTITY_RESOLVE=true

EVIDENCE_SEARCH_PROVIDER=serpapi
EVIDENCE_SEARCH_API_KEY=your-key
EVIDENCE_SEARCH_MAX_RESULTS=5
EVIDENCE_FETCH_TIMEOUT_SECONDS=20

AI_VERIFY_API_URL=https://api.deepseek.com
AI_VERIFY_API_KEY=your-key
AI_VERIFY_MODEL=deepseek-v4-flash
AI_VERIFY_API_STYLE=openai_chat
AI_VERIFY_TIMEOUT_SECONDS=60

FINAL_REVIEW_MIN_SCORE=75
FINAL_REVIEW_MAX_SPAM_RISK=40
FINAL_REVIEW_MIN_CREDIBILITY=60
```

---

## 14. 开发优先级

### P0：让最终推荐更可靠

目标：解决“RSS 信息真假难辨、AI 只看摘要打分”的问题。

任务：

```text
1. 新增 extracted_claims 表。
2. 新增 evidence_items 表。
3. 新增 verification_items 表。
4. 新增 claim_extract_job。
5. 新增 evidence_search_job。
6. 新增 ai_verify_job。
7. review_export 支持按 final_score 导出。
```

验收标准：

```text
1. 系统能对候选输出证据列表。
2. final_score 不等同于 candidate_score 或 ai_score。
3. 无证据或弱证据内容会被明显降分。
4. 至少能识别 broken_link、empty_repo、pure_marketing、unverifiable_entity。
```

### P1：减少重复推荐

目标：同一个工具只推荐一次。

任务：

```text
1. 新增 canonical_entities。
2. 新增 entity_mentions。
3. 基于 GitHub / Hugging Face / official URL 做强匹配聚合。
4. 推荐导出基于 entity，而不是 candidate。
```

验收标准：

```text
1. 同一个 GitHub repo 不重复出现在日报中。
2. Product Hunt + Reddit + X 提到同一工具时能合并。
3. 推荐项能显示多个来源证据。
```

### P2：提升推荐质量

目标：从“筛选内容”升级为“推荐内容”。

任务：

```text
1. 增加多维评分。
2. 增加推荐等级 S/A/B/C/D。
3. 增加 source quality 权重。
4. 增加日报 Markdown 模板。
5. 增加分类输出。
```

验收标准：

```text
1. 日报顶部内容明显优于普通候选。
2. S/A 级推荐具有明确证据和推荐理由。
3. C/D 级内容不会进入主推荐区。
```

### P3：用户反馈闭环

目标：让推荐结果逐步贴合用户偏好。

任务：

```text
1. 新增 user_feedback 表。
2. 记录 save / hide / like / dislike / click。
3. 根据反馈调整 audience_fit_score。
4. 支持用户偏好配置。
```

验收标准：

```text
1. 用户隐藏的类型后续权重降低。
2. 用户保存较多的类型后续权重提高。
3. 可以按个人偏好生成推荐。
```

---

## 15. AI verification prompt 草案

```text
你是严格的 AI 工具情报核实器。

你的任务不是根据标题判断内容是否有趣，而是根据证据判断该候选是否值得推荐给用户。

目标保留内容：
1. 有实际使用价值的 AI 工具。
2. Agent workflow、自动化工作流、MCP server/client、skill/skills。
3. OpenAI-compatible API、2API、API proxy、模型调用网关。
4. 明确的新模型、新开源权重、重要产品能力发布。
5. 可复现的教程、部署方案、使用指南。

明确排除内容：
1. 泛 benchmark、纯模型横评。
2. 硬件功耗、VRAM、吞吐调优。
3. 观点讨论、吐槽、求推荐、个人经历。
4. 融资故事、社区公告、抽奖。
5. 没有可复用工具或明确发布对象的内容。
6. 只有营销宣传但无官网、文档、仓库或其他证据的内容。

评分要求：
- 如果 evidence_items 不足，credibility_score 不得高于 50。
- 如果声称开源但没有 GitHub / Hugging Face / license / code / weights，必须降低 credibility_score，并添加 risk_flags。
- 如果只有 Product Hunt 或 X 来源，且没有官网/文档/仓库，final_score 不得高于 65。
- 如果是纯社区讨论，final_keep 必须为 false，除非其中包含明确可复用工具或教程。
- 不允许因为标题中包含 MCP、agent、workflow、LLM、Claude、GPT 等词就给高分。

输出严格 JSON：
{
  "verified": true|false,
  "final_keep": true|false,
  "final_score": 0-100,
  "recommendation_level": "S|A|B|C|D",
  "relevance_score": 0-100,
  "usefulness_score": 0-100,
  "credibility_score": 0-100,
  "novelty_score": 0-100,
  "reproducibility_score": 0-100,
  "audience_fit_score": 0-100,
  "source_quality_score": 0-100,
  "spam_risk_score": 0-100,
  "category": "ai_tool|workflow|mcp|skill|api_proxy|model_release|product_release|tutorial|other",
  "summary_cn": "中文摘要",
  "recommendation_reason": "推荐理由",
  "risk_reason": "风险或不足",
  "evidence_summary": ["证据摘要"],
  "risk_flags": ["风险标签"]
}
```

---

## 16. 测试建议

新增单元测试：

```text
1. claim 抽取 JSON 解析测试。
2. evidence_items 幂等入库测试。
3. verification_items 幂等入库测试。
4. final_score 计算测试。
5. spam penalty 测试。
6. entity strong match 测试。
7. recommendation export 排序测试。
```

准备固定样例：

```text
1. 真实 GitHub 工具发布。
2. 真实 MCP server 发布。
3. 只有 Product Hunt 的营销工具。
4. 空 GitHub 仓库。
5. 声称开源但无 license 的项目。
6. Reddit 求推荐帖。
7. benchmark 讨论帖。
8. 旧工具重复转载。
9. 官方模型发布。
10. 可复现 workflow 教程。
```

质量指标：

```text
precision@10：每日 Top 10 中真正值得推荐的比例
duplicate_rate：推荐结果中的重复实体比例
spam_rate：推荐结果中的垃圾/软文比例
unverified_rate：无证据但进入推荐区的比例
manual_accept_rate：人工审阅接受比例
```

目标：

```text
precision@10 >= 80%
duplicate_rate <= 10%
spam_rate <= 10%
unverified_rate <= 15%
manual_accept_rate >= 70%
```

---

## 17. 最小可行实现方案

如果不想一次性改太多，先做最小版本：

```text
fetch → normalize → prefilter → ai-review → evidence-search → ai-verify → recommendation-export
```

先不做 canonical entity。

最小版本新增表：

```text
evidence_items
verification_items
```

最小版本新增脚本：

```bash
python scripts/run_evidence_search_once.py --limit 30
python scripts/run_ai_verify_once.py --limit 30
python scripts/run_recommendation_export_once.py --limit 20
```

最小版本推荐规则：

```text
进入最终推荐：
1. final_keep = true
2. final_score >= 75
3. credibility_score >= 60
4. spam_risk_score <= 40
```

---

## 18. 推荐实施顺序

```text
1. 先实现 evidence_items。
2. 再实现 AI verification 多维评分。
3. 修改导出逻辑，按 final_score 输出。
4. 增加 source quality 配置。
5. 增加 canonical entity 聚合。
6. 最后做用户反馈和个性化推荐。
```

最高优先级是：

```text
候选内容 → 抽取 claim → 搜索证据 → AI 基于证据核实 → 多维评分 → 最终推荐
```

完成这一步后，系统才能从“看起来相关的信息流”升级为“可以推荐给用户的 AI 工具情报系统”。
