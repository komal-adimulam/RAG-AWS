"""Re-export rotating_gemini_client from project root for intra-package imports."""

import sys
from pathlib import Path

# Ensure project root is in sys.path
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from rotating_gemini_client import (
    RotatingGeminiClient,
    get_rotating_client,
    is_rate_limit_error,
    load_gemini_api_keys,
)

__all__ = [
    "RotatingGeminiClient",
    "get_rotating_client",
    "is_rate_limit_error",
    "load_gemini_api_keys",
]

