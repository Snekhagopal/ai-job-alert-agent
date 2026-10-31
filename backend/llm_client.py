"""Thin JSON-mode client for Gemini or OpenAI over plain REST (no vendor SDKs).

Every call is counted in the llm_usage table and capped by MAX_LLM_CALLS_PER_DAY.
"""
from __future__ import annotations

import json
import logging

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.config import get_settings
from backend.database import llm_calls_today, record_llm_call, session_scope

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when no provider is configured or the daily budget is exhausted."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=3, min=3, max=60), reraise=True)
def _call_gemini(system: str, prompt: str) -> str:
    s = get_settings()
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:generateContent",
        headers={"x-goog-api-key": s.gemini_api_key},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        },
        timeout=s.llm_timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected Gemini response: {str(data)[:300]}") from exc


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=3, min=3, max=60), reraise=True)
def _call_openai(system: str, prompt: str) -> str:
    s = get_settings()
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {s.openai_api_key}"},
        json={
            "model": s.openai_model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        },
        timeout=s.llm_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def is_configured() -> bool:
    s = get_settings()
    return (s.llm_provider == "gemini" and bool(s.gemini_api_key)) or (
        s.llm_provider == "openai" and bool(s.openai_api_key)
    )


def generate_json(system: str, prompt: str) -> dict:
    s = get_settings()
    if not is_configured():
        raise LLMUnavailable(f"LLM provider '{s.llm_provider}' is not configured")
    with session_scope() as db:
        if llm_calls_today(db) >= s.max_llm_calls_per_day:
            raise LLMUnavailable(f"daily LLM budget of {s.max_llm_calls_per_day} calls reached")
    call = _call_gemini if s.llm_provider == "gemini" else _call_openai
    ok = False
    try:
        text = call(system, prompt)
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        ok = True
        return result
    finally:
        with session_scope() as db:
            record_llm_call(db, ok)
