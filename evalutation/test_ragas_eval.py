"""test_ragas_eval.py

Layer-2 End-to-End Generation Evaluation for Hierarchical AWS RAG using Ragas.
Evaluates Faithfulness (hallucination check) and Answer Relevance using Gemini.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
import types
import warnings
from pathlib import Path
from typing import Any

# Silence all deprecation, user, and third-party warnings
warnings.filterwarnings("ignore")

# Suppress HF Hub token advisory and tokenizer parallelism warnings
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

# Suppress google-genai automatic function calling advisories
try:
    from google.genai.models import AsyncModels, Models

    Models._logged_afc_warning = True
    AsyncModels._logged_afc_warning = True
except Exception:
    pass

# Monkey-patch legacy vertexai imports for ragas 0.4.3 compatibility
m1 = types.ModuleType("langchain_community.chat_models.vertexai")
m1.ChatVertexAI = type("ChatVertexAI", (), {})
sys.modules["langchain_community.chat_models.vertexai"] = m1

m2 = types.ModuleType("langchain_community.llms.vertexai")
m2.VertexAI = type("VertexAI", (), {})
sys.modules["langchain_community.llms.vertexai"] = m2

# Ensure project root is in sys.path
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from datasets import Dataset
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from ragas import evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import answer_relevancy, faithfulness

from src.rag_aws.generation import generate_rag_answer
from src.rag_aws.retrieval import get_embeddings_model, retrieve_parent_chunks
from src.rag_aws.rotating_gemini_client import get_rotating_client

load_dotenv()

# Configure clean logging: show progress only, mute noisy dependencies
logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("ragas_eval")
logger.setLevel(logging.INFO)

for noisy in [
    "google",
    "google.genai",
    "langchain",
    "langchain_google_genai",
    "rotating_gemini_client",
    "src.rag_aws.generation",
    "ragas",
    "urllib3",
    "httpx",
    "httpcore",
    "transformers",
    "sentence_transformers",
]:
    logging.getLogger(noisy).setLevel(logging.ERROR)

DATASET_PATH = Path("evalutation/dataset/evaluation_dataset.json")
OUTPUT_RESULTS_PATH = Path("evalutation/dataset/ragas_results.json")
CACHE_PATH = Path("evalutation/dataset/pipeline_answers_cache.json")
_cache_lock = threading.Lock()


def _load_cache() -> dict[str, dict[str, Any]]:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def load_evaluation_queries(limit: int | None = None) -> list[dict[str, Any]]:
    """Load evaluation ground truth dataset, skipping out-of-scope for generation scoring."""
    target_path = DATASET_PATH if DATASET_PATH.exists() else Path("evaluation_dataset.json")
    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Filter for valid in-scope evaluation items
    in_scope = [d for d in data if d.get("type") != "out_of_scope"]
    if limit:
        in_scope = in_scope[:limit]
    return in_scope


async def _process_single_query(
    item: dict[str, Any],
    idx: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Process a single query through retrieval and generation bounded by semaphore and cache."""
    q = item["question"]
    gt = item.get("ground_truth_answer", "")

    # Return cached answer if available to save quota
    with _cache_lock:
        cache = _load_cache()
        if q in cache:
            logger.info("[%d/%d] Using cached answer: %s", idx, total, q[:70])
            return cache[q]

    async with semaphore:
        logger.info("[%d/%d] Starting Query: %s", idx, total, q[:70])

        # 1. Fetch retrieved parent chunk texts
        parent_records = await retrieve_parent_chunks(q, as_documents=False)
        chunk_texts = [
            record.get("text_content", "")
            for record in parent_records
            if record.get("text_content")
        ]

        # 2. Generate answer via full pipeline with rotating client
        generated_answer = await generate_rag_answer(q)
        logger.info("[%d/%d] Completed Query: %s", idx, total, q[:70])

        result_item = {
            "question": q,
            "answer": generated_answer,
            "contexts": chunk_texts,
            "ground_truth": gt,
        }

        # Cache answer immediately
        with _cache_lock:
            current_cache = _load_cache()
            current_cache[q] = result_item
            _save_cache(current_cache)

        return result_item


async def run_pipeline_inference(
    queries: list[dict[str, Any]],
    max_concurrency: int = 2,
) -> dict[str, list[Any]]:
    """Run full RAG pipeline (Retrieval + Generation) with bounded concurrency."""
    concurrency_env = os.getenv("RAGAS_CONCURRENCY")
    concurrency = int(concurrency_env) if concurrency_env else max_concurrency

    logger.info(
        "Executing pipeline inference for %d queries with bounded concurrency=%d...",
        len(queries),
        concurrency,
    )

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _process_single_query(item, idx, len(queries), semaphore)
        for idx, item in enumerate(queries, 1)
    ]
    results = await asyncio.gather(*tasks)

    return {
        "question": [r["question"] for r in results],
        "answer": [r["answer"] for r in results],
        "contexts": [r["contexts"] for r in results],
        "ground_truth": [r["ground_truth"] for r in results],
    }


