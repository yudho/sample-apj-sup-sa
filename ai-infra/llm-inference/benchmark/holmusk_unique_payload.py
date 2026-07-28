"""UniquePayloadEndpoint — give every LLMeter request a DISTINCT prompt.

WHY THIS EXISTS (measurement bug found 2026-07-28)
---------------------------------------------------
LLMeter 0.1.12's per-client payload shuffle calls ``random.seed(0)`` before
``random.sample`` (runner.py ``_invoke_n_no_wait``). The seed is a *constant*,
so EVERY client derives the IDENTICAL permutation of the payload pool and then
walks it from the start via ``itertools.cycle``. With ``min_requests_per_client
= K``, all C clients send the SAME first K payloads.

Measured consequence on last night's banked c=800 tier: 40,000 responses came
from only **49 distinct prompts** — each prompt was served ~816 times. With
``--enable-prefix-caching`` on, repeats are near-free full-prompt cache hits,
which is exactly why /metrics reported a 96% prefix-cache hit rate.

That inflation is tolerable-but-noted for the short-note A/B (all arms shared
the same bias, so the *relative* +4.7% still holds). It is FATAL for the
real-shape run: Holmusk's sheet says ~1% of their prompt is cacheable, so a
96%-cache-hit measurement would overstate p6 throughput and hand the customer
a number their production pipeline can never reproduce.

THE FIX
-------
``prepare_payload`` is called by LLMeter's ``llmeter_invoke`` decorator BEFORE
the response timer starts (see llmeter/endpoints/base.py), so swapping the
prompt there costs nothing in the measured latency. This endpoint pops the
next unused note from a thread-safe iterator and substitutes it into the
user message, guaranteeing every request across every client is unique.

Requires: pool size >= total requests for the tier (c * K). Assert it.
"""
from __future__ import annotations

import threading
from typing import Any

from vllm_ec2_bench.endpoint import VLLMEndpoint


class PayloadPoolExhausted(RuntimeError):
    """Raised when more requests were issued than unique notes supplied."""


class UniquePayloadEndpoint(VLLMEndpoint):
    """vLLM endpoint that serves each request a unique note from a pool.

    Parameters
    ----------
    notes
        The pool of note texts. Consumed at most once each.
    system_prompt
        Prepended as the system message on every request (this is the ONLY
        shared prefix, matching Holmusk's "~1% cacheable" reality).
    max_tokens, temperature, top_p
        Sampling params applied to every generated payload.
    """

    def __init__(
        self,
        *args: Any,
        notes: list[str],
        system_prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Bypass any dataclass/pydantic __setattr__ machinery in the parent.
        object.__setattr__(self, "_notes", list(notes))
        object.__setattr__(self, "_system_prompt", system_prompt)
        object.__setattr__(self, "_max_tokens", max_tokens)
        object.__setattr__(self, "_temperature", temperature)
        object.__setattr__(self, "_top_p", top_p)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_iter", iter(range(len(notes))))
        object.__setattr__(self, "_served", 0)

    # -- pool plumbing -----------------------------------------------------
    def reset_pool(self, offset: int = 0) -> None:
        """Restart consumption at ``offset`` (call between concurrency tiers)."""
        with self._lock:
            object.__setattr__(self, "_iter", iter(range(offset, len(self._notes))))
            object.__setattr__(self, "_served", 0)

    @property
    def served(self) -> int:
        return self._served

    def remaining(self) -> int:
        return len(self._notes) - self._served

    def _next_note(self) -> str:
        """Thread-safe pop. LLMeter drives clients via asyncio.to_thread."""
        with self._lock:
            try:
                idx = next(self._iter)
            except StopIteration:
                raise PayloadPoolExhausted(
                    f"payload pool of {len(self._notes)} notes exhausted after "
                    f"{self._served} requests — supply a larger pool than c*K"
                ) from None
            object.__setattr__(self, "_served", self._served + 1)
        return self._notes[idx]

    # -- the hook ----------------------------------------------------------
    def prepare_payload(self, payload: dict) -> dict:
        """Substitute a fresh note. Runs OUTSIDE the measured response timer."""
        prepared = super().prepare_payload(payload)
        return {
            **prepared,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": self._next_note()},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "top_p": self._top_p,
        }


__all__ = ["UniquePayloadEndpoint", "PayloadPoolExhausted"]
