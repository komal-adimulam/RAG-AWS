import asyncio
import json
import math
import os
import time
from rag_aws.retrieval import retrieve_parent_chunks


def calculate_mrr(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """Calculate Reciprocal Rank (RR) for a single query."""
    if not expected_ids:
        return 0.0

    expected_set = set(expected_ids)
    for rank, p_id in enumerate(retrieved_ids, start=1):
        if p_id in expected_set:
            return 1.0 / rank
    return 0.0


def calculate_recall(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """Calculate Recall for a single query (1.0 if any expected chunk retrieved)."""
    if not expected_ids:
        return 1.0  # Out of scope - successfully skipped

    expected_set = set(expected_ids)
    retrieved_set = set(retrieved_ids)

    # Number of expected chunks successfully retrieved
    hits = len(expected_set.intersection(retrieved_set))
    return hits / len(expected_set)


def calculate_ndcg(
    retrieved_ids: list[str], expected_ids: list[str], k: int | None = None
) -> float:
    """Calculate Normalized Discounted Cumulative Gain (NDCG@K)."""
    if not expected_ids:
        return 1.0  # Out of scope - successfully skipped

    top_k = retrieved_ids[:k] if k is not None else retrieved_ids
    if not top_k:
        return 0.0

    expected_set = set(expected_ids)

    # 1. Compute DCG (Discounted Cumulative Gain)
    dcg = 0.0
    for rank, p_id in enumerate(top_k, start=1):
        if p_id in expected_set:
            dcg += 1.0 / math.log2(rank + 1)

    # 2. Compute IDCG (Ideal DCG)
    num_relevant = min(len(expected_set), len(top_k))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, num_relevant + 1))

    return (dcg / idcg) if idcg > 0.0 else 0.0


async def run_retrieval_benchmark():
    print("==================================================")
    print("Running Offline Retrieval Benchmark (Layer 1)")
    print("==================================================\n")

    # Load ground truth dataset
    dataset_file = "evaluation_dataset.json"
    if not os.path.exists(dataset_file) and os.path.exists("evalutation/dataset/evaluation_dataset.json"):
        dataset_file = "evalutation/dataset/evaluation_dataset.json"
    elif not os.path.exists(dataset_file) and os.path.exists("dataset/evaluation_dataset.json"):
        dataset_file = "dataset/evaluation_dataset.json"

    with open(dataset_file, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    results = []
    total_start_time = time.perf_counter()

    for item in dataset:
        q_id = item.get("type")
        query = item["question"]
        expected_parent_ids = item["expected_parent_ids"]

        # Skip out of scope for pure context retrieval benchmarks
        if q_id == "out_of_scope":
            continue

        query_start = time.perf_counter()

        # Run retrieval pipeline
        parent_records = await retrieve_parent_chunks(query, as_documents=False)

        elapsed_ms = (time.perf_counter() - query_start) * 1000
        retrieved_ids = [record["id"] for record in parent_records]

        # Compute IR metrics
        mrr = calculate_mrr(retrieved_ids, expected_parent_ids)
        recall = calculate_recall(retrieved_ids, expected_parent_ids)
        ndcg = calculate_ndcg(retrieved_ids, expected_parent_ids)

        hit_status = "HIT" if recall > 0 else "MISS"

        # Find position of expected chunk
        rank_str = "N/A"
        if expected_parent_ids and expected_parent_ids[0] in retrieved_ids:
            rank_str = f"#{retrieved_ids.index(expected_parent_ids[0]) + 1}"

        print(
            f"[{hit_status:4s}] [{q_id.upper()}] Rank: {rank_str:<4} | NDCG: {ndcg:.2f} | Latency: {elapsed_ms:.1f}ms"
        )
        print(f"  Q: {query[:75]}...")

        results.append(
            {
                "type": q_id,
                "mrr": mrr,
                "recall": recall,
                "ndcg": ndcg,
                "latency_ms": elapsed_ms,
                "retrieved_count": len(retrieved_ids),
            }
        )

    total_elapsed = time.perf_counter() - total_start_time

    # Aggregate Statistics
    num_queries = len(results)
    avg_mrr = sum(r["mrr"] for r in results) / num_queries if num_queries else 0
    avg_recall = (
        sum(r["recall"] for r in results) / num_queries if num_queries else 0
    )
    avg_ndcg = (
        sum(r["ndcg"] for r in results) / num_queries if num_queries else 0
    )
    avg_latency = (
        sum(r["latency_ms"] for r in results) / num_queries
        if num_queries
        else 0
    )

    print("\n" + "=" * 50)
    print("RETRIEVAL BENCHMARK SUMMARY")
    print("=" * 50)
    print(f"Total Evaluated Queries : {num_queries}")
    print(f"Mean Reciprocal Rank    : {avg_mrr:.4f}  (Target: >= 0.80)")
    print(f"Recall@TopK             : {avg_recall:.4f}  (Target: >= 0.85)")
    print(f"NDCG@TopK               : {avg_ndcg:.4f}  (Target: >= 0.80)")
    print(f"Avg Retrieval Latency   : {avg_latency:.1f} ms")
    print(f"Total Test Duration     : {total_elapsed:.2f} s")
    print("=" * 50)

    # Pass / Fail Verdict
    if avg_recall >= 0.85 and avg_mrr >= 0.80 and avg_ndcg >= 0.80:
        print(
            "\nVERDICT: RETRIEVAL PASSED! Ready for Ragas End-to-End Evaluation."
        )
    else:
        print(
            "\nVERDICT: RETRIEVAL BELOW SLA. Consider adjusting RRF weights or search limits."
        )


if __name__ == "__main__":
    asyncio.run(run_retrieval_benchmark())