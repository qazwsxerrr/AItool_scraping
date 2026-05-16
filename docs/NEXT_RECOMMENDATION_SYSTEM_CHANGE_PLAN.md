# 下一阶段改动方案：证据链稳定性与推荐系统强化

## 1. 背景

当前项目已经形成较完整的 AI 工具情报推荐链路：

```text
fetch
→ normalize
→ prefilter
→ ai-review
→ claim-extract
→ evidence-search
→ evidence-fetch
→ evidence-classify
→ claim-verify
→ ai-verify
→ entity-resolve
→ recommendation-write
→ recommendation-export
```

并已经加入：

```text
1. claim-level verification
2. freshness_score
3. entity update detection
4. recommendation_writer
5. feedback-based rerank
6. Tavily search cache
7. GitHub / Hugging Face special verifier
8. source quality 持久化
9. run-daily 和 pipeline_runs
```

当前系统已经不再是简单 RSS 聚合器，而是一个“证据辅助的 AI 工具情报推荐系统”。下一阶段的重点不应该继续堆新功能，而应该提升以下能力：

```text
1. 证据变化后，下游结果能自动失效并重算。
2. 规则层发现强反证时，AI 不能绕过本地强约束。
3. claim 级支持要更精确，避免“实体存在”被误当作“每条 claim 都成立”。
4. 同一工具的更新要保留事件历史，而不是只覆盖 entity 当前状态。
5. 推荐卡片要区分给用户看的卡片和给开发者审阅的卡片。
6. feedback rerank 要逐步支持用户级和时间衰减。
```

---

## 2. 下一阶段核心目标

后续改动目标：

```text
P0：让证据链更可靠
P1：让实体更新判断更准确
P2：让推荐结果更可解释
P3：让反馈排序更可控
P4：让日常运行更容易调试和复盘
```

最终希望形成：

```text
候选内容
→ 证据发现
→ 证据抓取
→ 证据分类
→ claim 精细核实
→ 本地确定性强约束
→ AI 综合判断
→ entity 聚合
→ update event 记录
→ 推荐卡片生成
→ feedback rerank
→ 推荐快照保存
```

---

## 3. P0：增加下游失效与重算机制

### 3.1 当前问题

当前很多阶段是一次性插入：

```text
claim_verify：如果已有 ClaimVerificationItem，就不再处理
ai_verify：如果已有 VerificationItem，就不再处理
recommendation_write：如果已有 RecommendationCard，就不再处理
```

这会导致一个问题：

```text
如果 evidence 后来被重新 fetch 或 classify，
下游的 claim_verification、verification、recommendation_card 不会自动重算。
```

例如：

```text
1. Tavily 搜索到 GitHub URL。
2. 初次 fetch 失败，evidence 被标记 unknown。
3. 后来重新 fetch 成功，并 classify 为 support。
4. 但 ClaimVerificationItem 已经存在，不会重算。
5. VerificationItem 和 RecommendationCard 也仍然基于旧证据。
```

### 3.2 推荐实现方式

给关键表增加状态和版本字段。

#### EvidenceItem 增加

```python
class EvidenceItem(Base):
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    classify_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    classify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_version: Mapped[str] = mapped_column(String(32), nullable=False, default="rules_v1")
```

#### ClaimVerificationItem 增加

```python
class ClaimVerificationItem(Base):
    verification_version: Mapped[str] = mapped_column(String(32), nullable=False, default="claim_rules_v1")
    source_evidence_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

#### VerificationItem 增加

```python
class VerificationItem(Base):
    verification_version: Mapped[str] = mapped_column(String(32), nullable=False, default="ai_verify_v1")
    source_claim_verification_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

#### RecommendationCard 增加

```python
class RecommendationCard(Base):
    writer_version: Mapped[str] = mapped_column(String(32), nullable=False, default="rule_writer_v1")
    source_verification_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

### 3.3 失效规则

```text
EvidenceItem.updated_at > ClaimVerificationItem.source_evidence_updated_at
→ ClaimVerificationItem stale=true

ClaimVerificationItem.created_at / updated_at > VerificationItem.source_claim_verification_updated_at
→ VerificationItem stale=true

