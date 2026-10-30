"""Task-specific Gemini model registry for the assistant graph."""

import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

ModelTask = Literal["fast", "reasoning"]
DEFAULT_FAST_MODEL = "gemini-3.5-flash-lite"
DEFAULT_REASONING_MODEL = "gemini-3.5-flash-lite"


@lru_cache(maxsize=2)
def get_model(task: ModelTask = "fast") -> ChatGoogleGenerativeAI:
    """Create one bounded-timeout model client per configured task."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing from the environment.")

    model_name = os.getenv(
        "GEMINI_REASONING_MODEL" if task == "reasoning" else "GEMINI_FAST_MODEL",
        DEFAULT_REASONING_MODEL if task == "reasoning" else DEFAULT_FAST_MODEL,
    )
    timeout = float(os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "25"))
    if timeout <= 0:
        raise ValueError("GEMINI_REQUEST_TIMEOUT_SECONDS must be greater than zero.")
    return ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
        temperature=0,
        request_timeout=timeout,
        retries=1,
    )
