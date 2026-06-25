"""Tera AI inference client."""

import os
import logging
from typing import Optional, Dict, Any, Tuple, Union

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TERA_BASE_URL = "http://127.0.0.1:9090/v1"
DEFAULT_MODEL_NAME = "cloudcode/chat-gemini-3-flash-paid-tier"


class TeraClient:
    """Async client for Tera AI inference API."""

    @property
    def is_available(self) -> bool:
        """Check if client is available."""
        return True

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
        Generate completion via Tera AI.

        Returns:
            (content, usage) or (content, reasoning_content, usage) if reasoning is present.
        """
        base_url = os.getenv("TERA_BASE_URL", DEFAULT_TERA_BASE_URL).rstrip("/")
        model_name = model or os.getenv("TERA_MODEL", DEFAULT_MODEL_NAME)
        api_key = os.getenv("TERA_API_KEY")

        payload = {
            "model": model_name,
            "instructions": system_prompt,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": user_prompt
                }
            ],
            "stream": False,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }

        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}

        headers = {
            "Content-Type": "application/json",
            "client-origin": "cli-tools",
        }
        # tera requires an API key on /v1/* when TERA_API_KEYS is configured server-side.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Check if Ubuntu system ca-certificates bundle exists to trust proxy/MITM certs
        verify_path = "/etc/ssl/certs/ca-certificates.crt"
        verify = verify_path if os.path.exists(verify_path) else True

        async with httpx.AsyncClient(verify=verify, timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/responses",
                json=payload,
                headers=headers,
            )
            
            if response.status_code != 200:
                logger.error(f"Tera API error ({response.status_code}): {response.text}")
                return None
                
            data = response.json()

        output_items = data.get("output", [])
        content_parts = []
        for item in output_items:
            if item.get("type") == "message" and item.get("role") == "assistant":
                content_val = item.get("content", [])
                if isinstance(content_val, str):
                    content_parts.append(content_val)
                elif isinstance(content_val, list):
                    for part in content_val:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            content_parts.append(part.get("text", ""))

        content = "".join(content_parts)

        usage_data = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_data.get("input_tokens", 0),
            "completion_tokens": usage_data.get("output_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
            "model": model_name,
        }

        return content, usage


_client = None


def get_client() -> TeraClient:
    """Get or create singleton Tera client."""
    global _client
    if _client is None:
        _client = TeraClient()
    return _client
