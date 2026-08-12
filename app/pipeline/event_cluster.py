"""Deterministic event identity and candidate clustering helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from app.ai.schemas import ClusterDecision


_STOPWORDS = {
    "a", "an", "and", "for", "from", "new", "the", "to", "of", "in", "on", "with",
    "发布", "推出", "上线", "更新", "官方", "ai", "model", "release", "update",
}


@dataclass(frozen=True)
class ClusterCandidate:
    item: Any
    key: str
    section: str | None
    event_type: str | None
    title_tokens: frozenset[str]
    entities: frozenset[str]


def canonical_event_key(item: Any) -> str:
    """Return an exact, stable event identity when one is available.

    Exact keys are intentionally conservative: canonical URL, GitHub
    repository+release, arXiv ID, and DOI.  Items without one receive a
    deterministic ``hint`` key so they can still be grouped for review.
    """

    values = _mapping(item)
    canonical_url = _canonical_url(values.get("canonical_url") or values.get("source_url") or values.get("url"))
    repository = _text(values.get("repository") or values.get("repo") or values.get("github_repo"))
    release = _text(values.get("release") or values.get("release_tag") or values.get("version"))
    external_id = _text(values.get("external_id")) or ""
    if not repository and external_id.casefold().startswith("github_repo:"):
        repository = external_id.split(":", 1)[1]
    if repository:
        repository = repository.casefold().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if release:
            return f"github:{repository}@{release.casefold()}"
        # A repository URL is an exact identity even when no release is known.
        if canonical_url and "github.com/" in canonical_url.casefold():
            return f"github:{repository}"
    arxiv = _text(values.get("arxiv_id") or values.get("arxiv") or values.get("paper_id"))
    if not arxiv and external_id.casefold().startswith("arxiv:"):
        arxiv = external_id.split(":", 1)[1]
    if arxiv:
        return f"arxiv:{arxiv.casefold().removeprefix("arxiv:").strip()}"
    doi = _text(values.get("doi"))
    if not doi and external_id.casefold().startswith("doi:"):
        doi = external_id.split(":", 1)[1]
    if doi:
        return f"doi:{doi.casefold().removeprefix("https://doi.org/").strip()}"
    if canonical_url:
        return f"url:{canonical_url}"
    hint = _text(values.get("event_hint") or values.get("title")) or "unknown"
    return "hint:" + hashlib.sha256(_normalise_text(hint).encode("utf-8")).hexdigest()[:24]


def cluster_candidates(
    candidates: Iterable[Any],
    *,
    section: str | None = None,
    event_type: str | None = None,
    title_threshold: float = 0.45,
) -> list[list[Any]]:
    """Group exact matches and likely fuzzy candidates without AI side effects."""

    rows = [_candidate(value) for value in candidates]
    groups: list[list[ClusterCandidate]] = []
    for row in rows:
        if section and row.section != section:
            continue
        if event_type and row.event_type != event_type:
            continue
        placed = False
        for group in groups:
            anchor = group[0]
            same_dimensions = (
                (row.section is None or anchor.section is None or row.section == anchor.section)
                and (row.event_type is None or anchor.event_type is None or row.event_type == anchor.event_type)
            )
            exact = row.key == anchor.key and not row.key.startswith("hint:")
            phrase_entity = bool(row.entities & anchor.entities)
            similarity = _jaccard(row.title_tokens, anchor.title_tokens)
            if same_dimensions and (exact or phrase_entity or similarity >= title_threshold):
                group.append(row)
                placed = True
                break
        if not placed:
            groups.append([row])
    return [[member.item for member in group] for group in groups]


def accept_cluster_decision(decision: ClusterDecision | Mapping[str, Any] | Any) -> bool:
    """Return whether an AI judgement may merge two candidates locally."""

    values = _mapping(decision)
    value = _text(values.get("decision"))
    confidence = _number(values.get("confidence"))
    if value not in {"merge", "related", "separate", "uncertain"}:
        return False
    return value == "merge" and confidence >= 80


def merge_cluster_decision(decision: ClusterDecision | Mapping[str, Any] | Any) -> bool:
    """Compatibility alias with an explicit name for callers."""

    return accept_cluster_decision(decision)


def _candidate(value: Any) -> ClusterCandidate:
    values = _mapping(value)
    title = _text(values.get("title") or values.get("original_title")) or ""
    entities = values.get("entities") or values.get("entity_names") or []
    if isinstance(entities, str):
        entities = re.split(r"[,，;；]", entities)
    return ClusterCandidate(
        item=value,
        key=canonical_event_key(value),
        section=_text(values.get("section")),
        event_type=_text(values.get("event_type") or values.get("type")),
        title_tokens=frozenset(_tokens(title)),
        entities=frozenset(_normalise_text(str(entity)) for entity in entities if _text(entity)),
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _canonical_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.casefold().rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return text.casefold().rstrip("/")
    host = (parsed.hostname or parsed.netloc).casefold()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), host, path, parsed.query, ""))


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]+", _normalise_text(value)) if token not in _STOPWORDS and len(token) > 1}


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["ClusterCandidate", "accept_cluster_decision", "canonical_event_key", "cluster_candidates", "merge_cluster_decision"]
