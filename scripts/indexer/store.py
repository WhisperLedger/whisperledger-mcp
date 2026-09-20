"""Qdrant collection setup and upserts.

Collection routing: most callers use the default public collection
(config.QDRANT_COLLECTION). Restricted repos (see ~/.config/jarvis/restricted_acl.json)
live in their own per-privilege-tier collection; callers pass an explicit
'collection' argument in that case. The 'collection' argument on every
mutating call is authoritative — the module never re-derives it from repo name.
"""
from __future__ import annotations
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, PayloadSchemaType,
)
from . import config

_client = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=config.QDRANT_URL)
    return _client


def ensure_collection(collection: str | None = None) -> None:
    name = collection or config.QDRANT_COLLECTION
    c = client()
    existing = {col.name for col in c.get_collections().collections}
    if name not in existing:
        c.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE),
        )
        for field, schema in [
            ('repo', PayloadSchemaType.KEYWORD),
            ('path', PayloadSchemaType.KEYWORD),
            ('ext', PayloadSchemaType.KEYWORD),
            ('language', PayloadSchemaType.KEYWORD),
            ('symbols', PayloadSchemaType.KEYWORD),
        ]:
            c.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=schema,
            )


def ensure_prs_collection() -> None:
    c = client()
    existing = {col.name for col in c.get_collections().collections}
    if config.QDRANT_COLLECTION_PRS not in existing:
        c.create_collection(
            collection_name=config.QDRANT_COLLECTION_PRS,
            vectors_config=VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE),
        )
        for field, schema in [
            ('repo', PayloadSchemaType.KEYWORD),
            ('state', PayloadSchemaType.KEYWORD),
            ('author', PayloadSchemaType.KEYWORD),
        ]:
            c.create_payload_index(
                collection_name=config.QDRANT_COLLECTION_PRS,
                field_name=field,
                field_schema=schema,
            )


def upsert_points(points: list[PointStruct], collection: str | None = None) -> None:
    if not points:
        return
    client().upsert(
        collection_name=collection or config.QDRANT_COLLECTION,
        points=points,
        wait=False,
    )


def upsert_pr_points(points: list[PointStruct]) -> None:
    upsert_points(points, collection=config.QDRANT_COLLECTION_PRS)


def delete_repo_prs(repo: str) -> int:
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    res = client().delete(
        collection_name=config.QDRANT_COLLECTION_PRS,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key='repo', match=MatchValue(value=repo))])
        ),
        wait=True,
    )
    return getattr(res, 'operation_id', 0)


def delete_repo(repo: str, collection: str | None = None) -> int:
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    res = client().delete(
        collection_name=collection or config.QDRANT_COLLECTION,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key='repo', match=MatchValue(value=repo))])
        ),
        wait=True,
    )
    return getattr(res, 'operation_id', 0)


def delete_chunks_for_paths(repo: str, paths: list[str], collection: str | None = None) -> int:
    if not paths:
        return 0
    from qdrant_client.models import (
        Filter, FieldCondition, MatchValue, MatchAny, FilterSelector,
    )
    res = client().delete(
        collection_name=collection or config.QDRANT_COLLECTION,
        points_selector=FilterSelector(
            filter=Filter(must=[
                FieldCondition(key='repo', match=MatchValue(value=repo)),
                FieldCondition(key='path', match=MatchAny(any=paths)),
            ])
        ),
        wait=True,
    )
    return getattr(res, 'operation_id', 0)
