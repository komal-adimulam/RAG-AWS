"""rotating_gemini_client.py

Standalone round-robin Gemini API client wrapper for hierarchical RAG workloads.
Distributes requests across multiple Gemini API keys to stay within free-tier rate
limits (20 requests/day per key).
"""

import asyncio
import logging
import os
import re
import threading
import time
import warnings
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ponytail: suppress verbose AFC advisory emitted by google-genai when tools are not used
warnings.filterwarnings("ignore", message=".*Direct use of automatic function calling.*")
try:
    from google.genai.models import AsyncModels, Models
    Models._logged_afc_warning = True
    AsyncModels._logged_afc_warning = True
except Exception:
    pass

logger = logging.getLogger("rotating_gemini_client")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _handler.setFormatter(_formatter)
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def load_gemini_api_keys() -> list[str]:
    """Load numbered Gemini API keys from environment variables.

    Discovers GEMINI_API_KEY_1, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ... (any number found).
    Falls back to GEMINI_API_KEY or GOOGLE_API_KEY if no numbered keys are configured.
    """
    load_dotenv()

    numbered_keys: list[tuple[int, str]] = []
    for var_name, var_value in os.environ.items():
        match = re.match(r"^GEMINI_API_KEY_(\d+)$", var_name)
        if match and var_value.strip():
            numbered_keys.append((int(match.group(1)), var_value.strip()))

    if numbered_keys:
        numbered_keys.sort(key=lambda item: item[0])
        return [k for _, k in numbered_keys]

    single_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if single_key and single_key.strip():
        return [single_key.strip()]

    return []


def is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is an HTTP 429 / RESOURCE_EXHAUSTED rate limit error."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "resource_exhausted" in msg or "quota" in msg


