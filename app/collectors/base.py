"""Canonical collector contract for the v2 ingestion flow."""

from __future__ import annotations

from typing import Mapping, Protocol

from app.domain.models import FetchBatch, SourceSpec


class Collector(Protocol):
    def collect(
        self,
        source: SourceSpec,
        limit: int,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchBatch:
        """Fetch and map one source without database or AI side effects."""

__all__ = ["Collector"]
