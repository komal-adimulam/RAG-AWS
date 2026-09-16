import asyncio
import logging
import os
import time
from typing import Any

import psycopg2
from dotenv import load_dotenv
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from pgvector.psycopg2 import register_vector

load_dotenv()

logger = logging.getLogger(__name__)

# Database connection parameters
DB_PARAMS = {
    "dbname": os.getenv("POSTGRES_DB", "rag_hierarchical"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
}

# Number of top candidate child chunks selected directly after RRF fusion
TOP_K_FUSED = 8

# RRF fusion weights for hybrid search
DENSE_WEIGHT = 0.7
SPARSE_WEIGHT = 0.3

# Global module state for cached models and retriever
_embeddings_model: HuggingFaceEmbeddings | None = None
_bm25_retriever: BM25Retriever | None = None


def format_timing_summary(timings: dict[str, Any]) -> str:
    """Format per-stage latency timings into a single-line summary string."""
    def _fmt(val: float | None) -> str:
        if val is None:
            return "0.00s"
        formatted = f"{val:.2f}"
        if val >= 0.1 and formatted.endswith("0"):
            formatted = f"{val:.1f}"
        return f"{formatted}s"

    embed_s = _fmt(timings.get("embed", 0.0))
    dense_db_s = _fmt(timings.get("dense_db", 0.0))
    sparse_bm25_s = _fmt(timings.get("sparse_bm25", 0.0))
    concurrent_total_s = _fmt(timings.get("concurrent_total", 0.0))
    rrf_s = _fmt(timings.get("rrf", 0.0))
    parent_fetch_s = _fmt(timings.get("parent_fetch", 0.0))
    parents = timings.get("parents", 0)
    context_build_s = _fmt(timings.get("context_build", 0.0))
    generation_s = _fmt(timings.get("generation", 0.0))
    total_s = _fmt(timings.get("total", 0.0))

    return (
        f"[Timing] embed={embed_s} | dense_db={dense_db_s} | sparse_bm25={sparse_bm25_s} | "
        f"concurrent_total={concurrent_total_s} | rrf={rrf_s} | "
        f"parent_fetch={parent_fetch_s} (parents={parents}) | "
        f"context_build={context_build_s} | generation={generation_s} | total={total_s}"
    )


def get_db_connection():
    """Establish connection to PostgreSQL and register the pgvector extension."""
    conn = psycopg2.connect(**DB_PARAMS)
    register_vector(conn)
    return conn


def get_embeddings_model() -> HuggingFaceEmbeddings:
    """Get or initialize the BAAI BGE-Large embedding model."""
    global _embeddings_model
    if _embeddings_model is None:
        _embeddings_model = HuggingFaceEmbeddings(
            model_name="BAAI/bge-large-en-v1.5",
            encode_kwargs={"normalize_embeddings": True},
        )
    return _embeddings_model


def load_bm25_retriever(k: int = 15) -> BM25Retriever:
    """Load all child chunks from PostgreSQL to initialize BM25Retriever."""
    global _bm25_retriever
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, parent_id, text_content, page, source FROM child_chunks;"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    documents = [
        Document(
            page_content=row[2],
            metadata={
                "id": str(row[0]),
                "parent_id": str(row[1]),
                "page": row[3],
                "source": row[4],
            },
        )
        for row in rows
    ]

    retriever = BM25Retriever.from_documents(documents)
    retriever.k = k
    _bm25_retriever = retriever
    return _bm25_retriever


def get_bm25_retriever(k: int = 15) -> BM25Retriever:
    """Get or lazily initialize the BM25Retriever."""
    global _bm25_retriever
    if _bm25_retriever is None:
        _bm25_retriever = load_bm25_retriever(k=k)
    return _bm25_retriever


def refresh_bm25_retriever(k: int = 15) -> BM25Retriever:
    """Force reload of BM25Retriever from PostgreSQL child_chunks.

    Bypasses the cached _bm25_retriever and reloads all child chunks from the
    database. Call this manually after re-ingesting chunks.
    """
    global _bm25_retriever
    _bm25_retriever = load_bm25_retriever(k=k)
    logger.info("BM25 retriever successfully refreshed from PostgreSQL.")
    return _bm25_retriever


# Pre-load embeddings and BM25 retriever if database is available
try:
    _embeddings_model = get_embeddings_model()
    _bm25_retriever = load_bm25_retriever()
except Exception as _e:
    logger.warning("Pre-loading failed, will lazy load on first retrieval: %s", _e)


