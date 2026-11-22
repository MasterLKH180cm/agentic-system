"""Bounded lexical memory recall with tenant, namespace and expiry boundaries."""
import re

from agentic.schemas import MemoryIn, NAME_PATTERN
from agentic.features import Features
from agentic.store import Store


STOPWORDS = {"the", "a", "an", "is", "are", "what", "when", "does", "do", "of", "to", "in", "and", "for"}


def terms(text: str) -> set[str]:
    return {token for token in re.findall(r"[^\W_]+", text.lower()) if token not in STOPWORDS}


class Memory:
    def __init__(self, store: Store, features: Features | None = None):
        self.store = store
        self.features = features if features is not None else Features.from_env()

    @staticmethod
    def validate(namespace: str, record_id: str | None = None) -> None:
        if not re.fullmatch(NAME_PATTERN, namespace) or (
            record_id is not None and not re.fullmatch(NAME_PATTERN, record_id)
        ):
            raise ValueError("Invalid memory namespace or ID")

    def put(self, tenant: str, namespace: str, record_id: str, record: MemoryIn) -> dict:
        self.features.require("memory")
        self.validate(namespace, record_id)
        return self.store.put_memory(tenant, namespace, record_id, record)

    def search(self, tenant: str, namespace: str, query: str, limit: int = 5) -> list[dict]:
        self.features.require("memory")
        self.validate(namespace)
        if len(query) > 2000 or not 1 <= limit <= 10:
            raise ValueError("Invalid memory query bounds")
        query_terms = terms(query)
        candidates = self.store.memory_candidates(tenant, namespace, sorted(query_terms)[:30])
        ranked = []
        for row in candidates:
            overlap = len(query_terms & terms(row["content"]))
            score = overlap / max(1, len(query_terms))
            if not query_terms or overlap:
                ranked.append({**row, "score": round(score, 4), "search_type": "lexical"})
        return sorted(ranked, key=lambda row: (-row["score"], row["id"]))[:limit]