VerificationItem.created_at / updated_at > RecommendationCard.source_verification_updated_at
→ RecommendationCard stale=true
```

### 3.4 查询逻辑调整

`list_pending_claims` 改成：

```text
没有 ClaimVerificationItem
OR ClaimVerificationItem.stale = true
OR evidence 比 claim verification 更新
```

`list_pending_for_ai_verify` 改成：

```text
没有 VerificationItem
OR VerificationItem.stale = true
OR claim verification 比 ai verification 更新
```

`list_pending_for_write` 改成：

```text
没有 RecommendationCard
OR RecommendationCard.stale = true
OR VerificationItem 比 RecommendationCard 更新
```

### 3.5 CLI 建议

新增参数：

```bash
python -m app.main claim-verify --force
python -m app.main ai-verify --force
python -m app.main recommendation-write --force
python -m app.main invalidate-downstream --from evidence
```

### 3.6 验收标准

```text
1. evidence 重新 classify 后，claim_verify 会重新运行。
2. claim_verification 变化后，ai_verify 会重新运行。
3. ai_verify 变化后，recommendation_write 会重新运行。
4. --force 可以强制重算指定阶段。
5. 重算不会产生重复行，而是 update 或 replace 原有行。
```

---

## 4. P0：evidence-classify 增加状态字段

### 4.1 当前问题

当前 `evidence-classify` 主要按：

```text
fetch_status == completed
```

选取 evidence。这样已经 classify 过的 evidence 下次还会被重复处理。

### 4.2 改动建议

给 `EvidenceItem` 增加：

```python
classify_status: str = "pending"  # pending | completed | failed
classified_at: datetime | None
classify_error: str | None
classification_version: str = "rules_v1"
```

### 4.3 Job 行为

```python
try:
    classification = classify_evidence(evidence)
    repo.update_classification(...)
    evidence.classify_status = "completed"
    evidence.classified_at = utcnow()
    evidence.classify_error = None
except Exception as exc:
    evidence.classify_status = "failed"
    evidence.classify_error = str(exc)
```

### 4.4 fetch 后重置 classify

当 `update_fetch_result` 写入新抓取结果时：

```text
fetch_status = completed
classify_status = pending
classified_at = None
classify_error = None
```

### 4.5 验收标准

```text
1. 已 classify 的 evidence 不会重复 classify。
2. evidence 重新 fetch 后会重新 classify。
3. classify 失败后可以重试。
4. audit export 能显示 classify_status。
```

---

## 5. P0：finalize_verification 加确定性强约束

### 5.1 当前问题

当前 AI verify 阶段已经把 evidence_items 和 claim_verifications 传给模型，但 finalizer 主要依赖模型返回的 score 和 risk_flags。

如果规则层已经发现强反证，但模型没有正确写入 risk_flags，本地 finalizer 未必会强制拦截。

### 5.2 新增 Evidence/Claim 摘要统计

在 `run_ai_verify_job` 中计算：

```python
@dataclass(frozen=True)
class EvidenceGuardStats:
    support_evidence_count: int
    contradict_evidence_count: int
    high_confidence_contradict_count: int
    supported_claim_count: int
    contradicted_claim_count: int
    unknown_claim_count: int
    neutral_claim_count: int
    broken_primary_link_count: int
    broken_github_count: int
    broken_huggingface_count: int
```

### 5.3 finalizer 增加参数

```python
def finalize_verification(
    response: AIVerifyResponse,
    *,
    evidence_count: int,
    guard_stats: EvidenceGuardStats,
    min_score: int = 75,
    min_credibility: int = 60,
    max_spam_risk: int = 40,
) -> FinalVerification:
    ...
```

### 5.4 强约束规则

```text
规则 1：无支持证据
support_evidence_count == 0
→ credibility_score <= 50
→ final_score <= 65
→ final_keep = false

规则 2：存在高置信反证
high_confidence_contradict_count >= 1
→ final_score <= 44
→ final_keep = false
→ risk_flags += high_confidence_contradiction

规则 3：存在 claim 级反证
contradicted_claim_count >= 1
→ final_score <= 59
→ final_keep = false
→ risk_flags += contradicted_claim

规则 4：GitHub / HF broken
broken_github_count >= 1 or broken_huggingface_count >= 1
→ final_score <= 44
→ final_keep = false
→ risk_flags += broken_primary_artifact