class RotatingGeminiClient:
    """Round-robin wrapper around google.genai.Client to manage multiple API keys."""

    def __init__(
        self,
        api_keys: list[str] | None = None,
        default_model: str | None = None,
        delay_seconds: float = 1.5,
    ) -> None:
        self.api_keys = api_keys if api_keys is not None else load_gemini_api_keys()
        if not self.api_keys:
            raise ValueError(
                "No Gemini API keys found. Set GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... "
                "or GEMINI_API_KEY / GOOGLE_API_KEY in your environment/.env file."
            )

        self.default_model = (
            default_model
            or os.getenv("GEMINI_MODEL")
            or "gemini-3.5-flash-lite"
        )
        self.delay_seconds = delay_seconds

        # Rotation state
        self._current_index = 0
        self._lock = threading.Lock()
        self._usage_counts: dict[int, int] = {i: 0 for i in range(len(self.api_keys))}

        # Pre-initialize clients for all keys
        self._clients = [genai.Client(api_key=k) for k in self.api_keys]

        logger.info(
            "RotatingGeminiClient initialized with %d keys (model: %s, delay: %.1fs)",
            len(self.api_keys),
            self.default_model,
            self.delay_seconds,
        )

    def _get_next_key_index(self) -> int:
        """Advance rotation index in round-robin order and return the selected index."""
        with self._lock:
            selected_idx = self._current_index
            self._current_index = (self._current_index + 1) % len(self.api_keys)
            return selected_idx

    def _format_distribution(self) -> str:
        with self._lock:
            return ", ".join(
                f"Key #{i + 1}: {self._usage_counts[i]}"
                for i in range(len(self.api_keys))
            )

    def _record_success(self, key_idx: int) -> None:
        """Record successful call for key index and log usage distribution."""
        with self._lock:
            self._usage_counts[key_idx] += 1
        logger.info(
            "Completed call using key index %d (Key #%d of %d). Distribution: [%s]",
            key_idx,
            key_idx + 1,
            len(self.api_keys),
            self._format_distribution(),
        )

    def generate(
        self,
        prompt: str,
        model: str | None = None,
        config: Any | None = None,
        **kwargs: Any,
    ) -> str:
        """Generate text using the next API key in round-robin order.

        On 429 rate limit response: catches it, advances to the next key,
        and retries once before raising.
        """
        response = self.generate_content(
            contents=prompt, model=model, config=config, **kwargs
        )
        return response.text or ""

    def generate_content(
        self,
        contents: Any,
        model: str | None = None,
        config: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call models.generate_content using round-robin rotation with 429 retry."""
        selected_model = model or self.default_model
        key_idx = self._get_next_key_index()

        logger.info(
            "Dispatching generate request to key index %d (Key #%d of %d)...",
            key_idx,
            key_idx + 1,
            len(self.api_keys),
        )

        try:
            client = self._clients[key_idx]
            response = client.models.generate_content(
                model=selected_model, contents=contents, config=config, **kwargs
            )
            self._record_success(key_idx)
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            return response
        except Exception as exc:
            if not is_rate_limit_error(exc):
                raise exc

            logger.warning(
                "Key index %d (Key #%d of %d) hit 429 rate limit: %s. Advancing to next key and retrying once.",
                key_idx,
                key_idx + 1,
                len(self.api_keys),
                exc,
            )

            # Advance to next key for retry
            retry_key_idx = self._get_next_key_index()
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

            logger.info(
                "Retrying request with key index %d (Key #%d of %d)...",
                retry_key_idx,
                retry_key_idx + 1,
                len(self.api_keys),
            )

            # Retry once - if it raises, allow exception to propagate
            retry_client = self._clients[retry_key_idx]
            response = retry_client.models.generate_content(
                model=selected_model, contents=contents, config=config, **kwargs
            )
            self._record_success(retry_key_idx)
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            return response

    async def agenerate(
        self,
        prompt: str,
        model: str | None = None,
        config: Any | None = None,
        **kwargs: Any,
    ) -> str:
        """Async variant of generate() using round-robin rotation with 429 retry."""
        response = await self.agenerate_content(
            contents=prompt, model=model, config=config, **kwargs
        )
        return response.text or ""

    async def agenerate_content(
        self,
        contents: Any,
        model: str | None = None,
        config: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async variant of generate_content() using round-robin rotation with 429 retry."""
        selected_model = model or self.default_model
        key_idx = self._get_next_key_index()

        logger.info(
            "Dispatching async generate request to key index %d (Key #%d of %d)...",
            key_idx,
            key_idx + 1,
            len(self.api_keys),
        )

        try:
            client = self._clients[key_idx]
            response = await client.aio.models.generate_content(
                model=selected_model, contents=contents, config=config, **kwargs
            )
            self._record_success(key_idx)
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            return response
        except Exception as exc:
            if not is_rate_limit_error(exc):
                raise exc

            logger.warning(
                "Key index %d (Key #%d of %d) hit 429 rate limit: %s. Advancing to next key and retrying once.",
                key_idx,
                key_idx + 1,
                len(self.api_keys),
                exc,
            )

            retry_key_idx = self._get_next_key_index()
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

            logger.info(
                "Retrying async request with key index %d (Key #%d of %d)...",
                retry_key_idx,
                retry_key_idx + 1,
                len(self.api_keys),
            )

            retry_client = self._clients[retry_key_idx]
            response = await retry_client.aio.models.generate_content(
                model=selected_model, contents=contents, config=config, **kwargs
            )
            self._record_success(retry_key_idx)
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            return response

    @property
    def current_index(self) -> int:
        """Return the current rotation index."""
        with self._lock:
            return self._current_index

    def get_client(self, index: int | None = None) -> genai.Client:
        """Return the underlying genai.Client for a given index, or current rotation index."""
        with self._lock:
            idx = index if index is not None else self._current_index
            return self._clients[idx % len(self.api_keys)]

    def get_usage_stats(self) -> dict[int, int]:
        """Return a copy of the usage count distribution per key index."""
        with self._lock:
            return dict(self._usage_counts)

    def create_chat_session(
        self,
        model: str | None = None,
        system_instruction: str | None = None,
        config: Any | None = None,
        key_idx: int | None = None,
    ) -> tuple[int, Any]:
        """Create an AsyncChat session on the next rotated key (or specified key_idx)."""
        if key_idx is None:
            key_idx = self._get_next_key_index()
        client = self._clients[key_idx]
        if config is None and system_instruction is not None:
            config = types.GenerateContentConfig(system_instruction=system_instruction)
        session = client.aio.chats.create(model=model or self.default_model, config=config)
        return key_idx, session

    def record_success(self, key_idx: int) -> None:
        """Record successful call for key index and log usage distribution."""
        self._record_success(key_idx)

    def sleep_delay(self) -> None:
        """Apply anti-burst delay synchronously."""
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    async def asleep_delay(self) -> None:
        """Apply anti-burst delay asynchronously."""
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

    def __len__(self) -> int:
        return len(self.api_keys)


_global_rotating_client: RotatingGeminiClient | None = None
_global_client_lock = threading.Lock()


def get_rotating_client() -> RotatingGeminiClient:
    """Get or initialize the shared singleton RotatingGeminiClient instance."""
    global _global_rotating_client
    if _global_rotating_client is None:
        with _global_client_lock:
            if _global_rotating_client is None:
                _global_rotating_client = RotatingGeminiClient()
    return _global_rotating_client


# ponytail: minimal runnable check for round-robin rotation, 429 retry, and real API dispatch
if __name__ == "__main__":
    print("=== Testing RotatingGeminiClient ===")

    # 1. Test key loading
    keys = load_gemini_api_keys()
    print(f"Loaded {len(keys)} keys from environment.")
    assert len(keys) >= 1, "Expected at least 1 key in environment"

    # 2. Test round-robin rotation index logic
    dummy_keys = ["k1", "k2", "k3"]
    test_client = RotatingGeminiClient(api_keys=dummy_keys, delay_seconds=0)
    assert len(test_client) == 3
    indices = [test_client._get_next_key_index() for _ in range(7)]
    assert indices == [0, 1, 2, 0, 1, 2, 0], f"Rotation mismatch: {indices}"
    print("[OK] Round-robin rotation index verified: 0 -> 1 -> 2 -> 0 -> 1 -> 2 -> 0")

    # 3. Test 429 retry logic simulation
    class Fake429(Exception):
        code = 429

    class FakeClientSuccess:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                class Resp:
                    text = "success after retry"
                return Resp()

    class FakeClient429:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                raise Fake429("Rate limit reached")

    retry_client = RotatingGeminiClient(api_keys=["k1", "k2"], delay_seconds=0)
    retry_client._clients = [FakeClient429(), FakeClientSuccess()]
    res = retry_client.generate("test prompt")
    assert res == "success after retry"
    stats = retry_client.get_usage_stats()
    assert stats[1] == 1 and stats[0] == 0, f"Expected retry on key 1: {stats}"
    print("[OK] 429 retry logic verified: automatically advanced to next key and succeeded.")

    # 4. Live test against Gemini API across all configured keys
    print("\n=== Running Live Multi-Key Round-Robin Generation ===")
    live_client = RotatingGeminiClient(delay_seconds=1.0)
    for i in range(len(live_client.api_keys)):
        answer = live_client.generate(f"Respond in 3 words for test #{i + 1}.")
        print(f"Call {i + 1} Result: {answer.strip()}")

    print("\nFinal Usage Distribution:")
    for k_idx, count in sorted(live_client.get_usage_stats().items()):
        print(f"  Key #{k_idx + 1} (index {k_idx}): {count} calls")

    print("\n[OK] All RotatingGeminiClient tests passed successfully!")
