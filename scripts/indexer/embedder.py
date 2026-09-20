"""Voyage AI embedding client with token-budget batching and basic retry."""
from __future__ import annotations
import os
import time
import tiktoken
import voyageai
from . import config

_client = None
_enc = tiktoken.get_encoding("cl100k_base")


def client() -> voyageai.Client:
    global _client
    if _client is None:
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise RuntimeError("VOYAGE_API_KEY not set in environment")
        _client = voyageai.Client(api_key=key)
    return _client


def _send(batch: list[str]) -> list[list[float]]:
    for attempt in range(5):
        try:
            resp = client().embed(
                batch,
                model=config.EMBED_MODEL,
                input_type=config.EMBED_INPUT_TYPE_DOC,
                output_dimension=config.EMBED_DIM,
            )
            return resp.embeddings
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    return []  # unreachable


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch by combined token budget (Voyage hard limit: 120k tokens/batch)."""
    out: list[list[float]] = []
    i = 0
    n = len(texts)
    while i < n:
        batch: list[str] = []
        batch_tokens = 0
        while i < n and len(batch) < config.EMBED_BATCH_MAX_ITEMS:
            t = texts[i]
            tt = len(_enc.encode(t, disallowed_special=()))
            if batch and batch_tokens + tt > config.EMBED_BATCH_MAX_TOKENS:
                break
            batch.append(t)
            batch_tokens += tt
            i += 1
        out.extend(_send(batch))
    return out


def embed_query(text: str) -> list[float]:
    resp = client().embed(
        [text],
        model=config.EMBED_MODEL,
        input_type=config.EMBED_INPUT_TYPE_QUERY,
        output_dimension=config.EMBED_DIM,
    )
    return resp.embeddings[0]
