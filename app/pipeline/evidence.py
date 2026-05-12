from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class EvidenceSeed:
    url: str
    evidence_type: str
    title: str | None
    snippet: str | None
    confidence: int
    raw_payload: dict


def build_evidence_queries(*, entity_name: str | None, entity_type: str | None) -> list[str]:
    if not entity_name:
        return []
    name = entity_name.strip()
    if not name:
        return []

    queries = [
        f"{name} official",
        f"{name} github",
        f"{name} documentation",
    ]
    normalized_type = (entity_type or "").lower()
    if normalized_type == "mcp":
        queries.append(f"{name} MCP server install")
    elif normalized_type == "workflow":
        queries.append(f"{name} workflow github")
    elif normalized_type == "model_release":
        queries.append(f"{name} huggingface model")
    elif normalized_type == "skill":
        queries.append(f"{name} skill install")

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


def build_direct_evidence_seeds(
    *,
    source_url: str | None,
    official_url: str | None,
    github_url: str | None,
    huggingface_url: str | None,
    producthunt_url: str | None,
) -> list[EvidenceSeed]:
    seeds: list[EvidenceSeed] = []
    for label, url in [
        ("source_url", source_url),
        ("official_url", official_url),
        ("github_url", github_url),
        ("huggingface_url", huggingface_url),
        ("producthunt_url", producthunt_url),
    ]:
        if not url:
            continue
        evidence_type = classify_evidence_type(url)
        seeds.append(
            EvidenceSeed(
                url=url,
                evidence_type=evidence_type,
                title=label,
                snippet=None,
                confidence=70 if label != "source_url" else 55,
                raw_payload={"source": "direct_claim_url", "field": label},
            )
        )
    return seeds


def classify_evidence_type(url: str) -> str:
    domain = extract_domain(url)
    if domain == "github.com" or domain.endswith(".github.com"):
        return "github_repo"
    if domain == "huggingface.co":
        return "huggingface_model"
    if domain == "producthunt.com" or domain.endswith(".producthunt.com"):
        return "producthunt_page"
    if any(part in domain for part in ("docs.", "readthedocs", "gitbook", "notion.site")):
        return "documentation"
    if domain:
        return "official_page"
    return "unknown"


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None
