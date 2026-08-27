# AGENTS.md

本仓库的主日报流程以 `reference/AI 资讯整理流程.md` 为业务基准。实现先阅读相关代码、测试和文档，再进行小范围、可验证的修改。

## 主流程

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

阶段职责：

- **信息源配置**：`source_registry.yaml` 是正式抓取清单；`transport` 只决定抓取路由，来源归因由 `source_group` 和派生的 `content_class` 表达。
- **抓取与标准化**：collector/parser 生成统一 `FetchItem`，Repository 负责持久化、身份去重、来源关联和抓取审计。
- **Stage A**：唯一的时间准入和初筛阶段；硬拒绝项停止，低置信度项保留给 B1。
- **Stage B1**：面向单条资讯生成短摘要、关键词/实体、分类和新闻价值评分。本地只保留分数和 AI 主体相关度两道门槛；过线项全部交给 Stage C，不再做名额、主题打散或 reserve 截杀。
- **Stage C**：根据关键词、实体和事件语义聚合去重，结合近三期已发布日报判断新旧与进展，并核验影响事件结论的争议事实，生成 `candidate`、`needs_review`、`rejected` 事件池。
- **Stage D**：正式日报发布前必经的人工式二次审核，只选择有序事件子集，不重写 Stage C 的标题、摘要、分类或聚合关系。
- **Export**：校验 Stage D 契约并原样序列化其有序子集，不重新做时间筛选、评分、聚合或选稿。
- **正式发布**：原子提升 draft/audit、日报文件和正式数据库；失败时保留旧日报并回滚公开状态。

“B→C 适配”聚焦字段、任务状态和数据接口；C 保持事件聚合与核验职责。进 B 的资讯数量由 Stage A 控制。

## 实现边界

1. 沿用现有 Python、SQLAlchemy、FastAPI、Repository、Job、CLI 和模板结构。
2. 每次改动围绕一个明确行为，保持相邻阶段、数据库和公共接口稳定。
3. 新增阶段、数据库表字段、schema、CLI 命令、provider 抽象或复杂框架前，先确认业务目标、影响范围、迁移成本和回退方案。
4. embedding、向量数据库、工作流引擎、消息队列、微服务和 React/Vue 属于独立架构扩展，按明确任务单独设计。
5. 筛选、评分、聚合和导出规则放在对应阶段，输入、输出和审计结果保持一致。
6. 数据任务通过 `app/collectors/`、`app/parsers/`、`app/ai/`、`app/jobs/` 和 `app/storage/` 协作完成；网页 UI 通过 `app/web/` 和 `read_repository` 展示数据。

## 数据与 AI

- 原始资讯、AI 分析、事件聚合、来源和候选事件保持可追溯关联。
- AI 输出使用结构化 schema 解析、校验并保存原始响应。
- AI 分数经过本地 deterministic guard 复算后参与下游处理。
- 外部请求配置超时、重试和错误记录，单条失败进入任务状态并支持恢复。
- X、Reddit、RSSHub 和社区内容作为线索来源；高可信推荐结合直接来源证据。

## 验证

修改核心逻辑后进行编译检查，并通过项目正常入口做针对性验证：

```bash
python -m compileall -q app
```

完成任务后说明修改文件、实现原因、验证结果和已知限制。Git 提交、推送和其他仓库操作按照用户指令执行。
