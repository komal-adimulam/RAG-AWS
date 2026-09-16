import asyncio
import logging
import os
import time
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from google import genai
from google.genai import types

try:
    from src.rag_aws.retrieval import format_timing_summary, retrieve_parent_chunks
    from src.rag_aws.rotating_gemini_client import (
        RotatingGeminiClient,
        get_rotating_client,
        is_rate_limit_error,
    )
except ImportError:
    from rag_aws.retrieval import format_timing_summary, retrieve_parent_chunks
    from rag_aws.rotating_gemini_client import (
        RotatingGeminiClient,
        get_rotating_client,
        is_rate_limit_error,
    )

logger = logging.getLogger(__name__)

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
NO_CONTEXT_MESSAGE = (
    "I cannot answer this based on the provided document. No relevant context was found."
)

SYSTEM_INSTRUCTION = """You are a helpful and precise assistant answering questions about AWS.

Strict Grounding Rules:
1. Answer the question ONLY using the provided context below. Do not use any outside or prior knowledge about AWS.
2. If the context does not contain enough information to answer the question, respond with exactly: "I cannot answer this based on the provided document."

Formatting & Citation Rules:
1. Write a clean, complete, and well-structured answer.
2. Do NOT include any inline citations, brackets, or source markers anywhere in the body of your answer. The entire answer text must be completely free of inline brackets, footnotes, or source tags (for example, do NOT write "[Source: ...]" or "[Page: ...]" in your sentences).
3. After the answer is fully written, append a separate "Sources:" section at the very end listing the distinct category/service and page pairs actually used, deduplicated (e.g., "- Amazon EC2, Page: 45"). Never repeat the same category/service and page combination twice."""

# Persistent client and chat session caches
_client: genai.Client | None = None
_chat_session: Any | None = None


def get_genai_client() -> genai.Client:
    """Get the current rotated Google GenAI Client."""
    return get_rotating_client().get_client()


def get_chat_session(
    model: str = DEFAULT_MODEL,
    system_instruction: str = SYSTEM_INSTRUCTION,
    force_new: bool = False,
) -> Any:
    """Get or initialize an AsyncChat session using the next rotated API key."""
    global _chat_session
    if _chat_session is None or force_new:
        _, _chat_session = get_rotating_client().create_chat_session(
            model=model, system_instruction=system_instruction
        )
    return _chat_session


def get_llm(model: str = DEFAULT_MODEL, temperature: float = 0.0) -> Any:
    """Backward-compatible helper returning an active AsyncChat session."""
    return get_chat_session(model=model)


def format_parent_chunks(parent_chunks: list[dict[str, Any]]) -> str:
    """Format retrieved parent chunks for the prompt context block."""
    formatted_blocks: list[str] = []

    for chunk in parent_chunks:
        source_label = chunk.get("service") or chunk.get("category") or "Document"
        if isinstance(source_label, str):
            clean_label = source_label.replace("**", "").strip()
        else:
            clean_label = str(source_label)

        page = chunk.get("page", "N/A")
        text_content = chunk.get("text_content", "").strip()

        block = f"[Source: {clean_label}, Page: {page}]\n{text_content}"
        formatted_blocks.append(block)

    return "\n\n---\n\n".join(formatted_blocks)


def build_source_footer(parent_chunks: list[dict[str, Any]]) -> str:
    """Build a deduplicated footer listing distinct sources and pages."""
    sources: list[str] = []
    seen: set[str] = set()

    for chunk in parent_chunks:
        source_label = chunk.get("service") or chunk.get("category") or "Document"
        if isinstance(source_label, str):
            clean_label = source_label.replace("**", "").strip()
        else:
            clean_label = str(source_label)

        page = chunk.get("page")
        page_str = f"Page: {page}" if page is not None else "Page: N/A"
        item = f"- {clean_label}, {page_str}"

        if item not in seen:
            seen.add(item)
            sources.append(item)

    if not sources:
        return ""

    footer_items = "\n".join(sources)
    return f"\n\nSources:\n{footer_items}"


