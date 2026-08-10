# 简化情报流水线 v2

本版本只处理抓取和 AI 情报处理，不在请求期间运行 UI 逻辑。默认链路固定为：

```text
source registry -> fetch -> process -> export
```

## 内容分流

| 类别 | 典型来源 | 确定性筛选 | 轻量核实 | 输出语义 |
| --- | --- | --- | --- | --- |
| `official_model_company` | 官方博客、模型卡、官方发布 | 最近 30 天 + 发布/模型/API 等关键词 | 一个允许域名内的官方直链，2xx 才是 `verified` | 无直链或失败为 `needs_review` |
| `project_tool` | GitHub Trending/Search、Product Hunt、工具页 | Trending 使用 daily/weekly 周期 Star；Search 使用 `stars > 100` 且最近 7 天 push；Product Hunt: 时间/票数排序 | GitHub/产品元数据，不做第三方 claim 核实 | `hotspot`，README 是项目自述 |
| `community_social` | X、Reddit、RSSHub 搜索、LINUX DO | 最近 7 天 + 关键词/互动信号 | 不做事实核实 | `discovery_only` |

社区正文中的 GitHub、官网或模型卡链接写入 `intel_items.discovered_links_json`，作为后续候选；原社区条目仍保持 `community_social`，不会自动成为强推荐。

## 数据表

- `sources`：来源的 `transport`、Feed/GitHub 嵌套选项、`content_class` 和 JSON 策略。
- `fetch_attempts`：每次请求的状态、HTTP 状态、传输方式、重试、字节数和条目计数。
- `intel_items`：统一条目、稳定去重键、指标、选择状态和原始 payload。
- `ai_item_reviews`：每条最多一条结构化 AI 分析，保留原始响应。
- `item_verifications`：官方直链、项目元数据或社区发现结果。
- `intel_runs`：一次 `run-once` 的汇总计数和状态。

旧的 `claim/evidence/recommendation` 表、Job、客户端和脚本已经移除。历史数据库可以直接删除后由 `init_db` 创建新表；不需要迁移历史数据。

## 运行

```bash
# 逐阶段运行
python -m app.main fetch --class project_tool
python -m app.main process --class project_tool --limit 100
python -m app.main export --output-dir output/intel

# 日常入口
python -m app.main run-once --limit 100

# 单来源、强制重跑或本地 dry-run
python -m app.main run-once --source github_search_topic_llm --force
python -m app.main run-once --class official_model_company --dry-run
```

AI provider 继续使用 `AI_REVIEW_API_URL`、`AI_REVIEW_API_KEY`、`AI_REVIEW_MODEL` 和 `AI_REVIEW_API_STYLE`。未配置或单条调用失败时，条目保留并标记 `ai_failed`，不会静默丢弃。

GitHub Trending/Search repository 已经包含结构化 stars、周期新增 Star、forks、language、topics 和 pushed_at（Search）。处理阶段先执行 registry 的 Star 规则，AI 不参与保留决策；选中的唯一仓库再补充 bounded metadata/README，并最多调用一次窄项目摘要 AI，生成项目介绍、主要能力、适用场景和风险提示，最终始终标记 `hotspot`，不执行 claim/evidence 核实。导出后由 `app/storage/github_reader.py` 展示周期指标和持久化项目介绍。

Product Hunt 统一使用公开 Atom。`feed.adapter=producthunt` 只做 Product Hunt 条目的字段映射；feed 未提供的 votes/comments/rank 不会通过 Product Hunt API 补抓，缺失指标保持为空并由策略透明降级。条目若包含 GitHub 仓库链接，可按 GitHub 例外规则执行有界 metadata enrichment。

导出目录包含：

- `intel_items.jsonl`：可展示的 `verified`、`hotspot`、`discovery_only` 条目。
- `intel_pending.jsonl`：`needs_review`、`ai_failed` 和尚未分析的条目。
- `intel_digest.md`：带分类、状态、指标、风险和链接的日报。
- `output/github-trending/YYYY/MM/YYYYMMDD.md`：按日期保存的 GitHub Trending/Search 热点报告。

## Collector 边界

`app/collectors/` 按职责提供共享基类和 HTTP 客户端、Feed/RSSHub 解析、GitHub REST、GitHub Trending HTML 以及显式路由工厂。所有实现都接受 `SourceSpec` 和共享 `httpx.Client`，返回 `FetchBatch`；数据库写入、筛选、AI 和核实均在 Job/Domain 层完成。路由只接受已声明的 `transport` 和嵌套 mode，不再根据旧字段或未知值猜测 collector。
