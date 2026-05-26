# AItool_scraping 下一阶段最高优先级开发方向

## 1. 当前判断

当前项目已经不再是简单 RSS 聚合器，而是一个具备以下链路的 AI 工具情报系统：

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

这次新增的 RSSHub 对 X 账号和 X 搜索流的追踪能力，属于“发现层”增强。它能更早发现 AI 工具、模型发布、GitHub 项目、Hugging Face 模型、MCP/Agent 工作流等线索。

但是，X 来源噪声高、时效强、失效风险高，不应该成为系统主干。后续不应继续优先堆更多 X 账号，而应优先增强：

```text
证据链稳定性
claim 级核实准确性
本地强约束
实体更新识别
推荐结果可解释性
长期运行可调试性
```

下一阶段的核心目标是把系统从：

```text
证据辅助的推荐流水线
```

升级为：

```text
可重算、可审计、可解释、可长期运行的 AI 工具情报推荐系统
```

---

## 2. 最高优先级总结

最应该优先做的方向是：

```text
P0-1：增加证据链失效与重算机制
P0-2：给 evidence-classify 增加状态字段，避免重复分类和旧分类污染
P0-3：增强 deterministic guard，让规则层强反证不能被 AI 绕过
P0-4：claim-level verification 增加 support_strength，区分直接支持和仅证明实体存在
P0-5：让 X/RSSHub 来源变成线索源，而不是可信推荐依据
```

这五项完成后，系统的推荐质量会比继续添加信息源提升更明显。

---

## 3. P0-1：证据链失效与重算机制

### 3.1 当前问题

当前多个阶段是一次性插入逻辑：

```text
claim_verify：如果已有 ClaimVerificationItem，就不再处理
ai_verify：如果已有 VerificationItem，就不再处理
recommendation_write：如果已有 RecommendationCard，就不再处理
```

这会导致：

```text
如果 evidence 后续重新 fetch 成功、重新 classify、risk_flags 变化，
下游 claim verification、AI verification、recommendation card 不会自动重算。
```

典型错误链路：

```text
1. Tavily 搜到 GitHub URL。
2. 初次 evidence-fetch 超时，证据被标记 unknown。
3. claim-verify 基于 unknown 生成弱判断。
4. ai-verify 基于旧 evidence 输出低可信结果。
5. 后来 evidence-fetch 成功，GitHub verifier 证明 repo 存在。
6. 但 claim_verify / ai_verify / recommendation_write 不再重跑。
7. 推荐结果仍然基于旧证据。
```

### 3.2 推荐数据字段

#### EvidenceItem 增加

```python
updated_at: datetime
classified_at: datetime | None
classify_status: str = "pending"  # pending | completed | failed
classify_error: str | None
classification_version: str = "rules_v1"
```

#### ClaimVerificationItem 增加

```python
verification_version: str = "claim_rules_v1"
source_evidence_updated_at: datetime | None
stale: bool = False
updated_at: datetime
```

#### VerificationItem 增加

```python
verification_version: str = "ai_verify_v1"
source_claim_verification_updated_at: datetime | None
stale: bool = False
updated_at: datetime
```

#### RecommendationCard 增加

```python
writer_version: str = "recommendation_writer_v1"
source_verification_updated_at: datetime | None
stale: bool = False
updated_at: datetime
```

### 3.3 失效规则

```text
EvidenceItem.updated_at > ClaimVerificationItem.source_evidence_updated_at
→ ClaimVerificationItem.stale = true

ClaimVerificationItem.updated_at > VerificationItem.source_claim_verification_updated_at
→ VerificationItem.stale = true

VerificationItem.updated_at > RecommendationCard.source_verification_updated_at
→ RecommendationCard.stale = true
```

### 3.4 查询逻辑调整

#### claim-verify pending 查询

```text
没有 ClaimVerificationItem
OR ClaimVerificationItem.stale = true
OR evidence 比 claim verification 更新
```

