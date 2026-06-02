"""Fireworks AI inference client."""

import os
import logging
from typing import Optional, Dict, Any, Tuple, Union

import httpx

logger = logging.getLogger(__name__)

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/glm-5"

FIREWORKS_MODELS = {
    "glm-5": "accounts/fireworks/models/glm-5",
    "glm-5p1": "accounts/fireworks/models/glm-5p1",
    "gpt-oss-120b": "accounts/fireworks/models/gpt-oss-120b",
    "kimi-k2p5": "accounts/fireworks/models/kimi-k2p5",
    "kimi-k2": "accounts/fireworks/models/kimi-k2-instruct",
}


def _resolve_model(model: str) -> str:
    """Resolve model alias to full Fireworks model path, or return as-is."""
    return FIREWORKS_MODELS.get(model, model)


class FireworksClient:
    """Async client for Fireworks AI inference API."""

    def __init__(self) -> None:
        self.api_key = os.getenv("FIREWORKS_API_KEY")
        if not self.api_key:
            logger.warning("FIREWORKS_API_KEY not found in environment")

    @property
    def is_available(self) -> bool:
        """Check if API key is configured."""
        return bool(self.api_key)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        reasoning_effort: Optional[str] = None,
    ) -> Optional[Union[Tuple[str, Dict[str, Any]], Tuple[str, str, Dict[str, Any]]]]:
        """
        Generate completion via Fireworks AI.

        Returns:
            (content, usage) or (content, reasoning_content, usage) if reasoning is present.
        """
        if not self.api_key:
            raise RuntimeError("FIREWORKS_API_KEY is not configured.")

        resolved_model = _resolve_model(model or DEFAULT_MODEL)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        payload = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{FIREWORKS_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            
            if response.status_code != 200:
                logger.error(f"Fireworks API error ({response.status_code}): {response.text}")
                return None
                
            data = response.json()

        message = data["choices"][0]["message"]
        content = message.get("content", "")
        reasoning_content = message.get("reasoning_content")

        usage_data = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_data.get("prompt_tokens", 0),
            "completion_tokens": usage_data.get("completion_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
            "model": resolved_model,
        }

        if reasoning_content:
            return content, reasoning_content, usage
        return content, usage


_client = None


def get_client() -> FireworksClient:
    """Get or create singleton Fireworks client."""
    global _client
    if _client is None:
        _client = FireworksClient()
    return _client
