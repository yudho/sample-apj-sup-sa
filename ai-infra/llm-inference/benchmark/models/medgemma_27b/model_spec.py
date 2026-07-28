"""MedGemma-27B ModelSpec.

This is the single place where MedGemma-specific model facts live. Adding a
new model = create a sibling folder with its own ``model_spec.py``,
``experiments.py``, and ``prompts.py``.
"""
from __future__ import annotations

from vllm_ec2_bench import ModelSpec


MEDGEMMA_27B = ModelSpec(
    resource_prefix="medgemma-27b",
    display_name="MedGemma 27B",
    hf_model_id="google/medgemma-27b-text-it",
    served_model_name="medgemma-27b",
    # ~27B parameters × 2 bytes (BF16) ≈ 54 GiB. We use 55 to leave a small
    # margin for the embeddings / LM head.
    weight_size_gib=55.0,
    default_max_model_len=16384,
    gated=True,
    dtype="bfloat16",
    # A/B-measured 2026-07-27 (p6-b200, DP=8, c=800): v0.26.0 gives +4.7%
    # total tok/min over v0.25.1 with identical flags. cu129 build runs clean
    # on the p6 DLAMI driver. Do NOT add --max-num-batched-tokens 16384 or
    # --async-scheduling: measured regression + silent request loss on this
    # short-prompt shape (see p6-ab-vllm-results.json).
    vllm_gpu_image="vllm/vllm-openai:v0.26.0-cu129-ubuntu2404",
)


__all__ = ["MEDGEMMA_27B"]
