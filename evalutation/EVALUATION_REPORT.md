# Evaluation Report: AWS Hierarchical RAG Pipeline

---

## 1. Executive Summary

This report documents the rigorous two-layer evaluation conducted on the **AWS Hierarchical RAG Pipeline**. The system was evaluated on a curated 50-query benchmark across PostgreSQL hybrid retrieval (`pgvector` dense + BM25 sparse + Reciprocal Rank Fusion) and Google Gemini generation with Ragas LLM-as-a-judge scoring.

All primary targets were exceeded:
* **Retrieval Recall@TopK**: **1.0000** (Target: $\ge 0.85$)
* **Retrieval MRR**: **0.9479** (Target: $\ge 0.80$)
* **Retrieval NDCG@TopK**: **0.9616** (Target: $\ge 0.80$)
* **Generation Faithfulness (Zero-Hallucination)**: **0.9984** (Target: $\ge 0.85$)
* **Generation Answer Relevancy**: **0.9078** (Target: $\ge 0.80$)

---

## 2. LLM Call Volume & API Consumption

* **Per-Question Calls**: Each evaluation question consumes **~6 LLM calls** (1 call for RAG pipeline answer generation + ~5 internal Ragas judge calls: 2 for Faithfulness claim extraction/verification and 3 for Answer Relevancy synthetic question generation).
* **Total Calls Consumed**: Across all 48 benchmark questions, **~288 total LLM calls** were executed and balanced across the 15 rotating Gemini API keys.

---

## 3. Two-Layer Evaluation Architecture

To isolate retrieval quality from generation fidelity, the evaluation was structured into two decoupled layers:

```
[50 Ground Truth Benchmark Queries]
         │
         ├──► Layer 1: Retrieval Evaluation (Deterministic & Algorithmic)
         │       ├─► Evaluates: pgvector dense + BM25 sparse + Reciprocal Rank Fusion (RRF)
         │       └─► Metrics: Recall@TopK, MRR, NDCG@TopK
         │
         └──► Layer 2: Generation Evaluation (Ragas LLM-as-a-Judge)
                 ├─► Evaluates: End-to-end answer synthesis grounded in parent chunks
                 └─► Metrics: Faithfulness (hallucination check) & Answer Relevancy
```

### Layer 1: Retrieval Evaluation
* **Script**: `evalutation/test_retrieval_metrics.py`
* **Mechanism**: Evaluates the hybrid retrieval engine directly against ground-truth parent IDs without calling any LLM. Measures ranking quality, candidate coverage, and position penalties.

### Layer 2: Generation Evaluation
* **Script**: `evalutation/test_ragas_eval.py`
* **Mechanism**: Runs the full RAG pipeline (retrieval + prompt assembly + answer generation). Feeds the resulting (Question, Context, Answer) triplets into Ragas using Google Gemini (`gemini-3.5-flash-lite`) as the analytical judge and local HuggingFace embeddings for question alignment.

---

## 4. Benchmark Results & Metric Definitions

| Layer | Metric | Score Achieved | Target SLA | Status |
| :--- | :--- | :---: | :---: | :---: |
| **Layer 1: Retrieval** | **Recall@TopK** | **1.0000** | $\ge 0.85$ | **PASSED** |
| **Layer 1: Retrieval** | **MRR (Mean Reciprocal Rank)** | **0.9479** | $\ge 0.80$ | **PASSED** |
| **Layer 1: Retrieval** | **NDCG@TopK** | **0.9616** | $\ge 0.80$ | **PASSED** |
| **Layer 2: Generation** | **Faithfulness** | **0.9984** | $\ge 0.85$ | **PASSED** |
| **Layer 2: Generation** | **Answer Relevancy** | **0.9078** | $\ge 0.80$ | **PASSED** |

### Metric Definitions

