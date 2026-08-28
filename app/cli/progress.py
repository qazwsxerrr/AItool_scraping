"""Rich terminal progress for the daily AI report pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text


STAGE_ORDER = ("fetch", "screen", "analyze", "cluster", "stage_d", "draft-export", "export")
STAGE_LABELS = {
    "fetch": "Fetch｜抓取",
    "screen": "Stage A｜时间准入与初筛",
    "analyze": "Stage B｜分析、评分与准入",
    "cluster": "Stage C｜事件聚合与核验",
    "stage_d": "Stage D｜二次审核与排序",
    "draft-export": "Export｜生成审核稿",
    "export": "Export｜正式发布",
}
TERMINAL_STATUSES = {"succeeded", "failed", "skipped"}
STAGE_METRIC_LABELS = {
    "fetch": {
        "source_count": "抓取来源",
        "fetched_items": "原始资讯",
        "inserted_items": "新增入库",
        "skipped_items": "重复跳过",
        "failed_sources": "失败来源",
        "degraded_sources": "降级来源",
    },
    "screen": {
        "processed": "待初筛",
        "time_filtered": "时间过滤",
        "screened": "完成初筛",
        "screened_out": "初筛拒绝",
        "screen_failed": "失败",
    },
    "analyze": {
        "processed": "待分析",
        "analyzed": "已分析",
        "candidate": "准入",
        "analysis_filtered": "过滤",
        "analysis_failed": "失败",
    },
    "cluster": {
        "input_items": "输入资讯",
        "reserve_items": "备用资讯",
        "history_events": "历史窗口",
        "processed": "输入资讯",
        "event_count": "事件",
        "merged": "已合并",
        "repeats": "历史重复",
        "updated": "更新",
        "needs_review": "需要复核",
        "rejected": "历史重复/拒绝",
        "turns": "模型调用",
        "tool_calls": "工具调用",
        "web_searches": "联网搜索",
    },
    "stage_d": {
        "candidates": "候选事件",
        "selected": "入选",
        "unselected": "未入选",
        "provider_attempts": "模型调用",
        "web_searches": "联网搜索",
        "ai_failed": "失败",
    },
    "draft-export": {
        "exported": "导出事件",
        "markdown_path": "Markdown",
        "jsonl_path": "JSONL",
        "manifest_path": "Manifest",
    },
    "export": {
        "exported": "导出事件",
        "markdown_path": "Markdown",
        "jsonl_path": "JSONL",
        "manifest_path": "Manifest",
    },
}
ACTION_LABELS = {
    "prepare_workspace": "准备事件聚合工作台",
    "prepare_sources": "准备抓取来源",
    "fetch_source": "抓取当前来源",
    "source_succeeded": "来源抓取完成",
    "source_degraded": "来源抓取降级",
    "source_failed": "来源抓取失败",
    "source_skipped": "跳过未到抓取间隔的来源",
}


@dataclass(frozen=True)
class PipelineProgressEvent:
    """Normalized progress event consumed by the CLI reporter."""

    type: str
    stage: str | None = None
    message: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PipelineProgressEvent":
        return cls(
            type=str(value.get("type") or ""),
            stage=_text(value.get("stage")),
            message=_text(value.get("message")),
            data=dict(value.get("data") or {}),
        )


@dataclass
class StageSnapshot:
    name: str
    status: str = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int | None = None
    current: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    current_task: str | None = None
    current_action: str | None = None
    warning: str | None = None
    error: str | None = None
    current_source_id: str | None = None
    current_source_name: str | None = None

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now()
        return max(0.0, (end - self.started_at).total_seconds())


class PipelineConsoleReporter:
    """Render pipeline progress without leaking business payloads to stdout."""

    def __init__(
        self,
        *,
        edition_date: str | None,
        output_dir: str,
        mode: str = "完整执行",
        console: Console | None = None,
    ) -> None:
        self.edition_date = edition_date or _today_shanghai()
        self.output_dir = output_dir
        self.mode = mode
        self.console = console or Console()
        self.stages = {name: StageSnapshot(name=name) for name in STAGE_ORDER}
        self.errors: list[str] = []
        self._live: Live | None = None

    def __enter__(self) -> "PipelineConsoleReporter":
        if self.console.is_terminal:
            self._live = Live(self._render(), console=self.console, refresh_per_second=4, transient=False)
            self._live.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc is not None:
            self.errors.append(str(exc))
            self._active_stage_failed(str(exc))
        if self._live is not None:
            self.refresh()
            self._live.__exit__(exc_type, exc, traceback)
            self._live = None
        else:
            self.console.print(self._render())

    def emit(self, raw_event: Mapping[str, Any]) -> None:
        event = PipelineProgressEvent.from_mapping(raw_event)
        if event.type == "pipeline_start":
            self.edition_date = _text(event.data.get("edition_date")) or self.edition_date
            self.output_dir = _text(event.data.get("output_dir")) or self.output_dir
            self.mode = _text(event.data.get("mode")) or self.mode
        elif event.type == "stage_start" and event.stage:
            self._stage(event.stage).status = "running"
            self._stage(event.stage).started_at = datetime.now()
            self._stage(event.stage).error = None
            self._stage(event.stage).warning = None
        elif event.type == "stage_skip" and event.stage:
            snapshot = self._stage(event.stage)
            snapshot.status = "skipped"
            snapshot.finished_at = datetime.now()
            snapshot.current_action = event.message or "已复用现有结果"
        elif event.type == "stage_complete" and event.stage:
            snapshot = self._stage(event.stage)
            snapshot.status = "succeeded"
            snapshot.finished_at = datetime.now()
            self._merge_stage_data(snapshot, event.data)
        elif event.type == "stage_error" and event.stage:
            snapshot = self._stage(event.stage)
            snapshot.status = "failed"
            snapshot.finished_at = datetime.now()
            snapshot.error = event.message or _text(event.data.get("error"))
            if snapshot.error:
                self.errors.append(f"{STAGE_LABELS.get(event.stage, event.stage)}: {snapshot.error}")
        elif event.type == "stage_update" and event.stage:
            self._merge_stage_data(self._stage(event.stage), event.data)
        elif event.type == "stage_c_response":
            snapshot = self._stage("cluster")
            snapshot.status = "running"
            snapshot.metrics["turns"] = max(_int(snapshot.metrics.get("turns")), _int(event.data.get("turn")))
            snapshot.current_action = "模型推理"
        elif event.type == "stage_c_tool":
            self._apply_stage_c_tool(event)
        elif event.type == "pipeline_error":
            message = event.message or _text(event.data.get("error"))
            if message:
                self.errors.append(message)
        self.refresh()

    def refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _stage(self, name: str) -> StageSnapshot:
        return self.stages[name]

    def _active_stage_failed(self, error: str) -> None:
        for name in STAGE_ORDER:
            snapshot = self.stages[name]
            if snapshot.status == "running":
                snapshot.status = "failed"
                snapshot.error = error
                snapshot.finished_at = datetime.now()
                return

    def _merge_stage_data(self, snapshot: StageSnapshot, data: Mapping[str, Any]) -> None:
        if "total" in data:
            snapshot.total = _optional_int(data.get("total"))
        if "current" in data:
            snapshot.current = _optional_int(data.get("current"))
        if "current_task" in data:
            snapshot.current_task = _text(data.get("current_task"))
        if "current_action" in data:
            snapshot.current_action = _action_label(data.get("current_action"))
        if "current_source_id" in data:
            snapshot.current_source_id = _text(data.get("current_source_id"))
        if "current_source_name" in data:
            snapshot.current_source_name = _text(data.get("current_source_name"))
        if "warning" in data:
            snapshot.warning = _text(data.get("warning"))
        if "error" in data:
            snapshot.error = _text(data.get("error"))
        metrics = data.get("metrics")
        if isinstance(metrics, Mapping):
            snapshot.metrics.update(dict(metrics))

    def _apply_stage_c_tool(self, event: PipelineProgressEvent) -> None:
        snapshot = self._stage("cluster")
        snapshot.status = "running"
        snapshot.metrics["tool_calls"] = _int(snapshot.metrics.get("tool_calls")) + 1
        tool_name = str(event.data.get("tool") or "")
        snapshot.current_action = _stage_c_action(tool_name)
        title = _text(event.data.get("title"))
        if title:
            snapshot.current_task = title
        if tool_name == "search_web":
            snapshot.metrics["web_searches"] = _int(snapshot.metrics.get("web_searches")) + 1
        if tool_name in {"submit_event_plan", "list_plan_snapshot"}:
            saved = _optional_int(event.data.get("event_count"))
            if saved is not None:
                snapshot.metrics["event_count"] = saved
            covered = _optional_int(event.data.get("covered_items"))
            if covered is not None:
                snapshot.current = covered
            total = _optional_int(event.data.get("active_total"))
            if total is not None:
                snapshot.total = total
            needs_review = _optional_int(event.data.get("needs_review"))
            if needs_review is not None:
                snapshot.metrics["needs_review"] = needs_review
            rejected = _optional_int(event.data.get("rejected"))
            if rejected is not None:
                snapshot.metrics["rejected"] = rejected
        if event.data.get("ok") is False:
            snapshot.warning = _text(event.data.get("error")) or "工具返回失败"

    def _render(self) -> Panel:
        header = Text()
        header.append("AI 日报流水线\n", style="bold")
        header.append(f"日期：{self.edition_date}（Asia/Shanghai）\n")
        header.append(f"输出：{self.output_dir}\n")
        header.append(f"运行模式：{self.mode}")

        body = [header, _separator()]
        for name in STAGE_ORDER:
            snapshot = self.stages[name]
            if name == "export" and snapshot.status == "pending":
                continue
            body.append(self._render_stage(snapshot))
        if self.errors:
            body.append(_separator())
            for error in self.errors[-3:]:
                text = Text("错误：", style="bold red")
                text.append(error, style="red")
                body.append(text)
        return Panel(Group(*body), border_style="cyan", expand=True)

    def _render_stage(self, snapshot: StageSnapshot) -> Group:
        title = Text()
        title.append(f"{_status_icon(snapshot.status)} ", style=_status_style(snapshot.status))
        title.append(STAGE_LABELS.get(snapshot.name, snapshot.name), style="bold")
        if snapshot.total and snapshot.current is not None and snapshot.status == "running":
            title.append(f"    {snapshot.current} / {snapshot.total}", style="cyan")
        elapsed = _format_elapsed(snapshot.elapsed_seconds)
        if elapsed and snapshot.status in TERMINAL_STATUSES:
            title.append(f"    耗时：{elapsed}", style="dim")

        rows: list[Any] = [title]
        if snapshot.current_source_id:
            label = snapshot.current_source_id
            if snapshot.current_source_name and snapshot.current_source_name != snapshot.current_source_id:
                label = f"{snapshot.current_source_name}（{snapshot.current_source_id}）"
            rows.append(_kv("当前来源", label))
        if snapshot.current_task:
            rows.append(_kv("当前任务", snapshot.current_task))
        if snapshot.current_action:
            rows.append(_kv("当前动作", snapshot.current_action))
        if snapshot.metrics:
            table = Table.grid(padding=(0, 2))
            table.add_column(style="dim")
            table.add_column()
            for key, value in snapshot.metrics.items():
                if value is None or value == "":
                    continue
                table.add_row(f"{_metric_label(snapshot.name, key)}：", str(value))
            rows.append(table)
        if snapshot.total and snapshot.current is not None:
            complete = min(1.0, max(0.0, snapshot.current / snapshot.total))
            progress = Table.grid(padding=(0, 1))
            progress.add_column()
            progress.add_column(justify="right", no_wrap=True)
            progress.add_row(ProgressBar(total=1.0, completed=complete, width=30), f"{int(round(complete * 100))}%")
            rows.append(progress)
        if snapshot.warning:
            rows.append(Text(f"警告：{snapshot.warning}", style="yellow"))
        if snapshot.error:
            rows.append(Text(f"错误：{snapshot.error}", style="red"))
        return Group(*rows)


def _stage_c_action(tool_name: str) -> str:
    return {
        "list_candidates": "读取候选列表",
        "list_plan_snapshot": "读取事件方案快照",
        "read_items": "读取资讯正文",
        "search_candidates": "检索候选池",
        "read_recent_history": "比对近三期日报",
        "search_web": "搜索补充证据（Tavily）",
        "attach_search_evidence": "绑定搜索证据",
        "submit_event_plan": "提交完整事件方案",
    }.get(tool_name, tool_name or "工具执行")


def _metric_label(stage: str, key: str) -> str:
    return STAGE_METRIC_LABELS.get(stage, {}).get(key, key)


def _action_label(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return ACTION_LABELS.get(text, text)


def _status_icon(status: str) -> str:
    return {
        "succeeded": "[✓]",
        "running": "[▶]",
        "failed": "[×]",
        "skipped": "[-]",
    }.get(status, "[ ]")


def _status_style(status: str) -> str:
    return {
        "succeeded": "green",
        "running": "cyan",
        "failed": "red",
        "skipped": "yellow",
    }.get(status, "dim")


def _separator() -> Text:
    return Text("─" * 60, style="dim")


def _today_shanghai() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _kv(key: str, value: object) -> Text:
    text = Text(f"    {key}：", style="dim")
    text.append(str(value))
    return text


def _format_elapsed(value: float | None) -> str | None:
    if value is None:
        return None
    seconds = max(0, int(round(value)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value: Any) -> int:
    return _optional_int(value) or 0


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["PipelineConsoleReporter", "PipelineProgressEvent", "StageSnapshot"]
