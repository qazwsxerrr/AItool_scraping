# 简化情报流水线 v2

本版本只处理抓取和 AI 情报处理，不在请求期间运行 UI 逻辑。默认链路固定为：

```text
source registry -> fetch -> process -> export
```

## 内容分流

| 类别 | 典型来源 | 确定性筛选 | 轻量核实 | 输出语义 |
| --- | --- | --- | --- | --- |
| `official_model_company` | 官方博客、模型卡、官方发布 | 最近 30 天 + 发布/模型/API 等关键词 | 一个允许域名内的官方直链，2xx 才是 `verified` | 无直链或失败为 `needs_review` |
| `project_tool` | GitHub、Product Hunt、工具页 | GitHub: `stars > 100` 且最近 30 天 push；Product Hunt: 时间/票数排序 | GitHub/产品元数据，不做第三方 claim 核实 | `hotspot`，README 是项目自述 |
| `community_social` | X、Reddit、RSSHub 搜索、LINUX DO | 最近 7 天 + 关键词/互动信号 | 不做事实核实 | `discovery_only` |

社区正文中的 GitHub、官网或模型卡链接写入 `intel_items.discovered_links_json`，作为后续候选；原社区条目仍保持 `community_social`，不会自动成为强推荐。

## 数据表

- `sources`：来源和解析后的 `content_class`、collector 类型及 JSON 策略。
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
python -m app.main run-once --source github_search_ai_active_high_star --force
python -m app.main run-once --class official_model_company --dry-run
```

AI provider 继续使用 `AI_REVIEW_API_URL`、`AI_REVIEW_API_KEY`、`AI_REVIEW_MODEL` 和 `AI_REVIEW_API_STYLE`。未配置或单条调用失败时，条目保留并标记 `ai_failed`，不会静默丢弃。

GitHub repository/release 已经包含结构化 stars、forks、watchers、language、license、release 和 pushed_at。处理阶段只执行 Star/最近 push 等 registry 规则，直接标记 `hotspot` 并写入 metadata verification，不调用 AI；导出后由 `app/storage/github_reader.py` 按这些指标排序。

Product Hunt 公开 Atom 不保证提供 votes/comments。配置 `PRODUCTHUNT_API_TOKEN` 后，collector 使用官方 GraphQL API 获取 `votesCount`、`commentsCount` 和 rank；未配置时保留 Atom 抓取，并把 `producthunt_metrics_status=unavailable_in_feed` 写入 metrics，排序透明降级为发布时间。

导出目录包含：

- `intel_items.jsonl`：可展示的 `verified`、`hotspot`、`discovery_only` 条目。
- `intel_pending.jsonl`：`needs_review`、`ai_failed` 和尚未分析的条目。
- `intel_digest.md`：带分类、状态、指标、风险和链接的日报。

## Collector 边界

`app/collectors/unified.py` 中的 RSS/Atom、RSSHub、GitHub 和 Product Hunt collector 只负责请求和字段映射，接受共享 `httpx.Client`，返回 `FetchBatch`；数据库写入、筛选、AI 和核实均在 Job/Domain 层完成。
