# rag-aws — Hierarchical RAG Pipeline for AWS Documentation

**rag-aws** is a production-grade Retrieval-Augmented Generation (RAG) system that answers questions about Amazon Web Services using only grounded context from an AWS whitepaper PDF. It uses a **two-tier hierarchical chunking** strategy (parent/child), **hybrid retrieval** (pgvector dense + BM25 sparse + Reciprocal Rank Fusion), and **Google Gemini** generation with multi-key API rotation to stay within free-tier rate limits.

---

## Features

| Feature | Description |
|---|---|
| **PDF Ingestion** | Extracts markdown from AWS whitepapers, strips headers/footers/copyright/page numbers, injects per-page markers |
| **Hierarchical Chunking** | Two-tier parent/child chunking (~2,000-char parents capped, ~400-char children) with page tracking and header-derived category/service metadata |
| **Embedding & Storage** | BAAI/bge-large-en-v1.5 (1024-dim) embeddings stored in PostgreSQL with pgvector; idempotent re-ingestion |
| **Hybrid Retrieval** | Concurrent dense vector search (`<=>`) + BM25 keyword search, fused with weighted Reciprocal Rank Fusion (0.7/0.3) |
| **Grounded Generation** | Streaming answers via `gemini-3.5-flash-lite` with strict grounding rules, citation-free body, and a deduplicated `Sources:` footer |
| **Multi-Key Rotation** | Round-robin Gemini API client with automatic 429 retry and key failover |
| **Interactive CLI** | Async CLI query loop with per-query timing breakdown |
| **Two-Layer Evaluation** | Layer 1: Retrieval benchmark (MRR, Recall, NDCG); Layer 2: Ragas generation evaluation (Faithfulness, Answer Relevancy) |

---

## Project Structure

```
rag-aws/
├── README.md
├── pyproject.toml
├── .python-version
├── .env                        # gitignored — create your own
├── .gitignore
├── rotating_gemini_client.py   # Multi-key round-robin Gemini client (project root)
├── data/
│   ├── raw/                    # Source PDFs (e.g., aws-overview(1).pdf)
│   └── processed/              # Extracted markdown (e.g., aws-overview.md)
├── evalutation/                # Evaluation suite (note: typo preserved for compatibility)
│   ├── dataset/
│   │   ├── evaluation_dataset.json     # 50-question ground truth benchmark
│   │   ├── ragas_results.json          # Per-query Ragas scores
│   │   └── pipeline_answers_cache.json # Disk checkpoint cache
│   ├── test_retrieval_metrics.py       # Layer 1 retrieval benchmark
│   ├── test_ragas_eval.py              # Layer 2 Ragas generation eval
│   ├── generate_ground_truth_gemini.py # Dataset generator
│   └── EVALUATION_REPORT.md            # Full evaluation results
├── src/
│   └── rag_aws/
│       ├── __init__.py             # Package entrypoint (main -> pipeline CLI)
│       ├── ingestion.py            # PDF extraction & cleaning
│       ├── chunking.py             # Hierarchical parent/child chunking
│       ├── embed_store.py          # Embed + store in PostgreSQL/pgvector
│       ├── retrieval.py            # Hybrid dense + BM25 + RRF retrieval
│       ├── generation.py           # Streaming grounded answer generation
│       ├── pipeline.py             # Interactive RAG CLI
│       └── rotating_gemini_client.py # Package re-export of root client
└── uv.lock
```

---

## Evaluation Results

All SLA targets were met on a 50-query benchmark (48 in-scope + 2 out-of-scope negative controls):

| Layer | Metric | Score | Target | Status |
|---|---|---|---|---|
| Layer 1: Retrieval | Recall@TopK | **1.0000** | ≥ 0.85 | PASSED |
| Layer 1: Retrieval | MRR | **0.9479** | ≥ 0.80 | PASSED |
| Layer 1: Retrieval | NDCG@TopK | **0.9616** | ≥ 0.80 | PASSED |
| Layer 2: Generation | Faithfulness | **0.9984** | ≥ 0.85 | PASSED |
| Layer 2: Generation | Answer Relevancy | **0.9078** | ≥ 0.80 | PASSED |

See `evalutation/EVALUATION_REPORT.md` for the full report.

---

## Requirements