规则 5：全部 claim unknown
supported_claim_count == 0 and contradicted_claim_count == 0
→ final_score <= 65
→ recommendation_level <= B
```

### 5.5 验收标准

```text
1. broken GitHub repo 不会被 AI verify 误放进推荐区。
2. no support evidence 的候选不会 final_keep=true。
3. claim-level contradict 会强制降档。
4. 模型输出高分也不能绕过 deterministic guard。
```

---

## 6. P0：claim-level verification 区分 direct support 和 entity-only support

### 6.1 当前问题

当前 claim verification 会从 support evidence 中找匹配项。如果没有 term-specific match，可能 fallback 到全部 support evidence。

这会导致：

```text
证据证明“这个工具存在”
被误用来支持“这个工具支持 Claude Code / open weights / OpenAI-compatible API”。
```

### 6.2 新增字段

`ClaimVerificationItem` 增加：

```python
support_strength: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
```

取值：

```text
direct       证据直接支持该 claim
entity_only  证据只证明实体存在，不证明具体 claim
weak         弱支持，需要人工复核
none         无支持
```

### 6.3 规则建议

```text
MCP claim：证据需出现 mcp / server / config / install 等关键词。
安装 claim：证据需出现 install / usage / quickstart / pip / npm / docker / 配置等关键词。
OpenAI-compatible claim：证据需出现 OpenAI-compatible / /v1/chat/completions / base_url / API key 等关键词。
开源权重 claim：HF/GitHub 需存在真实 weights 文件，不能只看标题。
Claude Code claim：证据需出现 Claude Code / claude-code / config / workflow 等关键词。
```

### 6.4 输出示例

```json
{
  "claim_text": "支持 OpenAI-compatible API",
  "supports_claim": "support",
  "support_strength": "direct",
  "evidence_item_ids": [12, 13],
  "confidence": 88,
  "risk_flags": []
}
```

### 6.5 验收标准

```text
1. 只证明实体存在的 evidence 不会强支持所有 claim。
2. open weights 必须由 HF/GitHub 文件证据支持。
3. install claim 必须匹配安装或使用说明。
4. support_strength 会进入 AI verify request 和 recommendation export。
```

---

## 7. P1：新增 EntityUpdateEvent，记录更新历史

### 7.1 当前问题

当前 `CanonicalEntity` 有：

```text
major_update_detected
last_update_reason
last_recommended_at
```

这只能表达当前聚合状态，无法记录每次出现为什么被认为是新工具、旧闻、重大更新或普通重复 mention。

### 7.2 新增表

```python
class EntityUpdateEvent(Base):
    __tablename__ = "entity_update_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("canonical_entities.id"), nullable=False)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    verification_item_id: Mapped[int | None] = mapped_column(ForeignKey("verification_items.id"), nullable=True)

    update_type: Mapped[str] = mapped_column(String(64), nullable=False)
    update_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_item_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
