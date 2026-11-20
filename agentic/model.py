"""Model provider abstraction; offline workflows require no paid API keys."""
import httpx

from agentic.config import Settings


class ModelGateway:
    def __init__(self, config: Settings):
        self.config = config

    async def complete(self, system: str, message: str) -> str | None:
        if self.config.provider == "offline":
            return None
        async with httpx.AsyncClient(timeout=90) as client:
            if self.config.provider == "openai":
                if not self.config.openai_key:
                    raise ValueError("OPENAI_API_KEY is required")
                response = await client.post(
                    self.config.openai_base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.config.openai_key},
                    json={
                        "model": self.config.openai_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": message},
                        ],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            if not self.config.anthropic_key:
                raise ValueError("ANTHROPIC_API_KEY is required")
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.config.anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.config.anthropic_model,
                    "system": system,
                    "messages": [{"role": "user", "content": message}],
                    "max_tokens": 2000,
                },
            )
            response.raise_for_status()
            return "".join(
                block["text"] for block in response.json()["content"]
                if block.get("type") == "text"
            )
