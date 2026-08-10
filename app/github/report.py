"""Render date-scoped GitHub Trending reports from exported metadata."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def write_github_trending_report(
    records: list[dict[str, Any]],
    *,
    output_root: str | Path,
    report_date: date | None = None,
) -> Path | None:
    github_records = [record for record in records if _is_github_repository(record)]
    if not github_records:
        return None

    report_date = report_date or datetime.now(timezone.utc).date()
    root = Path(output_root)
    path = root / f"{report_date:%Y}" / f"{report_date:%m}" / f"{report_date:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_github_trending_report(github_records, report_date=report_date), encoding="utf-8")
    return path


def render_github_trending_report(
    records: list[dict[str, Any]],
    *,
    report_date: date,
) -> str:
    weekly = _period_rows(records, "weekly")
    daily = _period_rows(records, "daily")
    search_only = [record for record in records if not _trending_periods(_mapping(record.get("metrics")))]
    search_only.sort(key=_search_sort_key, reverse=True)

    lines = [
        f"# {report_date:%Y-%m-%d} GitHub AI 热点",
        "",
        "> 数据来源：GitHub Trending HTML（daily/weekly）与 GitHub Search API；本报告不使用历史 Star 快照。",
        "",
        "## 本周 Trending Top 20",
        "",
    ]
    lines.extend(_table(weekly, period="weekly"))
    lines.extend(["", "## 今日 Trending Top 20", ""])
    lines.extend(_table(daily, period="daily"))
    lines.extend(["", "## Search API 补充候选", ""])
    lines.extend(_search_table(search_only[:20]))
    lines.extend(["", "## 项目详情", ""])

    detail_records = _unique_records([*weekly[:20], *daily[:20], *search_only[:20]])
    if not detail_records:
        lines.append("暂无项目数据。")
    for index, record in enumerate(detail_records, start=1):
        lines.extend(_detail_block(index, record))
    return "\n".join(lines).rstrip() + "\n"


def _table(rows: list[dict[str, Any]], *, period: str) -> list[str]:
    if not rows:
        return ["暂无数据。"]
    label = "本周新增 Star" if period == "weekly" else "今日新增 Star"
    lines = [
        "| 排名 | 项目 | 累计 Star | " + label + " | Fork | 语言 |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for row in rows[:20]:
        metrics = _mapping(row.get("metrics"))
        period_data = _trending_periods(metrics).get(period, {})
        lines.append(
            "| {rank} | [{name}]({url}) | {stars} | {since} | {forks} | {language} |".format(
                rank=_number(period_data.get("rank")) or "-",
                name=_repo_name(row),
                url=row.get("url") or "",
                stars=_count(metrics.get("stars")),
                since=_count(period_data.get("stars_since")),
                forks=_count(metrics.get("forks")),
                language=metrics.get("language") or "-",
            )
        )
    return lines


def _search_table(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return ["暂无仅由 Search API 发现的候选项目。"]
    lines = [
        "| 项目 | 累计 Star | 最近 Push | 命中 Topic | 语言 |",
        "|---|---:|---|---|---|",
    ]
    for record in records:
        metrics = _mapping(record.get("metrics"))
        lines.append(
            "| [{name}]({url}) | {stars} | {pushed} | {topics} | {language} |".format(
                name=_repo_name(record),
                url=record.get("url") or "",
                stars=_count(metrics.get("stars")),
                pushed=metrics.get("pushed_at") or "-",
                topics=", ".join(_project_topics(metrics)) or "-",
                language=metrics.get("language") or "-",
            )
        )
    return lines


def _detail_block(index: int, record: dict[str, Any]) -> list[str]:
    metrics = _mapping(record.get("metrics"))
    periods = _trending_periods(metrics)
    weekly = periods.get("weekly", {})
    daily = periods.get("daily", {})
    sources = ", ".join(_string_list(metrics.get("discovery_sources"))) or record.get("source_id") or "-"
    topics = ", ".join(_project_topics(metrics))
    ai = _mapping(record.get("ai"))
    introduction = _clean_text(ai.get("summary_cn") or record.get("summary") or record.get("content_text"))
    lines = [
        f"### {index}. {_repo_name(record)}",
        f"- 链接：{record.get('url') or '-'}",
        f"- 累计 Star：{_count(metrics.get('stars'))}",
        f"- 本周新增 Star（GitHub Trending）：{_count(weekly.get('stars_since')) if weekly else '-'}",
        f"- 今日新增 Star（GitHub Trending）：{_count(daily.get('stars_since')) if daily else '-'}",
        f"- Fork：{_count(metrics.get('forks'))}",
        f"- 最近 Push：{metrics.get('pushed_at') or '-'}",
        f"- 来源：{sources}",
        *([f"- 命中 Topic：{topics}"] if topics else []),
        f"- 项目介绍：{introduction or '暂无介绍'}",
        "",
    ]
    return lines


def _period_rows(records: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    rows = [record for record in records if period in _trending_periods(_mapping(record.get("metrics")))]
    rows.sort(
        key=lambda record: (
            _number(_trending_periods(_mapping(record.get("metrics"))).get(period, {}).get("rank")) or 10**9,
            -_number(_trending_periods(_mapping(record.get("metrics"))).get(period, {}).get("stars_since")),
            _number(_mapping(record.get("metrics")).get("stars")),
        ),
    )
    return rows


def _search_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    metrics = _mapping(record.get("metrics"))
    return _number(metrics.get("stars")), str(metrics.get("pushed_at") or "")


def _trending_periods(metrics: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    periods = metrics.get("trending")
    result = {key: dict(value) for key, value in periods.items() if isinstance(value, Mapping)} if isinstance(periods, Mapping) else {}
    period = metrics.get("trending_period")
    if period and period not in result:
        result[str(period)] = {
            "rank": metrics.get("trending_rank"),
            "stars_since": metrics.get("stars_since"),
            "stars": metrics.get("stars"),
            "forks": metrics.get("forks"),
        }
    return result


def _unique_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = str(record.get("external_id") or record.get("url") or record.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _is_github_repository(record: dict[str, Any]) -> bool:
    source_type = str(record.get("source_type") or "").casefold()
    source_id = str(record.get("source_id") or "").casefold()
    external_id = str(record.get("external_id") or "").casefold()
    url = str(record.get("url") or "").casefold()
    payload = _mapping(record.get("raw_payload"))
    payload_type = payload.get("github_item_type")
    return (
        record.get("content_class") == "project_tool"
        and payload_type not in {"release"}
        and not external_id.startswith("github_release:")
        and (
            payload_type == "repository"
            or source_type in {"github_api", "github_trending"}
            or source_id.startswith("github_")
            or external_id.startswith("github_repo:")
            or "github.com/" in url
        )
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _repo_name(record: Mapping[str, Any]) -> str:
    title = str(record.get("title") or "").removeprefix("GitHub repo:").strip()
    if "/" in title:
        return title
    payload = _mapping(record.get("raw_payload"))
    return str(payload.get("full_name") or title or "unknown/repository")


def _number(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _count(value: Any) -> str:
    number = _number(value)
    return f"{number:,}" if number else "-"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(entry) for entry in value if str(entry).strip()]


def _project_topics(metrics: Mapping[str, Any]) -> list[str]:
    """Combine repository topics and Search query topics without duplicates."""
    topics: list[str] = []
    for value in (_string_list(metrics.get("topics")), _string_list(metrics.get("search_topics"))):
        for topic in value:
            if topic not in topics:
                topics.append(topic)
    return topics


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
