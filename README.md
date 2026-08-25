# AI 资讯抓取与筛选

项目从配置的信息源抓取 AI 资讯，经过多阶段筛选生成待审核日报，并通过 Web UI 展示已发布结果。以下命令均在项目根目录执行，并默认已完成依赖安装和 `.env` 配置。

## 启动 RSSHub

RSSHub 用于抓取 X 等 RSSHub 来源。默认读取同级目录 `../RSSHub`，也可以用 `RSSHUB_DIR` 指定仓库位置；端口、Token 和代理从项目根目录的 `.env` 读取。

```bash
bash scripts/start_rsshub.sh
```

## 数据抓取与筛选流程

```text
source_registry.yaml 信息源
→ 抓取并标准化
→ Stage A 时间准入与初筛
→ Stage B1 摘要、分类、评分与准入
→ Stage C 事件聚合、去重与事实核验
→ Stage D 最终复审与排序
→ 生成待审核日报
```

- 抓取：读取 `app/config/source_registry.yaml` 中启用的 Feed、RSSHub 和 GitHub 来源，标准化后写入当日 `draft.db`。
- Stage A：执行时间范围检查和硬性初筛。
- Stage B1：生成摘要、主题、关键词和评分，将资讯划分为 `active`、`reserve` 或 `filtered`。
- Stage C：把相关资讯聚合为事件，结合近期日报去重，并核验争议事实。
- Stage D：从候选事件中选择最终有序子集。

运行当天的完整抓取与筛选：

```bash
PYTHON=./.venv/bin/python
EDITION_DATE=$(TZ=Asia/Shanghai date +%F)

$PYTHON -m app.main pipeline run \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

筛选结果位于 `output/intel/draft/YYYY-MM-DD/`。检查各阶段和 draft 状态：

```bash
$PYTHON -m app.main pipeline status \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

确认筛选结果后正式发布：

```bash
$PYTHON -m app.main pipeline export \
  --edition-date "$EDITION_DATE" \
  --output-dir output/intel
```

## 启动 Web UI

```bash
./.venv/bin/python -m uvicorn app.web.app:app \
  --host 127.0.0.1 \
  --port 8000
```

浏览器访问：<http://127.0.0.1:8000>