```

`update_type` 取值：

```text
new_entity
major_release
minor_update
repeated_mention
stale_duplicate
reactivated
```

### 7.3 更新判断规则

```text
new_entity：第一次出现该 canonical entity。
major_release：官方发布、GitHub release、HF 权重更新、版本号变化、重大功能词出现。
minor_update：GitHub pushed_at 新，但无 release / changelog / 新功能证据。
repeated_mention：只是社区重复讨论。
stale_duplicate：旧内容重复转载，且无新证据。
reactivated：长期未出现后重新活跃，且有实质证据。
```

### 7.4 不建议只靠 pushed_at

`pushed_at` 最近只说明仓库有改动，不一定说明发生重大更新。

更强的更新证据：

```text
1. GitHub latest release 时间。
2. GitHub tag 新增。
3. README / changelog 出现新版本。
4. HF 文件 last_modified 新，且权重文件变化。
5. 官方博客或 release note 时间新。
6. RSS 标题包含 v2 / launch / release / major / new feature 等。
```

### 7.5 验收标准

```text
1. 新 entity 会创建 new_entity event。
2. 旧 entity 重复 mention 不会误判为重大更新。
3. GitHub release / HF 权重更新会产生 major_release。
4. recommendation export 能显示 update_type 和 update_reason。
5. entity 历史更新可查询。
```

---

## 8. P1：推荐卡片拆分为 user_card 和 audit_card

### 8.1 当前问题

当前 `RecommendationCard` 既包含给用户看的内容，又包含证据域名、风险标签、claim 数量等审阅信息。

这对内部调试有用，但面向用户时可能信息过载。

### 8.2 推荐结构

可以保留 `RecommendationCard`，但增加字段：

```python
card_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
```

或拆成两张表：

```text
recommendation_cards
recommendation_audit_cards
```

### 8.3 user_card 内容

```json
{
  "title": "工具名",
  "one_liner": "一句话说明它解决什么问题",
  "why_recommend": [
    "有 GitHub / 官网 / 文档证据",
    "关键 claim 已被支持",
    "适合某类使用场景"
  ],
  "how_to_try": "优先查看 GitHub README / Docs",
  "risk_note": "社区反馈较少，建议实际试用",
  "links": {}
}
```

### 8.4 audit_card 内容

```json
{
  "evidence_status": "completed",
  "claim_verifications": [],
  "evidence_domains": [],
  "risk_flags": [],
  "quality_flags": [],
  "source_quality": {},
  "rerank_breakdown": {},
  "update_event": {}
}
```

### 8.5 writer 规则

user_card 应优先从 claim-level verification 生成：

```text
支持的 claim → 写入推荐理由
unknown 的 claim → 不写成已确认事实
contradict 的 claim → 写入风险提示或直接不推荐
```

### 8.6 验收标准

```text
1. 用户卡片不暴露过多内部字段。
2. 审阅卡片保留完整证据链信息。
3. unsupported claim 不会出现在推荐理由中。
4. recommendation-export 默认使用 user_card。
5. audit-export 使用 audit_card。
```

---

## 9. P1：推荐排序保存快照

### 9.1 当前问题

当前 rerank_score 是在 export 时动态计算：

```text
final_score + feedback_adjustment + freshness_bonus + update_bonus
```

这能排序，但不方便复盘。后续反馈变化后，无法知道某次日报当时为什么这样排序。

### 9.2 新增表

```python
class RecommendationRankSnapshot(Base):
    __tablename__ = "recommendation_rank_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_entities.id"), nullable=True)
    verification_item_id: Mapped[int] = mapped_column(ForeignKey("verification_items.id"), nullable=False)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)

    final_score: Mapped[int]
    freshness_score: Mapped[int]
    feedback_adjustment: Mapped[int]
    freshness_bonus: Mapped[int]
    update_bonus: Mapped[int]
    rerank_score: Mapped[int]
    rank_position: Mapped[int | None]
    selected: Mapped[bool]
    reason_json: Mapped[str]
    created_at: Mapped[datetime]