1. **Recall@TopK**: The percentage of test queries where the required ground-truth parent chunk appeared anywhere in the top-$K$ retrieved set. A score of `1.0000` indicates that the correct context was never missed.
2. **Mean Reciprocal Rank (MRR)**: Evaluates ranking position: $\text{MRR} = \frac{1}{|Q|} \sum \frac{1}{\text{rank}_i}$. A score of `0.9479` indicates the relevant parent chunk was ranked #1 in almost every test query.
3. **NDCG@TopK (Normalized Discounted Cumulative Gain)**: Measures graded relevance with logarithmic position discounting: $\text{DCG} = \sum \frac{\text{rel}_i}{\log_2(i + 1)}$. Rewards ranking relevant chunks near the top and penalizes buried documents.
4. **Faithfulness**: Measures grounding against retrieved context to detect hallucinations:
   $$\text{Faithfulness} = \frac{\text{Number of claims inferred from context}}{\text{Total claims in generated answer}}$$
   A score of `0.9984` represents near-perfect factual adherence with virtually zero hallucination.
5. **Answer Relevancy**: Evaluates how directly and completely the generated answer addresses the original query by embedding synthetically generated questions and measuring cosine similarity against the user question.

---

## 5. Ground Truth Dataset Construction

* **Generator Script**: `evalutation/generate_ground_truth_gemini.py`
* **Source Corpus**: Extracted from PostgreSQL `parent_chunks` covering 24 core architectural topics (Compute, Storage, Databases, Networking, Security, Well-Architected Framework).
* **Dataset Composition** (`evalutation/dataset/evaluation_dataset.json`):
  * **48 In-Scope Factual Queries**: Paired with `target_parent_id`, `ground_truth_answer`, and source metadata.
  * **2 Out-of-Scope Negative Controls**: Verifies negative rejection behavior (`"I cannot answer this based on the provided document."`).

---

## 6. Engineering Challenges & Error Resolutions

During development and evaluation, four critical errors were diagnosed and resolved:

### 1. The Ragas OpenAI Dependency Trap
* **Problem**: Ragas defaults to OpenAI models (`ChatOpenAI`, `OpenAIEmbeddings`) and crashes with `openai.OpenAIError: Missing credentials` if no OpenAI key is configured.
* **Resolution**: Explicitly passed local HuggingFace embeddings wrapped in `LangchainEmbeddingsWrapper(get_embeddings_model())` and initialized Gemini via `LangchainLLMWrapper(rotating_chat)`.

### 2. LangChain / VertexAI Import Conflict in Ragas 0.4.3
* **Problem**: `ragas.llms.base` imported `from langchain_community.chat_models.vertexai import ChatVertexAI`, which raised `ModuleNotFoundError` with modern `langchain-community` 0.4.x.
* **Resolution**: Injected a 4-line `sys.modules` monkey-patch prior to importing Ragas to stub the deprecated module paths cleanly without modifying vendor packages.

### 3. Modern Ragas `EvaluationResult` Attribute Error
* **Problem**: `results.get("faithfulness")` raised `AttributeError: 'EvaluationResult' object has no attribute 'get'` because modern Ragas returns an `EvaluationResult` dataclass.
* **Resolution**: Converted the evaluation result to a pandas DataFrame (`df = results.to_pandas()`) and computed mean metric scores via `df["faithfulness"].mean()`.

### 4. Google Gemini Free-Tier Rate Limits (`429 RESOURCE_EXHAUSTED`)
* **Problem**: Firing concurrent requests triggered Google Gemini's 15 Requests-Per-Minute (RPM) project quota.
* **Resolution**:
  * Expanded the key pool to **15 active Gemini API keys** in `.env`.
  * Implemented multi-key rotation with exponential cooldown (`8s, 16s, 24s...`) in `src/rag_aws/generation.py` and `RotatingLangchainChatGemini`.
  * Regulated execution concurrency to `2` concurrent workers to pace requests under the 15 RPM ceiling.
  * Implemented disk checkpoint caching (`evalutation/dataset/pipeline_answers_cache.json`), ensuring previously generated answers are never lost or re-billed on subsequent runs.

---

## 7. Artifacts & Generated Data

* **Retrieval Test Suite**: `evalutation/test_retrieval_metrics.py`
* **Ragas Evaluation Suite**: `evalutation/test_ragas_eval.py`
* **Ground Truth Dataset**: `evalutation/dataset/evaluation_dataset.json`
* **Inference Cache**: `evalutation/dataset/pipeline_answers_cache.json`
* **Detailed Per-Query Scores**: `evalutation/dataset/ragas_results.json`

