"""generate_ground_truth_gemini.py

Generates a production-grade 50-question evaluation dataset using
RotatingGeminiClient across 4 rotating API keys (6 calls per key).
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import psycopg2
from dotenv import load_dotenv

from rotating_gemini_client import RotatingGeminiClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PARAMS = {
    "dbname": os.getenv("POSTGRES_DB", "rag_hierarchical"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
}

DATASET_PATH = Path("evalutation/dataset/evaluation_dataset.json")
ROOT_DATASET_PATH = Path("evaluation_dataset.json")

# 24 Representative core services and foundational topics
TARGET_SPECS = [
    # Foundations
    {"type": "page", "page": 13, "label": "Cloud Computing Definition"},
    {"type": "page", "page": 14, "label": "Six Advantages of Cloud Computing", "filter": "Stop spending money"},
    {"type": "service", "service": "Hybrid", "label": "Hybrid Cloud Deployment Model"},
    {"type": "page", "page": 16, "label": "AWS Security Principles"},
    {"type": "page", "page": 18, "label": "AWS Cloud Compliance & Shared Responsibility"},
    {"type": "page", "page": 153, "label": "AWS Well-Architected Framework"},
    # Compute & Containers
    {"type": "service", "service": "Amazon EC2", "label": "Amazon EC2"},
    {"type": "service", "service": "AWS Lambda", "label": "AWS Lambda"},
    {"type": "service", "service": "Amazon Elastic Container Service", "label": "Amazon ECS"},
    {"type": "service", "service": "Amazon Elastic Kubernetes Service", "label": "Amazon EKS"},
    # Storage
    {"type": "service", "service": "Amazon Simple Storage Service", "label": "Amazon S3"},
    {"type": "service", "service": "Amazon Elastic Block Store", "label": "Amazon EBS"},
    {"type": "service", "service": "Amazon Elastic File System", "label": "Amazon EFS"},
    # Databases
    {"type": "service", "service": "Amazon Relational Database Service", "label": "Amazon RDS"},
    {"type": "service", "service": "Amazon Aurora", "label": "Amazon Aurora"},
    {"type": "service", "service": "Amazon DynamoDB", "label": "Amazon DynamoDB"},
    {"type": "service", "service": "Amazon ElastiCache", "label": "Amazon ElastiCache"},
    # Networking
    {"type": "service", "service": "Amazon VPC", "label": "Amazon VPC"},
    {"type": "service", "service": "Amazon CloudFront", "label": "Amazon CloudFront"},
    {"type": "service", "service": "Elastic Load Balancing", "label": "Elastic Load Balancing"},
    # Security & Identity
    {"type": "service", "service": "AWS Identity and Access Management", "label": "AWS IAM"},
    {"type": "service", "service": "AWS Key Management Service", "label": "AWS KMS"},
    # Analytics & Integration
    {"type": "service", "service": "Amazon Athena", "label": "Amazon Athena"},
    {"type": "service", "service": "AWS Glue", "label": "AWS Glue"},
]


def fetch_target_chunks() -> list[dict[str, Any]]:
    """Fetch the selected target chunks from PostgreSQL."""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    selected_chunks = []
    seen_ids = set()

    for spec in TARGET_SPECS:
        row = None
        if spec["type"] == "service":
            cur.execute(
                "SELECT id, service, category, page, text_content FROM parent_chunks WHERE service ILIKE %s LIMIT 1;",
                (f"%{spec['service']}%",),
            )
            row = cur.fetchone()
        elif spec["type"] == "page":
            filter_text = spec.get("filter")
            if filter_text:
                cur.execute(
                    "SELECT id, service, category, page, text_content FROM parent_chunks WHERE page = %s AND text_content ILIKE %s LIMIT 1;",
                    (spec["page"], f"%{filter_text}%"),
                )
            else:
                cur.execute(
                    "SELECT id, service, category, page, text_content FROM parent_chunks WHERE page = %s AND text_content NOT ILIKE '%%whitepaper updated%%' LIMIT 1;",
                    (spec["page"],),
                )
            row = cur.fetchone()

        if row and str(row[0]) not in seen_ids:
            seen_ids.add(str(row[0]))
            selected_chunks.append({
                "id": str(row[0]),
                "service": row[1] or spec["label"],
                "category": row[2] or "Overview",
                "page": row[3],
                "text": row[4],
                "label": spec["label"],
            })

    conn.close()
    return selected_chunks


def parse_llm_json(raw_text: str) -> list[dict[str, Any]]:
    """Extract and parse JSON array from LLM response."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^```\s*$", "", cleaned, flags=re.MULTILINE).strip()
    match = re.search(r"\[\s*\{.*\}\s*\]", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def generate_benchmark_dataset():
    logger.info("=== Starting Ground Truth Generation via RotatingGeminiClient ===")

    chunks = fetch_target_chunks()
    logger.info("Fetched %d core chunks covering foundations, compute, storage, db, network, security, analytics.", len(chunks))

    client = RotatingGeminiClient(delay_seconds=1.5)
    dataset: list[dict[str, Any]] = []

    for i, chunk in enumerate(chunks, 1):
        clean_service = re.sub(r"[*_#`]", "", chunk["service"]).strip()
        clean_category = re.sub(r"[*_#`]", "", chunk["category"]).strip()
        text_sample = chunk["text"][:1500]

        prompt = f"""You are creating a rigorous evaluation dataset for an AWS RAG system.
Given the following AWS documentation text chunk:

TOPIC / SERVICE: {clean_service} ({clean_category})
PAGE: {chunk['page']}
TEXT:
{text_sample}

Task: Generate exactly TWO high-quality question-answer pairs based STRICTLY on this text:
1. One 'factoid' question: A direct factual question about a definition, core purpose, or specific feature.
2. One 'complex' question: A technical question asking how a mechanism works, comparing capabilities, or describing multi-concept integration.

Rules:
- The question must be natural, clear, and representative of what an AWS cloud engineer or architect would ask.
- The ground_truth_answer must be complete, precise, and STRICTLY grounded in the provided text only (no hallucinated outside knowledge).
- Set "expected_parent_ids": ["{chunk['id']}"]
- Set "expected_page_numbers": [{chunk['page']}]

Return ONLY a valid JSON array matching this exact schema:
[
  {{
    "type": "factoid",
    "question": "...",
    "ground_truth_answer": "...",
    "expected_parent_ids": ["{chunk['id']}"],
    "expected_page_numbers": [{chunk['page']}]
  }},
  {{
    "type": "complex",
    "question": "...",
    "ground_truth_answer": "...",
    "expected_parent_ids": ["{chunk['id']}"],
    "expected_page_numbers": [{chunk['page']}]
  }}
]"""

        logger.info("[%d/%d] Generating Q&A for '%s' (Page %d)...", i, len(chunks), clean_service, chunk["page"])

        try:
            raw_response = client.generate(prompt)
            qa_pairs = parse_llm_json(raw_response)
            if isinstance(qa_pairs, list) and len(qa_pairs) == 2:
                dataset.extend(qa_pairs)
                logger.info("Successfully added 2 questions for %s.", clean_service)
            else:
                logger.warning("Unexpected format for chunk %s: %s", chunk["id"], raw_response[:100])
        except Exception as err:
            logger.error("Failed to generate Q&A for chunk %s: %s", chunk["id"], err)

    # Add Negative / Out of Scope queries to test hallucination resistance
    dataset.append({
        "type": "out_of_scope",
        "question": "How do I configure virtual network peering on Microsoft Azure Virtual Machines?",
        "ground_truth_answer": "I cannot answer this based on the provided document.",
        "expected_parent_ids": [],
        "expected_page_numbers": [],
    })
    dataset.append({
        "type": "out_of_scope",
        "question": "What is the capital city of France?",
        "ground_truth_answer": "I cannot answer this based on the provided document.",
        "expected_parent_ids": [],
        "expected_page_numbers": [],
    })

    logger.info("=== Generation Complete! Total questions: %d ===", len(dataset))
    logger.info("Final Key Usage Distribution across 4 keys: %s", client.get_usage_stats())

    # Save to both dataset path and root path
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=True)
    logger.info("Saved to %s", DATASET_PATH)

    with open(ROOT_DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=True)
    logger.info("Saved to %s", ROOT_DATASET_PATH)


if __name__ == "__main__":
    generate_benchmark_dataset()