def _dense_search_sync(
    query: str, limit: int = 15, timings: dict[str, Any] | None = None
) -> list[Document]:
    """Execute dense vector search on child_chunks using pgvector <=> operator."""
    embeddings = get_embeddings_model()
    t_embed_start = time.perf_counter()
    query_vector = embeddings.embed_query(query)
    embed_time = time.perf_counter() - t_embed_start

    t_db_start = time.perf_counter()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, parent_id, text_content, page, source, (embedding <=> %s::vector) AS distance
                FROM child_chunks
                ORDER BY embedding <=> %s::vector ASC
                LIMIT %s;
                """,
                (query_vector, query_vector, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    dense_db_time = time.perf_counter() - t_db_start

    if timings is not None:
        timings["embed"] = embed_time
        timings["dense_db"] = dense_db_time

    return [
        Document(
            page_content=row[2],
            metadata={
                "id": str(row[0]),
                "parent_id": str(row[1]),
                "page": row[3],
                "source": row[4],
                "distance": float(row[5]),
            },
        )
        for row in rows
    ]


def _sparse_search_sync(
    query: str, limit: int = 15, timings: dict[str, Any] | None = None
) -> list[Document]:
    """Execute sparse keyword search on child_chunks using BM25."""
    bm25 = get_bm25_retriever(k=limit)
    bm25.k = limit
    t_sparse_start = time.perf_counter()
    results = bm25.invoke(query)
    sparse_time = time.perf_counter() - t_sparse_start

    if timings is not None:
        timings["sparse_bm25"] = sparse_time

    return results


def reciprocal_rank_fusion(
    ranked_lists: list[list[Document]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[Document]:
    """Combine ranked candidate lists using Reciprocal Rank Fusion, with optional weights."""
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    rrf_scores: dict[str, float] = {}
    doc_map: dict[str, Document] = {}

    for doc_list, weight in zip(ranked_lists, weights):
        for rank, doc in enumerate(doc_list, start=1):
            doc_id = doc.metadata.get("id") or doc.page_content
            if doc_id not in doc_map:
                doc_map[doc_id] = doc
                rrf_scores[doc_id] = 0.0
            rrf_scores[doc_id] += weight * (1.0 / (k + rank))

    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [doc_map[doc_id] for doc_id in sorted_ids]


def _fetch_parent_chunks_sync(parent_ids: list[str]) -> list[dict[str, Any]]:
    """Query parent_chunks table and return records preserving parent_ids order."""
    if not parent_ids:
        return []

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, text_content, category, service, page, source
                FROM parent_chunks
                WHERE id = ANY(%s::uuid[]);
                """,
                (parent_ids,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    parent_map = {
        str(row[0]): {
            "id": str(row[0]),
            "text_content": row[1],
            "category": row[2],
            "service": row[3],
            "page": row[4],
            "source": row[5],
        }
        for row in rows
    }

    # Strictly preserve the rank order of parent_ids
    return [parent_map[pid] for pid in parent_ids if pid in parent_map]


async def retrieve_parent_chunks(
    query: str,
    dense_limit: int = 15,
    sparse_limit: int = 15,
    as_documents: bool = False,
    timings: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | list[Document]:
    """Asynchronously retrieve parent context chunks for a query.

    Pipeline:
    1. Concurrently run Dense (pgvector <=>) and Sparse (BM25) searches via asyncio.gather.
    2. Fuse candidate lists using Reciprocal Rank Fusion (RRF).
    3. Select top 8 child chunks directly from fused candidate list.
    4. Extract parent IDs, strictly deduplicating while preserving fused rank order.
    5. Fetch corresponding records from parent_chunks.

    Args:
        query: User query string.
        dense_limit: Max dense vector search candidates (default: 15).
        sparse_limit: Max BM25 keyword search candidates (default: 15).
        as_documents: If True, return LangChain Document objects instead of dicts.
        timings: Optional dict to collect per-stage latency timings.

    Returns:
        List of dictionaries (or Document objects) with keys:
        'id', 'text_content', 'category', 'service', 'page', 'source'.
    """
    dense_timings: dict[str, Any] = {}
    sparse_timings: dict[str, Any] = {}

    # 1. Concurrent dense and sparse search on child_chunks
    t_gather_start = time.perf_counter()
    dense_task = asyncio.to_thread(_dense_search_sync, query, dense_limit, dense_timings)
    sparse_task = asyncio.to_thread(_sparse_search_sync, query, sparse_limit, sparse_timings)
    dense_results, sparse_results = await asyncio.gather(dense_task, sparse_task)
    concurrent_total = time.perf_counter() - t_gather_start

    # 2. Reciprocal Rank Fusion (weighted: DENSE_WEIGHT=0.7, SPARSE_WEIGHT=0.3)
    t_rrf_start = time.perf_counter()
    fused_candidates = reciprocal_rank_fusion(
        [dense_results, sparse_results],
        weights=[DENSE_WEIGHT, SPARSE_WEIGHT],
    )
    rrf_time = time.perf_counter() - t_rrf_start

    # 3. Take top 8 candidates directly from fused list
    top_child_chunks = fused_candidates[:TOP_K_FUSED]

    # 4. Extract parent_ids, strictly deduplicating while preserving fused rank order
    unique_parent_ids: list[str] = []
    seen_parent_ids: set[str] = set()
    for child in top_child_chunks:
        parent_id = child.metadata.get("parent_id")
        if parent_id and parent_id not in seen_parent_ids:
            seen_parent_ids.add(parent_id)
            unique_parent_ids.append(parent_id)

    # 5. Fetch parent chunks from PostgreSQL
    parents_count = len(unique_parent_ids)
    t_parent_start = time.perf_counter()
    parent_records = await asyncio.to_thread(
        _fetch_parent_chunks_sync, unique_parent_ids
    )
    parent_fetch_time = time.perf_counter() - t_parent_start

    if timings is not None:
        timings.update(dense_timings)
        timings.update(sparse_timings)
        timings["concurrent_total"] = concurrent_total
        timings["rrf"] = rrf_time
        timings["parent_fetch"] = parent_fetch_time
        timings["parents"] = parents_count

    if as_documents:
        return [
            Document(
                page_content=record["text_content"],
                metadata={
                    "id": record["id"],
                    "category": record["category"],
                    "service": record["service"],
                    "page": record["page"],
                    "source": record["source"],
                },
            )
            for record in parent_records
        ]

    return parent_records
