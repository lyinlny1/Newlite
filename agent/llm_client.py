from __future__ import annotations

from typing import Any

import aiohttp

from config import Settings


class LlmRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = aiohttp.ClientTimeout(total=settings.llm_timeout_seconds)

    def _provider_config(self, provider: str) -> dict[str, str] | None:
        if provider == "openrouter":
            if not self.settings.openrouter_api_key or not self.settings.openrouter_model:
                return None
            return {
                "base_url": self.settings.openrouter_base_url,
                "api_key": self.settings.openrouter_api_key,
                "model": self.settings.openrouter_model,
            }
        if provider == "ollama_local":
            if not self.settings.ollama_local_model:
                return None
            return {
                "base_url": self.settings.ollama_local_base_url,
                "api_key": "ollama",
                "model": self.settings.ollama_local_model,
            }
        if provider == "ollama_cloud":
            if not self.settings.ollama_cloud_api_key or not self.settings.ollama_cloud_model:
                return None
            return {
                "base_url": self.settings.ollama_cloud_base_url,
                "api_key": self.settings.ollama_cloud_api_key,
                "model": self.settings.ollama_cloud_model,
            }
        return None

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> dict[str, Any]:
        errors: list[str] = []
        for provider in self.settings.llm_provider_order:
            if provider == "none":
                break
            cfg = self._provider_config(provider)
            if not cfg:
                errors.append(f"{provider}: not configured")
                continue
            headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
            if provider == "openrouter":
                headers["X-Title"] = self.settings.app_name
                if self.settings.openrouter_http_referer:
                    headers["HTTP-Referer"] = self.settings.openrouter_http_referer
            payload = {"model": cfg["model"], "messages": messages, "temperature": temperature, "stream": False}
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(f"{cfg['base_url']}/chat/completions", headers=headers, json=payload) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            errors.append(f"{provider}: HTTP {resp.status} {text[:180]}")
                            continue
                        data = await resp.json()
            except (TimeoutError, aiohttp.ClientError) as exc:
                errors.append(f"{provider}: {exc}")
                continue
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                errors.append(f"{provider}: invalid response shape")
                continue
            return {"ok": True, "provider": provider, "content": content, "errors": errors}
        return {"ok": False, "provider": "none", "content": "", "errors": errors}