#### ai-verify pending 查询

```text
没有 VerificationItem
OR VerificationItem.stale = true
OR claim verification 比 ai verification 更新
```

#### recommendation-write pending 查询

```text
没有 RecommendationCard
OR RecommendationCard.stale = true
OR VerificationItem 比 RecommendationCard 更新
```

### 3.5 Repository 行为调整

不建议继续只用 `insert_if_new`。需要新增 update/upsert 行为：

```text
ClaimVerificationRepository.upsert(...)
VerificationItemRepository.upsert(...)
RecommendationCardRepository.upsert(...)
```

当发现已有记录但 stale=true 或 force=true 时，应更新原记录，而不是插入重复行。

### 3.6 CLI 建议

新增参数：

```bash
python -m app.main claim-verify --force
python -m app.main ai-verify --force
python -m app.main recommendation-write --force
```

新增命令：

```bash
python -m app.main invalidate-downstream --from evidence
python -m app.main invalidate-downstream --from claim-verification
python -m app.main invalidate-downstream --from verification
```

### 3.7 验收标准

```text
1. evidence 重新 fetch/classify 后，claim_verify 会重新运行。
2. claim verification 更新后，ai_verify 会重新运行。
3. ai verification 更新后，recommendation_write 会重新运行。
4. --force 可以强制重算指定阶段。
5. 重算不会产生重复行，而是 update 原记录。
6. audit-export 能显示 stale/version/source_updated_at 信息。
```

---

## 4. P0-2：evidence-classify 状态机制

### 4.1 当前问题

当前 evidence-classify 如果只按：

```text
fetch_status == completed
```

选择待分类 evidence，会导致已经分类过的 evidence 被重复处理。更严重的是，当 evidence 重新 fetch 后，旧分类可能没有被明确失效。

### 4.2 推荐字段

在 `EvidenceItem` 中增加：

```python
classify_status: str = "pending"  # pending | completed | failed
classified_at: datetime | None
classify_error: str | None
classification_version: str = "rules_v1"
updated_at: datetime
```

### 4.3 evidence-fetch 成功后的行为

当 `update_fetch_result()` 写入新的抓取结果时，如果抓取内容发生变化或重新抓取成功：

```text
fetch_status = completed
classify_status = pending
classified_at = None
classify_error = None
updated_at = now()
```

### 4.4 evidence-classify 行为

只处理：

```text
fetch_status = completed
AND classify_status IN (pending, failed)
```

分类成功：

```text
classify_status = completed
classified_at = now()
classify_error = None
classification_version = current_version
updated_at = now()
```

分类失败：

```text
classify_status = failed
classify_error = str(exc)
updated_at = now()
```

### 4.5 CLI 建议

```bash
python -m app.main evidence-classify --limit 100
python -m app.main evidence-classify --force
python -m app.main evidence-classify --version rules_v2
```

### 4.6 验收标准

```text
1. 已 classify 的 evidence 不会重复 classify。
2. evidence 重新 fetch 后会重新进入 classify pending。
3. classify 失败后可以重试。
4. classification_version 变化后可以批量重算。
5. audit-export 能显示 classify_status / classified_at / classify_error。
```

---

## 5. P0-3：deterministic guard 强约束

### 5.1 当前问题

AI verify 阶段会把 evidence_items 和 claim_verifications 传给模型，但如果模型误判、忽略反证、或没有正确返回 risk_flags，本地 finalizer 可能无法充分拦截。

因此，应把强反证、本地坏链、无支持证据、claim 级反证等情况转成确定性规则。模型可以参与综合判断，但不能绕过规则层。

### 5.2 新增 Guard Stats

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
    entity_only_support_count: int
    direct_support_count: int
