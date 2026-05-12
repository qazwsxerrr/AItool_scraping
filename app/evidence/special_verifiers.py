from __future__ import annotations

import base64
import json
import re
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx

from app.evidence.fetcher import EvidenceFetchResult
from app.storage.models import EvidenceItem


class SupportsGet(Protocol):
    def get(self, url: str, *, headers: dict[str, str]): ...


class CompositeSpecialVerifier:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        http_client: SupportsGet | None = None,
    ) -> None:
        self.github = GitHubEvidenceVerifier(user_agent=user_agent, timeout_seconds=timeout_seconds, http_client=http_client)
        self.huggingface = HuggingFaceEvidenceVerifier(user_agent=user_agent, timeout_seconds=timeout_seconds, http_client=http_client)

    def verify(self, evidence: EvidenceItem) -> EvidenceFetchResult | None:
        if evidence.evidence_type == "github_repo" or "github.com" in evidence.url:
            return self.github.verify(evidence)
        if evidence.evidence_type == "huggingface_model" or "huggingface.co" in evidence.url:
            return self.huggingface.verify(evidence)
        return None


class GitHubEvidenceVerifier:
    def __init__(
        self,
        *,
        user_agent: str = "AItool_scraping/0.1 (+https://example.local)",
        timeout_seconds: float = 20.0,
        http_client: SupportsGet | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    def verify(self, evidence: EvidenceItem) -> EvidenceFetchResult | None:
        repo = _parse_github_repo(evidence.url)
        if repo is None:
            return None
        owner, name = repo
        repo_url = f"https://api.github.com/repos/{owner}/{name}"
        readme_url = f"https://api.github.com/repos/{owner}/{name}/readme"
        headers = {"User-Agent": self.user_agent, "Accept": "application/vnd.github+json"}

        repo_response = self._get(repo_url, headers=headers)
        if repo_response.status_code == 404:
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=evidence.url,
                http_status=404,
                url_validation_status="unreachable",
                fetched_title=f"{owner}/{name}",
                fetched_description=None,
                fetched_text_preview=None,
                raw_payload={"provider": "github", "repo_exists": False, "risk_flags": ["broken_github_repo"]},
            )
        repo_data = repo_response.json()

        readme_exists = False
        readme_preview = None
        readme_response = self._get(readme_url, headers=headers)
        if readme_response.status_code == 200:
            readme_data = readme_response.json()
            readme_preview = _decode_github_content(readme_data.get("content"))[:4000]
            readme_exists = bool(readme_preview)

        quality_flags: list[str] = []
        risk_flags: list[str] = []
        if readme_exists:
            quality_flags.append("readme_exists")
        if _has_install_keywords(readme_preview or ""):
            quality_flags.append("install_docs")
        license_value = _extract_license(repo_data)
        if license_value:
            quality_flags.append("has_license")
        else:
            risk_flags.append("no_license")
        if repo_data.get("archived"):
            risk_flags.append("archived_repo")

        payload = {
            "provider": "github",
            "repo_exists": True,
            "owner": owner,
            "repo": name,
            "description": repo_data.get("description"),
            "stars": repo_data.get("stargazers_count"),
            "forks": repo_data.get("forks_count"),
            "open_issues": repo_data.get("open_issues_count"),
            "archived": repo_data.get("archived"),
            "disabled": repo_data.get("disabled"),
            "private": repo_data.get("private"),
            "license": license_value,
            "default_branch": repo_data.get("default_branch"),
            "created_at": repo_data.get("created_at"),
            "updated_at": repo_data.get("updated_at"),
            "pushed_at": repo_data.get("pushed_at"),
            "readme_exists": readme_exists,
            "readme_preview": readme_preview,
            "topics": repo_data.get("topics") or [],
            "languages": [repo_data.get("language")] if repo_data.get("language") else [],
            "quality_flags": quality_flags,
            "risk_flags": risk_flags,
        }
        return EvidenceFetchResult(
            url=evidence.url,
            final_url=evidence.url,
            http_status=repo_response.status_code,
            url_validation_status="reachable",
            fetched_title=f"{owner}/{name}",
            fetched_description=repo_data.get("description"),
            fetched_text_preview=readme_preview,
            raw_payload=payload,
        )

    def _get(self, url: str, *, headers: dict[str, str]):
        if self._http_client is not None:
            return self._http_client.get(url, headers=headers)
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            return client.get(url, headers=headers)