async def generate_rag_answer_stream(
    query: str,
    chat_session: Any | None = None,
    llm: Any | None = None,
    timings: dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    """Retrieve parent chunks and stream an answer grounded strictly in retrieved context.

    Uses a persistent AsyncChat session with send_message_stream to eliminate
    Automatic Function Calling (AFC) warnings and overhead.

    1. Retrieves parent context chunks from PostgreSQL via retrieve_parent_chunks.
    2. If no chunks found, yields NO_CONTEXT_MESSAGE and exits immediately without LLM call.
    3. Formats context with [Source: {service or category}, Page: {page}] blocks.
    4. Streams answer via persistent AsyncChat.send_message_stream().
    5. Clean answer body contains zero inline brackets; distinct sources are listed at the end.
    """
    if timings is None:
        timings = {}

    t_total_start = time.perf_counter()

    # 1. Retrieve parent chunks
    parent_chunks = await retrieve_parent_chunks(query, timings=timings)

    # 2. Empty retrieval check
    if not parent_chunks:
        timings["context_build"] = 0.0
        timings["generation"] = 0.0
        timings["total"] = time.perf_counter() - t_total_start
        yield NO_CONTEXT_MESSAGE
        return

    # 3. Context formatting (Stage 8)
    t_ctx_start = time.perf_counter()
    context_text = format_parent_chunks(parent_chunks)

    # 4. Message prompt
    prompt = f"Context:\n{context_text}\n\nQuestion:\n{query}"
    timings["context_build"] = time.perf_counter() - t_ctx_start

    # 5. Rotated AsyncChat session
    rotating_client = get_rotating_client()
    key_idx: int | None = None

    if chat_session:
        session = chat_session
    elif llm and hasattr(llm, "send_message_stream"):
        session = llm
    else:
        key_idx, session = rotating_client.create_chat_session(
            model=DEFAULT_MODEL, system_instruction=SYSTEM_INSTRUCTION
        )

    # 6. Stream tokens via send_message_stream with retry on 429 rate limits and 503 spikes (Stage 9)
    t_gen_start = time.perf_counter()
    max_retries = 5

    for attempt in range(max_retries):
        try:
            stream = await session.send_message_stream(prompt)
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
            if key_idx is not None:
                rotating_client.record_success(key_idx)
            await rotating_client.asleep_delay()
            break
        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if is_rate_limit_error(e) and attempt < max_retries - 1 and key_idx is not None:
                backoff = 8.0 * (attempt + 1)
                logger.warning(
                    "Key index %d hit 429 rate limit (attempt %d/%d). Cooling down for %.1fs and rotating to next key...",
                    key_idx,
                    attempt + 1,
                    max_retries,
                    backoff,
                )
                await asyncio.sleep(backoff)
                key_idx, session = rotating_client.create_chat_session(
                    model=DEFAULT_MODEL, system_instruction=SYSTEM_INSTRUCTION
                )
                continue
            raise e
    timings["generation"] = time.perf_counter() - t_gen_start
    timings["total"] = time.perf_counter() - t_total_start
    logger.info("%s", format_timing_summary(timings))


async def generate_rag_answer(
    query: str,
    chat_session: Any | None = None,
    llm: Any | None = None,
    timings: dict[str, Any] | None = None,
) -> str:
    """Non-streaming convenience wrapper around generate_rag_answer_stream."""
    accumulated: list[str] = []
    async for token in generate_rag_answer_stream(
        query, chat_session=chat_session, llm=llm, timings=timings
    ):
        accumulated.append(token)
    return "".join(accumulated)


if __name__ == "__main__":
    async def _test():
        test_query = "What is Amazon EC2?"
        print(f"Testing generation stream for query: '{test_query}'\n")
        try:
            test_timings = {}
            async for chunk in generate_rag_answer_stream(test_query, timings=test_timings):
                print(chunk, end="", flush=True)
            print()
            print(format_timing_summary(test_timings))
        except ValueError as e:
            print(f"Generation test note: {e}")

    asyncio.run(_test())