```

### 5.3 finalize_verification 参数调整

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

#### 规则 1：无支持证据

```text
support_evidence_count == 0
→ credibility_score <= 50
→ final_score <= 65
→ final_keep = false
→ risk_flags += no_support_evidence
```

#### 规则 2：高置信反证

```text
high_confidence_contradict_count >= 1
→ final_score <= 44
→ final_keep = false
→ risk_flags += high_confidence_contradiction
```

#### 规则 3：claim 级反证

```text
contradicted_claim_count >= 1
→ final_score <= 59
→ final_keep = false
→ risk_flags += contradicted_claim
```

#### 规则 4：GitHub / Hugging Face 主证据损坏

```text
broken_github_count >= 1 OR broken_huggingface_count >= 1
→ final_score <= 44
→ final_keep = false
→ risk_flags += broken_primary_artifact
```

#### 规则 5：全部 claim unknown

```text
supported_claim_count == 0 AND contradicted_claim_count == 0
→ final_score <= 65
→ recommendation_level <= B
→ risk_flags += all_claims_unknown
```

#### 规则 6：只有 entity-only support

```text
direct_support_count == 0 AND entity_only_support_count > 0
→ credibility_score <= 60
→ final_score <= 70
→ recommendation_level <= B
→ risk_flags += entity_only_support
```

### 5.5 验收标准

```text
1. broken GitHub repo 不会被 AI verify 误放进推荐区。
2. no support evidence 的候选不会 final_keep=true。
3. claim-level contradict 会强制降档。
4. 只有 entity-only support 时不会被写成强推荐。
5. 模型输出高分也不能绕过 deterministic guard。
6. audit-export 能展示 guard_stats 和最终拦截原因。
```

---

## 6. P0-4：claim support_strength

### 6.1 当前问题

当前 claim-level verification 存在一个风险：

```text
证据证明“这个工具存在”
被误当作支持“这个工具支持某个具体功能 claim”
```

例如：

```text
claim: 支持 OpenAI-compatible API
证据: GitHub repo 存在，有 README
错误结论: support
正确结论: entity_only 或 weak
```

### 6.2 新增字段

在 `ClaimVerificationItem` 增加：

```python
support_strength: str = "none"
```

取值：

```text
direct       证据直接支持该 claim
entity_only  证据只证明实体存在，不证明具体 claim
weak         弱支持，需要人工复核
none         无支持
```

### 6.3 规则示例

#### MCP claim

必须出现以下证据之一：

```text
mcp
model context protocol
server
client
config
install
smithery
mcp.json
```

#### OpenAI-compatible claim

必须出现以下证据之一：

```text
OpenAI-compatible
OpenAI compatible
/v1/chat/completions
base_url
api_key
OpenAI SDK
compatible with OpenAI API
```

#### install / usage claim

必须出现以下证据之一：

```text
install
usage
quickstart
pip install
npm install
docker run
configuration
setup
```

#### open weights claim

必须由 Hugging Face / GitHub 文件证据支持，例如：

```text
.safetensors
.gguf
.bin
.pt
model weights
weight files
```

仅有模型卡或标题不能算 direct support。

#### Claude Code workflow claim

必须出现：

```text
Claude Code
claude-code
settings.json
commands
agent workflow
slash command
```

### 6.4 claim verify 输出示例

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

只证明实体存在时：

```json
{
  "claim_text": "支持 OpenAI-compatible API",
  "supports_claim": "neutral",
  "support_strength": "entity_only",
  "evidence_item_ids": [12],
  "confidence": 45,
  "risk_flags": ["entity_only_support"]
}
```

### 6.5 对 AI verify 的影响

AI verify request 中应加入：

```text
claim_text
supports_claim
support_strength
evidence_item_ids
confidence
risk_flags
```

并在 prompt 中明确：

```text
unsupported / entity_only 的 claim 不能写进推荐理由。
只有 direct support 的 claim 可以作为确定事实写入推荐卡片。
```

### 6.6 验收标准

```text
1. 只证明实体存在的 evidence 不会强支持所有 claim。
2. open weights 必须由 HF/GitHub 文件证据支持。
3. install claim 必须匹配安装或使用说明。
4. OpenAI-compatible claim 必须匹配 API 兼容性证据。
5. support_strength 会进入 AI verify request。
6. recommendation-export 和 audit-export 能显示 support_strength。
```

---

## 7. P0-5：X/RSSHub 来源的正确定位

### 7.1 定位

X/RSSHub 来源应定位为：

```text
早期发现层 / lead generation layer
```

不应定位为：

```text
事实证据层 / final recommendation evidence
```

也就是说，X 可以触发候选进入系统，但不能单独支撑最终推荐。

### 7.2 X 来源质量分层

#### 官方账号流

例如：

```text
OpenAI
Anthropic
GoogleDeepMind
MistralAI
DeepSeek
Alibaba_Qwen
huggingface
ollama
replicate
```

建议：

```yaml
source_role: official
quality_weight: 0.75
spam_risk: medium
requires_verification: true
```

即使是官方 X，也仍然需要官网、文档、GitHub、HF 等证据补强。

#### X 搜索流

例如：

```text
url:github.com + launch/released/open source
url:huggingface.co + model/space/dataset/weights
aagent/MCP/workflow + github/release/tool
```

建议：

```yaml
source_role: social_search
quality_weight: 0.45
spam_risk: high
requires_verification: true
```

#### 个人账号流

例如创始人、开发者、工具作者账号。

建议：

```yaml
source_role: social
quality_weight: 0.35
spam_risk: high
requires_verification: true
```

### 7.3 推荐抓取策略

不要无限扩大 X 账号列表。优先保留：

```text
1. 官方账号
2. 高质量搜索流
3. 明确能带 GitHub/HF/官网链接的搜索流
```

推荐搜索关键词约束：

```text
-is:retweet
-is:reply
url:github.com
url:huggingface.co
launch
released
introducing
open source
MCP
agent workflow
```

不建议单独搜索：

```text
AI
LLM
ChatGPT
Claude
model
```

这些词过泛，噪声很高。

### 7.4 X 进入推荐区的硬条件

来自 X 的候选如果要进入最终推荐，应至少满足：

```text
1. 有 GitHub / Hugging Face / 官网 / 文档 / Product Hunt 等至少一个外部证据。
2. evidence-fetch 成功。
3. evidence-classify 至少有一个 support。
4. claim-verify 至少有一个 direct support。
5. 没有 high-confidence contradict。
6. deterministic guard 没有拦截。
```

---

## 8. 第一批开发任务拆分

### Task 01：EvidenceItem 分类状态

改动文件建议：

```text
app/storage/models.py
app/storage/db.py
app/storage/repository.py
app/jobs/evidence_fetch_job.py
app/jobs/evidence_classify_job.py
tests/test_evidence_classify_status.py
```

完成内容：

```text
1. 增加 classify_status / classified_at / classify_error / classification_version / updated_at。
2. evidence-fetch 成功后重置 classify_status=pending。
3. evidence-classify 只处理 pending/failed。
4. evidence-classify 成功后标记 completed。
5. 增加 --force 参数。
```

验收：

```bash
python -m pytest tests/test_evidence_classify_status.py
```

---

### Task 02：下游 stale / force / upsert 机制

改动文件建议：

```text
app/storage/models.py
app/storage/db.py
app/storage/repository.py
app/jobs/claim_verify_job.py
app/jobs/ai_verify_job.py
app/jobs/recommendation_write_job.py
app/main.py
tests/test_downstream_invalidation.py
```

完成内容：

```text
1. ClaimVerificationItem 增加 stale/version/source_evidence_updated_at/updated_at。
2. VerificationItem 增加 stale/version/source_claim_verification_updated_at/updated_at。
3. RecommendationCard 增加 stale/version/source_verification_updated_at/updated_at。
4. pending 查询支持 stale。
5. job 支持 --force。
6. repository 支持 upsert。
```

验收：

```bash
python -m pytest tests/test_downstream_invalidation.py
```

---

### Task 03：deterministic guard

改动文件建议：

```text
app/pipeline/verification.py
app/jobs/ai_verify_job.py
app/storage/repository.py
tests/test_verification_guard.py
```

完成内容：

```text
1. 增加 EvidenceGuardStats。
2. ai_verify_job 中计算 guard_stats。
3. finalize_verification 接收 guard_stats。
4. 实现 no_support / high_confidence_contradict / contradicted_claim / broken_primary_artifact 等强约束。
5. audit-export 显示 guard_stats。
```

验收：

```bash
python -m pytest tests/test_verification_guard.py
```

---

### Task 04：support_strength

改动文件建议：

```text
app/storage/models.py
app/storage/db.py
app/pipeline/claim_verification.py
app/storage/repository.py
app/jobs/ai_verify_job.py
app/jobs/recommendation_export_job.py
tests/test_claim_support_strength.py
```

完成内容：

```text
1. ClaimVerificationItem 增加 support_strength。
2. claim_verification 规则区分 direct / entity_only / weak / none。
3. AI verify request 加入 support_strength。
4. recommendation writer 不把 entity_only 写成确定事实。
5. audit-export 显示 support_strength。
```

验收：

```bash
python -m pytest tests/test_claim_support_strength.py
```

---

## 9. 第二批开发任务

第一批完成后，再做以下任务。

### Task 05：EntityUpdateEvent

目标：区分新实体、重大更新、轻微更新、重复 mention、旧闻重复。

新增表：

```python
class EntityUpdateEvent(Base):
    id: int
    entity_id: int
    candidate_item_id: int
    verification_item_id: int | None
    update_type: str
    update_reason: str | None
    previous_seen_at: datetime | None
    current_seen_at: datetime | None
    score_delta: int | None
    evidence_item_ids_json: str
    raw_payload: str
    created_at: datetime