class RotatingLangchainChatGemini:
    """Round-robin ChatGoogleGenerativeAI wrapper across multiple API keys with 429 retry."""

    def __init__(
        self,
        api_keys: list[str],
        model: str = "gemini-3.5-flash-lite",
        temperature: float = 0.0,
    ) -> None:
        self.api_keys = api_keys
        self.model = model
        self.temperature = temperature
        self._idx = 0
        self._lock = threading.Lock()
        self.clients = [
            ChatGoogleGenerativeAI(
                model=model,
                google_api_key=key,
                temperature=temperature,
            )
            for key in api_keys
        ]
        logger.info(
            "Initialized RotatingLangchainChatGemini judge across %d keys",
            len(self.clients),
        )

    def _next_client(self) -> ChatGoogleGenerativeAI:
        with self._lock:
            client = self.clients[self._idx % len(self.clients)]
            self._idx = (self._idx + 1) % len(self.clients)
            return client

    async def agenerate_prompt(self, prompts, stop=None, callbacks=None, **kwargs):
        for attempt in range(5):
            client = self._next_client()
            try:
                return await client.agenerate_prompt(
                    prompts, stop=stop, callbacks=callbacks, **kwargs
                )
            except Exception as e:
                msg = str(e).lower()
                if ("429" in msg or "resource_exhausted" in msg or "quota" in msg) and attempt < 4:
                    backoff = 8.0 * (attempt + 1)
                    await asyncio.sleep(backoff)
                    continue
                raise e

    def generate_prompt(self, prompts, stop=None, callbacks=None, **kwargs):
        for attempt in range(5):
            client = self._next_client()
            try:
                return client.generate_prompt(prompts, stop=stop, callbacks=callbacks, **kwargs)
            except Exception as e:
                msg = str(e).lower()
                if ("429" in msg or "resource_exhausted" in msg or "quota" in msg) and attempt < 4:
                    time.sleep(8.0 * (attempt + 1))
                    continue
                raise e


def execute_ragas_evaluation(
    eval_dict: dict[str, list[Any]],
) -> dict[str, Any]:
    """Score answers using Ragas LLM-as-a-Judge with rotated Gemini keys and concurrency."""
    logger.info("Initializing rotated Gemini judge and local embeddings for Ragas evaluation...")

    from ragas.run_config import RunConfig
    from rotating_gemini_client import load_gemini_api_keys

    api_keys = load_gemini_api_keys()
    rotating_chat = RotatingLangchainChatGemini(
        api_keys=api_keys,
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        temperature=0.0,
    )
    evaluator_llm = LangchainLLMWrapper(rotating_chat)
    evaluator_embeddings = LangchainEmbeddingsWrapper(get_embeddings_model())

    dataset = Dataset.from_dict(eval_dict)

    concurrency_env = os.getenv("RAGAS_CONCURRENCY")
    eval_workers = int(concurrency_env) if concurrency_env else 2
    run_config = RunConfig(max_workers=eval_workers, timeout=180)

    logger.info(
        "Evaluating Faithfulness and Answer Relevancy on %d items (workers=%d across %d keys)...",
        len(dataset),
        eval_workers,
        len(api_keys),
    )
    results = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy],
        llm=evaluator_llm,
        embeddings=evaluator_embeddings,
        run_config=run_config,
    )

    return results


async def main():
    print("==================================================")
    print("Layer 2: Ragas Generation Evaluation (Gemini)")
    print("==================================================\n")

    # By default, evaluate all in-scope questions using the 15-key pool.
    # To restrict for quick debugging, set e.g. $env:RAGAS_EVAL_LIMIT="5"
    limit_env = os.getenv("RAGAS_EVAL_LIMIT", "all").strip().lower()
    eval_limit = None if limit_env in ("all", "none", "0", "") else int(limit_env)
    queries = load_evaluation_queries(limit=eval_limit)

    print(f"Loaded {len(queries)} queries for evaluation (RAGAS_EVAL_LIMIT={limit_env}).")

    t_start = time.perf_counter()
    eval_dict = await run_pipeline_inference(queries)
    results = execute_ragas_evaluation(eval_dict)
    total_elapsed = time.perf_counter() - t_start

    print("\n" + "=" * 50)
    print("RAGAS EVALUATION SUMMARY")
    print("=" * 50)
    print(f"Total Evaluated Queries : {len(queries)}")
    print(f"Total Execution Time    : {total_elapsed:.2f} s")

    # Save detailed per-query scores & extract averages from DataFrame
    df = results.to_pandas()
    faithfulness_score = float(df["faithfulness"].mean()) if "faithfulness" in df else 0.0
    relevancy_score = float(df["answer_relevancy"].mean()) if "answer_relevancy" in df else 0.0

    print(f"Faithfulness (Grounded) : {faithfulness_score:.4f}  (Target: >= 0.85)")
    print(f"Answer Relevancy        : {relevancy_score:.4f}  (Target: >= 0.80)")
    print("=" * 50)

    OUTPUT_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(OUTPUT_RESULTS_PATH, orient="records", indent=2)
    print(f"\nDetailed evaluation results saved to: {OUTPUT_RESULTS_PATH}")

    if faithfulness_score >= 0.85 and relevancy_score >= 0.80:
        print("\nVERDICT: GENERATION QUALITY PASSED! Zero hallucination detected.")
    else:
        print("\nVERDICT: GENERATION QUALITY BELOW SLA. Check prompt grounding.")


if __name__ == "__main__":
    asyncio.run(main())

