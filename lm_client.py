"""
agent/lm_client.py

LLM backend wrapper. Auto-detects mode from environment.

Mode        When                         How to activate
---------   --------------------------   -----------------------------------
anthropic   ANTHROPIC_API_KEY is set     export ANTHROPIC_API_KEY=sk-ant-...
mock        nothing is set               default fallback (no API key needed)
"""

import asyncio
import json
import os
from typing import Optional


class LMClient:

    def __init__(self, mode: str = None, max_tokens: int = 4096):
        if mode is None:
            mode = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock"
        self.mode       = mode
        self.max_tokens = max_tokens

    async def call(self, prompt: str, system: str = None) -> str:
        if self.mode == "anthropic":
            return await self._anthropic(prompt, system)
        return self._mock()

    # ── Anthropic ─────────────────────────────────────────────────────

    async def _anthropic(self, prompt: str, system: Optional[str]) -> str:
        try:
            import anthropic
        except ImportError:
            return "[ERROR] pip install anthropic"

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        kwargs = dict(
            model="claude-sonnet-4-20250514",
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system
        resp = await client.messages.create(**kwargs)
        return resp.content[0].text

    # ── Mock ──────────────────────────────────────────────────────────

    def _mock(self) -> str:
        return (
            "### Executive Summary\n"
            "[mock] Set ANTHROPIC_API_KEY to enable real LLM analysis.\n\n"
            "### Recommended Actions\n"
            "1. Set ANTHROPIC_API_KEY and re-run without --dry-run.\n"
        )
