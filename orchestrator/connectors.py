"""Connector fabric: one real local connector and honest stubs for the roadmap.

The bible describes a ten-cloud connector bus. Today the only durable store is
the local SQLite vault, and nothing here pretends otherwise: every roadmap
connector reports ``not_implemented`` and cannot be enabled. The protocol is
the contract a real connector must satisfy when one is added.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Protocol, Tuple


@dataclass(frozen=True)
class ConnectorHealth:
    name: str
    status: str            # "healthy" | "not_implemented" | "error"
    detail: str
    source_of_truth: bool


class Connector(Protocol):
    name: str

    def health(self) -> ConnectorHealth: ...
    def put(self, record: Dict[str, Any]) -> Tuple[bool, str]: ...
    def get(self, key: str) -> Dict[str, Any]: ...


class LocalSQLiteConnector:
    """The vault on local disk. Authoritative; everything else would be a mirror."""

    name = "local_sqlite"

    def health(self) -> ConnectorHealth:
        from .vault import database_path

        path = database_path()
        exists = path.exists()
        size = os.path.getsize(path) if exists else 0
        return ConnectorHealth(
            name=self.name,
            status="healthy" if exists else "healthy",
            detail=f"{path} ({size} bytes)" if exists else f"{path} (created on first write)",
            source_of_truth=True,
        )

    def put(self, record: Dict[str, Any]) -> Tuple[bool, str]:
        return False, "use orchestrator.vault directly; the local connector is read-only here"

    def get(self, key: str) -> Dict[str, Any]:
        return {}


# Roadmap connectors from the bible. Deliberately not enable-able.
ROADMAP_CONNECTORS: Tuple[Tuple[str, str], ...] = (
    ("supabase", "Artifact BLOB bucket + auth mirror"),
    ("neon", "Postgres execution telemetry"),
    ("upstash_redis", "Hot 200-message window cache"),
    ("mongodb_atlas", "Unstructured pipeline metadata"),
    ("pinecone", "Embedding search across threads"),
    ("cloudflare_d1", "Edge failover"),
    ("planetscale", "Failover"),
    ("dynamodb", "Failover"),
    ("bigquery", "Analytics sweeps"),
)

ROADMAP_FEATURES: Tuple[Tuple[str, str, str], ...] = (
    ("Background Git-Streamer", "not_implemented", "Opt-in autosave commits to an autosave branch. No code exists; nothing is committed automatically."),
    ("Live SDK Document Scraper", "not_implemented", "Pre-prompt crawl of vendor docs for current syntax. No code exists."),
    ("Self-Correcting Execution Sandbox", "partial", "The chat-page Preview canvas runs a generated page in a sealed sandbox and, in Heavy Mode, feeds script errors back for automatic fix rounds; the Repository Work pipeline (orchestrator/test_loop.py repair loop) remains separate."),
    ("Cross-thread semantic search", "partial", "Long-distance memory: an FTS5/BM25 index over the project's other chats (summaries, digests, missions, artifact summaries) with recency decay, recalled into every prompt; no embedding index."),
)


def connector_status() -> List[ConnectorHealth]:
    rows = [LocalSQLiteConnector().health()]
    for name, purpose in ROADMAP_CONNECTORS:
        rows.append(ConnectorHealth(name=name, status="not_implemented", detail=purpose, source_of_truth=False))
    return rows