- Python **3.11+**
- **PostgreSQL** with the `pgvector` extension installed and running
- **Google Gemini API key(s)** — see [Configuration](#configuration)
- uv (recommended) or pip

Core dependencies (from `pyproject.toml`):

```
datasets, faiss-cpu, langchain, langchain-community, langchain-google-genai,
langchain-huggingface, langchain-pymupdf4llm, langchain-text-splitters,
llama-index-core, llama-index-readers-file, pandas, pgvector, psycopg2-binary,
python-dotenv, ragas, rank-bm25
```

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd "RAG PROJECT"
```

### 2. Create and activate a virtual environment

Using **uv** (recommended):

```bash
uv sync
source .venv/bin/activate   # Linux/macOS
# or
.venv\Scripts\activate      # Windows PowerShell
```

Or with pip:

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux/macOS
pip install -e .
```

### 3. Install PostgreSQL + pgvector

Install PostgreSQL and enable the `pgvector` extension:

```sql
CREATE DATABASE rag_hierarchical;
\c rag_hierarchical
CREATE EXTENSION IF NOT EXISTS vector;
```

### 4. Configure environment

Copy the env template and fill in your values:

```bash
cp .env.example .env   # if you create one; otherwise create .env manually
```

The `.env` file is gitignored and should **never** be committed. Required variables:

| Variable | Description | Default |
|---|---|---|
| `POSTGRES_DB` | Database name | `rag_hierarchical` |
| `POSTGRES_USER` | PostgreSQL user | `postgres` |
| `POSTGRES_PASSWORD` | PostgreSQL password | (required) |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `GEMINI_API_KEY_1`…`GEMINI_API_KEY_N` | Gemini API keys (numbered) | (at least one required) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Fallback single key | (optional) |
| `GEMINI_MODEL` | Gemini model name | `gemini-3.5-flash-lite` |

Example `.env`:

```ini
POSTGRES_DB=rag_hierarchical
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

GEMINI_API_KEY_1=your_key_here
GEMINI_API_KEY_2=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
```

> **Tip:** The rotating client auto-discovers all `GEMINI_API_KEY_N` variables. Use multiple keys to distribute load across Gemini's free-tier rate limits.

### 5. Place your source PDF

Drop your AWS whitepaper PDF into `data/raw/`. Supported candidate filenames:

- `aws-overview(1).pdf`
- `aws-overview (1).pdf`
- `aws-overview.pdf`

---

## Usage

### Step 1 — Ingest & Chunk the PDF

Extract text from the PDF and save cleaned markdown:

```bash
python -m src.rag_aws.ingestion
```

Output: `data/processed/aws-overview.md`

### Step 2 — Chunk & Store in PostgreSQL

Generate hierarchical chunks, embed them with BGE-large, and store in PostgreSQL:

```bash
python -m src.rag_aws.embed_store
```

This truncates and re-populates `parent_chunks` and `child_chunks` tables idempotently.

### Step 3 — Query the RAG Pipeline

Run the interactive CLI:

```bash
python -m src.rag_aws.pipeline
```

Or using the installed console script:

```bash
rag-aws
```

Example session:

```
  Hierarchical RAG Pipeline Ready (PostgreSQL + Gemini)
  Type 'quit', 'exit', or 'q' to exit.
=======================================================

Question: What is Amazon EC2?

--- Answer ---
Amazon Elastic Compute Cloud (Amazon EC2) is a web service that provides
secure, resizable compute capacity in the cloud...

Sources:
- Amazon EC2, Page: 45

[Timing] embed=0.15s | dense_db=0.03s | sparse_bm25=0.02s | ...
[Elapsed time: 2.41s]

Question: quit
Exiting pipeline. Goodbye!
```

### Programmatic usage

```python
import asyncio
from src.rag_aws.pipeline import answer_rag_query

async def main():
    answer = await answer_rag_query("What is Amazon S3?")
    print(answer)

asyncio.run(main())
```

For streaming responses:

```python
from src.rag_aws.pipeline import stream_rag_query

async def main():
    async for chunk in stream_rag_query("What is AWS Lambda?"):
        print(chunk, end="", flush=True)

asyncio.run(main())
```

---

## Running Evaluations

### Layer 1 — Retrieval Benchmark

Tests hybrid retrieval (dense + BM25 + RRF) against ground-truth parent IDs without calling any LLM:

```bash
python evalutation/test_retrieval_metrics.py
```

Metrics: MRR, Recall@TopK, NDCG@TopK, plus per-query latency.

### Layer 2 — Ragas Generation Evaluation

Runs the full RAG pipeline end-to-end and scores answers with Ragas (Faithfulness + Answer Relevancy) using Gemini as LLM-as-a-judge:

```bash
python evalutation/test_ragas_eval.py
```

Environment knobs:

| Variable | Description | Default |
|---|---|---|
| `RAGAS_EVAL_LIMIT` | Number of queries to evaluate (`all` or integer) | `all` |
| `RAGAS_CONCURRENCY` | Max concurrent pipeline workers | `2` |

Results are saved to `evalutation/dataset/ragas_results.json`.

### Regenerate the Ground Truth Dataset

Create a new 50-question benchmark from indexed parent chunks:

```bash
python evalutation/generate_ground_truth_gemini.py
```

Outputs `evalutation/dataset/evaluation_dataset.json` (and a copy at repo root).

---

## Architecture Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   AWS PDF       │────►│  Ingestion       │────►│  Processed MD   │
│  (data/raw/)    │     │  (ingestion.py)  │     │  (data/processed│
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                      │
                                                      ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   CLI / Query   │◄────│  Generation      │◄────│  Retrieval      │
│  (pipeline.py)  │     │  (generation.py) │     │  (retrieval.py) │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                      │
                                                      ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  PostgreSQL     │◄────│  Embed & Store   │◄────│  Chunking       │
│  + pgvector     │     │  (embed_store.py)│     │  (chunking.py)  │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                      ▲
                                                      │
                                    ┌─────────────────┘
                                    ▼
                              RotatingGeminiClient
                              (multi-key Gemini rotation)
```

**Retrieval flow**: Query → concurrent dense (pgvector `<=>`) + sparse (BM25) search → weighted Reciprocal Rank Fusion → top-8 child chunks → deduplicated parent IDs → fetch parent chunks from `parent_chunks` table.

**Generation flow**: Retrieved parent chunks → formatted context block (`[Source: {service}, Page: {page}]`) → Gemini streaming with strict grounding → answer body (no inline citations) + `Sources:` footer.

---

## License

MIT — see LICENSE file if present.

---

## Author

komal-adimulam <komal.adimulam@fissionlabs.com>
