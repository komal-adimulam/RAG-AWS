import asyncio
import time
from typing import Any, AsyncGenerator

from dotenv import load_dotenv

try:
    from src.rag_aws.generation import (
        generate_rag_answer,
        generate_rag_answer_stream,
    )
    from src.rag_aws.retrieval import format_timing_summary, retrieve_parent_chunks
except ImportError:
    from rag_aws.generation import (
        generate_rag_answer,
        generate_rag_answer_stream,
    )
    from rag_aws.retrieval import format_timing_summary, retrieve_parent_chunks

load_dotenv()


async def stream_rag_query(
    query: str, timings: dict[str, Any] | None = None
) -> AsyncGenerator[str, None]:
    """Execute the full RAG pipeline and yield response chunks asynchronously."""
    async for chunk in generate_rag_answer_stream(query, timings=timings):
        yield chunk


async def answer_rag_query(
    query: str, timings: dict[str, Any] | None = None
) -> str:
    """Execute the full RAG pipeline and return the complete answer string."""
    return await generate_rag_answer(query, timings=timings)


async def async_cli() -> None:
    """Interactive CLI query loop with real-time streaming."""
    print("\n" + "=" * 55)
    print("  Hierarchical RAG Pipeline Ready (PostgreSQL + Gemini)")
    print("  Type 'quit', 'exit', or 'q' to exit.")
    print("=" * 55)

    while True:
        try:
            user_query = await asyncio.to_thread(input, "\nQuestion: ")
            user_query = user_query.strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        if not user_query:
            continue

        if user_query.lower() in ("quit", "exit", "q"):
            print("Exiting pipeline. Goodbye!")
            break

        print("\n--- Answer ---")
        start_time = time.perf_counter()
        timings: dict[str, Any] = {}

        try:
            async for chunk in stream_rag_query(user_query, timings=timings):
                print(chunk, end="", flush=True)
        except Exception as err:
            print(f"\n[Error executing query]: {err}")

        elapsed = time.perf_counter() - start_time
        timings["total"] = elapsed
        print(f"\n\n{format_timing_summary(timings)}")
        print(f"[Elapsed time: {elapsed:.2f}s]")


def run_cli() -> None:
    """Synchronous entrypoint for running the interactive CLI."""
    try:
        asyncio.run(async_cli())
    except (KeyboardInterrupt, SystemExit):
        print("\nPipeline exited.")


if __name__ == "__main__":
    run_cli()