```

`update_type`：

```text
new_entity
major_release
minor_update
repeated_mention
stale_duplicate
reactivated
```

验收：

```text
1. 新实体产生 new_entity。
2. 旧实体重复 mention 产生 repeated_mention。
3. GitHub release / HF 权重更新产生 major_release。
4. 只有 pushed_at 变化最多产生 minor_update。
5. recommendation-export 能显示 update_type。
```

---

### Task 06：RecommendationRankSnapshot

目标：保存每次推荐排序快照，方便复盘。

新增表：

```python
class RecommendationRankSnapshot(Base):
    id: int
    run_id: int | None
    entity_id: int | None
    verification_item_id: int
    candidate_item_id: int
    final_score: int
    freshness_score: int
    feedback_adjustment: int
    freshness_bonus: int
    update_bonus: int
    rerank_score: int
    rank_position: int | None
    selected: bool
    reason_json: str
    created_at: datetime
```

验收：

```text
1. 每次 recommendation-export 都保存 rank snapshot。
2. snapshot 有完整分数组成。
3. 可以查询某次 run 的 Top N。
4. 可以比较 rerank 前后顺序变化。
```

---

### Task 07：run-daily 参数化和 dry-run

新增环境变量：

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

新增 CLI：

```bash
python -m app.main run-daily --dry-run
python -m app.main run-daily --skip-fetch
python -m app.main run-daily --only evidence-fetch
python -m app.main run-daily --from-step evidence-search
python -m app.main run-daily --to-step recommendation-export
```

`dry-run` 输出：

```text
将执行哪些步骤
每步 limit
各阶段 pending item 数量
AI/Tavily key 是否配置
预计 AI calls
预计 Tavily calls
预计输出文件
```

---

## 10. 第三批开发任务

### Task 08：GitHub verifier 增强

新增能力：

```text
1. 支持 GITHUB_TOKEN。
2. 处理 403 / 429 / rate limit，不直接当作 repo broken。
3. 获取 latest release。
4. 获取 tags。
5. 获取 repo size。
6. 获取 languages endpoint。
7. 检测关键文件：pyproject.toml、package.json、Dockerfile、smithery.yaml、mcp.json、README、LICENSE。
8. 识别 awesome-list / curated-list / paper-list。
```

目标：降低空仓库、列表仓库、假开源项目进入推荐区的概率。

---

### Task 09：Hugging Face verifier 增强

新增能力：

```text
1. 区分 model / dataset / space。
2. 统计权重文件数量和类型。
3. 统计权重文件大小。
4. 区分 gated model 和 open weights。
5. 识别 GGUF / safetensors / bin / pt。
6. 识别只有模型卡没有权重的占位页。
```

目标：让 open weights / GGUF / 模型发布类 claim 更可靠。

---

### Task 10：metrics-summary

新增命令：

```bash
python -m app.main metrics-summary --days 7
```

统计指标：

```text
precision@10
manual_accept_rate
hide_rate
report_rate
duplicate_rate
unverified_rate
broken_link_rate
claim_support_rate
contradicted_claim_rate
source_group 命中率
category 命中率
```

建议目标：

```text
precision@10 >= 80%
duplicate_rate <= 10%
unverified_rate <= 15%
broken_link_rate <= 5%
report_rate <= 5%
```

---

## 11. 不建议现在做的事

近期不要优先做：

```text
1. 不要继续无限添加 X 账号。
2. 不要直接写 X 爬虫。
3. 不要引入 Celery / Redis / Kafka。
4. 不要做前端。
5. 不要做视频 / TTS / 截图。
6. 不要把项目拆成多服务架构。
7. 不要把 AI 输出直接当事实。
8. 不要让 Product Hunt / X 文案直接进入强推荐。
```

项目当前应该保持轻量、可复跑、可测试、可审计。

---

## 12. 推荐实施顺序

### 第一阶段：必须优先完成

```text
1. EvidenceItem classify_status / classified_at / classify_error。
2. 下游 stale / force / upsert 机制。
3. deterministic guard。
4. ClaimVerificationItem support_strength。
```

完成后，系统可以避免“旧证据污染推荐结果”。

### 第二阶段：提高推荐解释性

```text
5. EntityUpdateEvent。
6. RecommendationRankSnapshot。
7. user_card / audit_card 拆分。
8. audit-export 展示完整证据链。
```

完成后，系统可以解释“为什么推荐它、为什么今天推荐、证据是什么”。

### 第三阶段：长期运行体验

```text
9. run-daily 参数化。
10. run-daily dry-run / only / from-step / to-step。
11. pending-summary。
12. metrics-summary。
```

完成后，系统更适合每天自动运行。

### 第四阶段：扩大能力边界

```text
13. GitHub verifier 增强。
14. Hugging Face verifier 增强。
15. Product Hunt API。
16. Telegram / Notion / GitHub Pages 输出。
```

---

## 13. 最终目标

这批优先级完成后，系统应满足：

```text
1. 每条推荐都能追溯证据。
2. 每条 claim 都知道是否被 direct support。
3. 规则层强反证无法被 AI 绕过。
4. evidence 更新后下游结果能自动失效并重算。
5. X/RSSHub 只作为发现信号，不作为最终事实依据。
6. 推荐卡片不会写未证实内容。
7. 旧闻、重复 mention、重大更新能区分。
8. 每次日报排序可复盘。
9. 用户反馈会影响排序，但不会无限放大。
10. 长期运行时可以通过 metrics 判断推荐质量是否变好。
```

---

## 14. 一句话结论

下一阶段不要继续以“增加信息源”为主，而应优先做：

```text
证据链重算 + evidence classify 状态 + deterministic guard + support_strength
```

这四项是当前项目从“能跑”走向“可靠”的关键。