class HuggingFaceEvidenceVerifier:
    def __init__(
        self,
        *,
        user_agent: str = "AItool_scraping/0.1 (+https://example.local)",
        timeout_seconds: float = 20.0,
        http_client: SupportsGet | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    def verify(self, evidence: EvidenceItem) -> EvidenceFetchResult | None:
        model_id = _parse_huggingface_model_id(evidence.url)
        if model_id is None:
            return None
        api_url = f"https://huggingface.co/api/models/{quote(model_id, safe='/')}"
        response = self._get(api_url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        if response.status_code == 404:
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=evidence.url,
                http_status=404,
                url_validation_status="unreachable",
                fetched_title=model_id,
                fetched_description=None,
                fetched_text_preview=None,
                raw_payload={"provider": "huggingface", "model_exists": False, "risk_flags": ["broken_huggingface_model"]},
            )
        data = response.json()
        siblings = data.get("siblings") if isinstance(data.get("siblings"), list) else []
        filenames = [str(item.get("rfilename")) for item in siblings if isinstance(item, dict) and item.get("rfilename")]
        has_weights = any(name.endswith((".safetensors", ".bin", ".gguf", ".pt")) for name in filenames)
        has_config = any(name.endswith("config.json") for name in filenames)
        card_exists = bool(data.get("cardData") or data.get("model-index") or data.get("README"))
        license_value = _extract_hf_license(data)

        quality_flags: list[str] = []
        risk_flags: list[str] = []
        if card_exists:
            quality_flags.append("model_card")
        if has_weights:
            quality_flags.append("has_weights")
        else:
            risk_flags.append("no_weights")
        if license_value:
            quality_flags.append("has_license")
        if data.get("gated"):
            risk_flags.append("gated_model")

        payload = {
            "provider": "huggingface",
            "model_exists": True,
            "model_id": data.get("id") or model_id,
            "author": data.get("author"),
            "pipeline_tag": data.get("pipeline_tag"),
            "tags": data.get("tags") or [],
            "license": license_value,
            "likes": data.get("likes"),
            "downloads": data.get("downloads"),
            "last_modified": data.get("lastModified"),
            "card_exists": card_exists,
            "files_count": len(filenames),
            "has_safetensors": any(name.endswith(".safetensors") for name in filenames),
            "has_gguf": any(name.endswith(".gguf") for name in filenames),
            "has_config": has_config,
            "has_weights": has_weights,
            "is_gated": bool(data.get("gated")),
            "quality_flags": quality_flags,
            "risk_flags": risk_flags,
        }
        return EvidenceFetchResult(
            url=evidence.url,
            final_url=evidence.url,
            http_status=response.status_code,
            url_validation_status="reachable",
            fetched_title=data.get("id") or model_id,
            fetched_description=data.get("pipeline_tag"),
            fetched_text_preview=json.dumps({"tags": data.get("tags") or [], "files": filenames[:20]}, ensure_ascii=False),
            raw_payload=payload,
        )

    def _get(self, url: str, *, headers: dict[str, str]):
        if self._http_client is not None:
            return self._http_client.get(url, headers=headers)
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            return client.get(url, headers=headers)


def _parse_github_repo(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], re.sub(r"\.git$", "", parts[1])


def _parse_huggingface_model_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "huggingface.co":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] in {"spaces", "datasets", "docs", "blog"}:
        return None
    return f"{parts[0]}/{parts[1]}"


def _decode_github_content(content: Any) -> str:
    if not isinstance(content, str) or not content:
        return ""
    try:
        return base64.b64decode(content, validate=False).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_license(data: dict[str, Any]) -> str | None:
    license_data = data.get("license")
    if isinstance(license_data, dict):
        value = license_data.get("spdx_id") or license_data.get("key") or license_data.get("name")
        return str(value) if value else None
    return None


def _extract_hf_license(data: dict[str, Any]) -> str | None:
    card = data.get("cardData")
    if isinstance(card, dict) and card.get("license"):
        return str(card["license"])
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    for tag in tags:
        text = str(tag)
        if text.startswith("license:"):
            return text.split(":", 1)[1]
    return None


def _has_install_keywords(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ("install", "usage", "quickstart", "mcp", "server", "config"))
