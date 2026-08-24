# AI 资讯整理正式流程

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

## 1. 信息源配置

正式日报只读取 `app/config/source_registry.yaml` 中当前启用的信息源。`transport` 决定使用 `feed`、`rsshub` 或 `github` 抓取；`source_group` 负责可追溯的来源归因，`content_class` 由来源组派生。当前仓库没有人工录入或观众投稿入口，不能把尚未实现的来源写入正式调用链。

## 2. 抓取与标准化

collector 获取原始响应，parser 将 RSS、Atom、RSSHub 和 GitHub 数据转换为统一 `FetchItem`。Repository 按来源身份、外部 ID、URL 和内容哈希持久化并去重，同时保存原始 payload、抓取 attempt、来源健康状态和本次 draft 的成员关系。

正式日报抓取全部当前启用来源；`fetch`、`fetch-only`、单来源和单 `content_class` 过滤只用于诊断。单个来源抓取失败保存为审计警告，不阻断其他来源和后续发布。

## 3. Stage A 初筛

Stage A 是唯一的时间准入阶段，先按日报日期前一天 00:00（Asia/Shanghai）执行确定性筛选，再调用结构化 AI 初筛。未提供有效发布时间、时间早于边界或时间在构建参考时间之后的普通资讯被排除；GitHub Trending 若重新启用，按项目发现信号豁免新闻发布时间筛选。

AI 初筛识别与目标主题无关、信息量低、缺乏新增事实、广告营销和转载噪声等内容。只有高置信度的规范硬拒绝会停止处理；低置信度或非硬拒绝结果保留为 `uncertain`，继续进入 Stage B1。

## 4. Stage B1 分析、评分、准入

Stage B1 面向单条资讯生成：

- 中文短摘要；
- 六类编辑主题之一；
- 关键词和实体；
- 内容价值评分及其结构化分量。

本地 deterministic guard 使用以下五个内容价值维度重算 `b1_priority`：

- `audience_relevance`：45%；
- `impact_scope`：25%；
- `independent_news_value`：20%；
- `material_change`：5%；
- `specificity`：5%。

来源身份、模型置信度和时间新鲜度不计入 B1 分数：来源归因来自 registry，时间准入已经由 Stage A 完成。达到本地总分和 AI 主体相关性门槛的条目按多样性进入 `active`，其后形成有限的 `reserve`，其余保存为 `filtered`。这三个准入结果是 Stage C 的唯一工作台输入。

## 5. Stage C 事件聚合与核验

Stage C 根据主体、动作、对象、版本或阶段、时间锚点、关键词和实体，将同一事件的多来源合并，保留补充信息，并区分同一事件、后续进展和仅主题相近的不同事件。

历史新旧判断只读取当前日报日期之前三个自然日内的已发布最终日报；草稿、候选池、搜索结果和更早日报不参与。`new` 只表示最近三期正式日报中没有同一事件，事件是否具有可核验的实质变化需要单独判断。

对影响事件结论的争议事实，Stage C 可调用 Tavily，并把查询、结果 URL、摘要和具体 claim 绑定到审计记录。最终形成：

- `candidate`：可进入最终复审；
- `needs_review`：信息有价值但仍需终审确认，携带原因和证据；
- `rejected`：不进入 Stage D，但完整保留在 Stage C 审计池。

事件级候选保存标题、摘要、主题、关键词、实体、来源、时间、评分、聚合成员、新旧状态、实质变化判断和核验证据。

## 6. Stage D 人工式二次审核

Stage D 是正式日报发布的必经阶段。它读取 Stage C 的 `candidate` 和 `needs_review` 事件，优先核验争议项，再选择本期展示的有序子集，并为每个入选事件记录 `reason_code` 和简短理由。

Stage D 不重新聚合事件，也不生成或修改事件标题、摘要、主题、评分、来源和新旧判断。未入选事件由可审事件集合减去入选集合得到，不额外维护排除快照。

## 7. Export 导出

Export 校验 Stage D 任务已经成功且输出 schema 有效，然后严格按 `selected` 数组顺序序列化 Stage C 事件内容，生成 `intel_items.jsonl`、`intel_digest.md` 和 `manifest.json`。Export 不重新做时间准入、初筛、评分、聚合、核验或选稿；这些职责分别属于 A、B1、C 和 D。

`pipeline run` 或 `pipeline resume` 在 A-D 全部成功后，先从 `draft.db` 生成 `output/intel/draft/YYYY-MM-DD/`，供人工查看。`draft.db` 是权威数据，Markdown、JSONL 和 manifest 只是展示投影；手工修改展示文件不会改变数据库或正式发布内容。

正式发布时，Export 再次从 `draft.db` 读取 Stage D 的有序结果并生成发布数据，不读取或解析 draft Markdown。文件先写入与正式目录相邻的 staging 目录，避免失败时产生半成品公开日报。

## 8. 正式发布

`pipeline export --edition-date ...` 是明确的批准与发布动作。只有 A、B1、C、D 和 Export 全部成功后，系统才会：

1. 将当前 `draft.db` 提升为该日期的 `audit.db`；
2. 原子替换 `output/daily/YYYY-MM-DD/`；
3. 在正式数据库中替换该日期唯一的已发布日报及其入选事件。

任一步骤失败都会恢复旧 audit、旧日报文件和旧正式数据库状态；失败 draft 保留，供按日期检查、重试或恢复。UI 和 API 只读取已发布日报，不读取构建中的 draft。

正式全量入口是：

```bash
python -m app.main pipeline run --edition-date YYYY-MM-DD
python -m app.main pipeline export --edition-date YYYY-MM-DD
```

也可以使用 `pipeline start`、`stage-a`、`stage-b1`、`stage-c`、`stage-d`、`retry` 和 `resume` 做按阶段恢复，但它们仍共享同一套日期级 draft 和阶段契约。