```

### 9.3 作用

```text
1. 复盘某条为什么排到前面。
2. 对比不同日期推荐质量。
3. 统计 Top10 precision。
4. 记录 feedback 当时的影响。
5. 支持后续学习排序模型。
```

### 9.4 验收标准

```text
1. 每次 recommendation-export 都保存 rank snapshot。
2. snapshot 中有完整分数组成。
3. 可以查询某次 run 的 Top N 推荐。
4. 可以比较 rerank 前后顺序变化。
```

---

## 10. P2：feedback rerank 增加 user_id 和时间衰减

### 10.1 当前问题

当前 feedback 是全局反馈，没有 user_id。适合个人使用，但不适合多用户或多偏好场景。

同时反馈没有时间衰减，较久之前的 hide/report/like 会长期影响排序。

### 10.2 UserFeedback 增加字段

```python
class UserFeedback(Base):
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
```

### 10.3 时间衰减

```python
feedback_weight = base_weight * exp(-age_days / half_life_days)
```

建议：

```text
click: half_life = 7 days
like/save: half_life = 30 days
hide: half_life = 30 days
report: 不衰减或半衰期很长
```

### 10.4 action 权重建议

```text
click: +1
like: +4
save: +6
dislike: -6
hide: -12
report: -30
```

### 10.5 分类偏好

后续可以统计：

```text
用户对 mcp / workflow / model_release / api_proxy 的偏好
用户对 source_group 的偏好
用户对 Product Hunt / Reddit / GitHub 的接受率
```

### 10.6 验收标准

```text
1. feedback 可按 user_id 查询。
2. rerank 可以选择 user_id。
3. 旧反馈影响逐渐减弱。
4. report 仍然保持强负反馈。
5. 同一个 entity 的反馈不会无限放大。
```

---

## 11. P2：run-daily 参数化和 dry-run

### 11.1 当前问题

当前 `run-daily` 的各步骤 limit 固定写在代码中，不方便调试和不同规模运行。

### 11.2 Settings 新增配置

```env
DAILY_NORMALIZE_LIMIT=500
DAILY_PREFILTER_LIMIT=500
DAILY_AI_REVIEW_LIMIT=80
DAILY_CLAIM_EXTRACT_LIMIT=80
DAILY_EVIDENCE_SEARCH_LIMIT=50
DAILY_EVIDENCE_FETCH_LIMIT=80
DAILY_EVIDENCE_CLASSIFY_LIMIT=120
DAILY_CLAIM_VERIFY_LIMIT=120
DAILY_AI_VERIFY_LIMIT=50
DAILY_ENTITY_RESOLVE_LIMIT=100
DAILY_RECOMMENDATION_WRITE_LIMIT=100
DAILY_RECOMMENDATION_EXPORT_LIMIT=20
```

### 11.3 CLI 参数

```bash
python -m app.main run-daily --dry-run
python -m app.main run-daily --skip-fetch
python -m app.main run-daily --only evidence-fetch
python -m app.main run-daily --from-step evidence-search
python -m app.main run-daily --to-step recommendation-export
```

### 11.4 dry-run 输出

```text
将要执行的步骤
每步 limit
需要的 API key 是否配置
当前 pending item 数量
预计 AI calls / Tavily calls
预计输出文件路径
```

### 11.5 验收标准

```text
1. 可以只运行某个阶段。
2. 可以从某个阶段继续运行。
3. dry-run 不修改数据库。
4. pipeline_runs 记录每步 stats。
5. 失败时能看到失败阶段和错误原因。
```

---

## 12. P2：GitHub / Hugging Face verifier 继续增强

### 12.1 GitHub verifier 增强

新增能力：

```text
1. 支持 GITHUB_TOKEN，避免匿名 rate limit。
2. 处理 403 / 429 / rate limit，不直接当作 repo broken。
3. 获取 latest release。
4. 获取 tags。
5. 获取 repo size，识别空仓库。
6. 获取 languages endpoint。
7. 检测关键文件：pyproject.toml、package.json、Dockerfile、smithery.yaml、mcp.json、README、LICENSE。
8. 识别 awesome-list / curated-list / paper-list，避免误判为工具。
```

新增 raw_payload 字段：

```json
{
  "latest_release_at": "...",
  "tag_count": 10,
  "repo_size": 123,
  "key_files": ["README.md", "pyproject.toml"],
  "is_list_repo": false,
  "rate_limited": false
}
```

### 12.2 Hugging Face verifier 增强

新增能力：

```text
1. 区分 model / dataset / space。
2. 统计权重文件数量和类型。
3. 统计权重文件大小。
4. 区分 gated model 和 open weights。
5. 识别 GGUF / safetensors / bin / pt。
6. 识别只有模型卡没有权重的占位页。
```

新增 raw_payload 字段：

```json
{
  "repo_type": "model",
  "weight_file_count": 6,
  "weight_file_types": ["safetensors", "gguf"],
  "total_weight_size": 123456789,
  "open_weights": true,
  "placeholder_model": false
}
```

### 12.3 验收标准

```text
1. GitHub rate limit 不被误判为 broken repo。
2. 空仓库能被识别。
3. list repo 不会被误判为工具发布。
4. HF open weights 和 gated model 能区分。
5. 声称 open weights 但没有权重文件会被 contradict。
```

---

## 13. P2：增强 URL fetch 安全性

### 13.1 当前状态

当前 fetcher 已阻止 IP literal 的 private / loopback / link-local，以及 localhost / .local。

### 13.2 仍需增强

普通域名可能解析到内网 IP，当前未做 DNS resolve 检查。

### 13.3 建议实现

```text
1. 对 hostname 做 DNS resolve。
2. 检查解析出的所有 IP 是否 private / loopback / link-local / multicast。
3. 每次 redirect 后重新校验 final_url。
4. 限制最大 redirect 次数。
5. 限制 content-length。
6. 对非文本内容只记录 metadata，不读取大文件。
```

### 13.4 验收标准

```text
1. 域名解析到 127.0.0.1 会被阻止。
2. redirect 到内网地址会被阻止。
3. 超大响应不会被完整读取。
4. PDF / binary 不会被当作 HTML 解析。
```

---

## 14. P3：推荐质量评估指标

### 14.1 新增统计脚本

```bash
python -m app.main metrics-summary --days 7
```

### 14.2 指标

```text
precision@10：Top10 人工接受率
manual_accept_rate：人工接受比例
hide_rate：隐藏比例
report_rate：举报比例
duplicate_rate：重复实体比例
unverified_rate：无支持证据但进入推荐区比例
broken_link_rate：推荐项中坏链比例
claim_support_rate：claim 被支持比例
contradicted_claim_rate：claim 被反证比例
```

### 14.3 数据来源

```text
VerificationItem
ClaimVerificationItem
EvidenceItem
CanonicalEntity
UserFeedback
RecommendationRankSnapshot
```

### 14.4 目标

```text
precision@10 >= 80%
duplicate_rate <= 10%
unverified_rate <= 15%
broken_link_rate <= 5%
report_rate <= 5%
```

---

## 15. 推荐实施顺序

### 第一批：最关键的稳定性改动

```text
1. EvidenceItem 增加 classify_status / classified_at / classify_error。
2. claim_verify / ai_verify / recommendation_write 增加 stale / force / update 机制。
3. finalize_verification 增加 deterministic guard。
4. ClaimVerificationItem 增加 support_strength。
```

这批改完后，证据链不会因为上游变化而产生旧结果。

### 第二批：更新检测和推荐可解释性

```text
1. 新增 EntityUpdateEvent。
2. recommendation_writer 拆分 user_card / audit_card。
3. recommendation_export 保存 RecommendationRankSnapshot。
4. export 中明确显示 new_entity / major_release / repeated_mention。
```

这批改完后，日报能解释“为什么这是新推荐”。

### 第三批：反馈和运行体验

```text
1. UserFeedback 增加 user_id / channel / weight。
2. feedback rerank 加时间衰减。
3. run-daily 参数化。
4. run-daily 支持 dry-run / only / from-step / to-step。
```

这批改完后，系统更适合长期运行。

### 第四批：专项 verifier 增强

```text
1. GitHub verifier 增加 release / tag / repo size / key files / rate limit 处理。
2. Hugging Face verifier 增加 file size / open weights / repo type。
3. URL fetch 增加 DNS 私网检查和 redirect 后二次检查。
```

这批改完后，虚假开源、空仓库、假模型页会更难进入推荐区。

---

## 16. 建议新增测试

### 16.1 失效重算测试

```text
1. evidence classify 结果变化后，claim verification 变 stale。
2. claim verification 变化后，ai verification 变 stale。
3. ai verification 变化后，recommendation card 变 stale。
4. --force 可以重算已有记录。
```

### 16.2 deterministic guard 测试

```text
1. high-confidence contradict 强制 final_keep=false。
2. broken GitHub repo 强制 D 档。
3. no support evidence 不允许 final_keep=true。
4. all unknown claims 限制 final_score。
```

### 16.3 support_strength 测试

```text
1. 实体存在但未提安装方式 → install claim 不得 direct support。
2. HF 有模型卡但无权重 → open weights claim 不得 support。
3. README 出现 install/usage → install claim direct support。
4. README 出现 /v1/chat/completions → OpenAI-compatible direct support。
```

### 16.4 update event 测试

```text
1. 新实体产生 new_entity。
2. 旧实体重复 mention 产生 repeated_mention。
3. GitHub release 新增产生 major_release。
4. 只有 pushed_at 变化产生 minor_update。
```

### 16.5 feedback rerank 测试

```text
1. like/save 提高排序。
2. hide/report 降低排序。
3. 旧反馈衰减。
4. user_id 不同，排序结果不同。
```

---

## 17. 最终目标形态

改完后，系统应从当前的：

```text
证据辅助推荐系统
```

进一步升级为：

```text
可重算、可审计、可解释、可个性化的 AI 工具情报推荐系统
```

核心能力：

```text
1. 每条推荐都能追溯证据。
2. 每条 claim 都知道是否被支持。
3. 强反证无法被模型输出绕过。
4. 旧闻和重大更新能区分。
5. 推荐卡片不会写未证实内容。
6. 用户反馈会影响排序，但不会无限放大。
7. 每次日报排序都可复盘。
```

---

## 18. 总结

下一阶段最重要的不是继续增加信息源，而是让当前证据链闭环更加可靠。

优先改：

```text
1. 失效重算机制
2. evidence classify 状态
3. deterministic guard
4. claim support_strength
5. EntityUpdateEvent
```

这五项完成后，系统的推荐结果会明显更稳定，也更适合长期自动运行。