# AGENTS.md

本仓库以 `reference/AI 资讯整理流程.md` 为业务基准。

## 工作原则

- 修改前阅读相关代码、测试和文档。
- 只做用户要求范围内的最小改动，不主动扩展需求。
- 围绕明确需求和可复现问题修改，不主动考虑低概率、假设性或过度复杂的边界条件。
- 按文字的直接含义执行；不得自行推导未明示的业务规则。
- 修改后运行相关定向验证，并说明改动和验证结果。
- 未经用户要求，不提交或推送 Git。

## 流程边界

- **信息源**：`source_registry.yaml` 是正式抓取清单；`transport` 负责路由，`source_group` 和 `content_class` 负责来源归因。
- **抓取**：collector/parser 生成 `FetchItem`，Repository 负责持久化和去重。
- **Stage A**：唯一的时间准入和初筛阶段。
- **Stage B1**：分析单条资讯；只按分数和 AI 主体相关度准入，过线项全部进入 Stage C。
- **Stage C**：聚合事件、比较近三期历史并核验争议事实，输出 `candidate`、`needs_review`、`rejected`。
- **Stage D**：只选择 Stage C 事件的有序子集，不重写事件内容或聚合关系。
- **Export**：校验 Stage D 契约并原样序列化其有序子集，不重新做时间筛选、评分、聚合或选稿。
- **发布**：原子提升 draft/audit、日报文件和正式数据库；失败时保留旧版本。
