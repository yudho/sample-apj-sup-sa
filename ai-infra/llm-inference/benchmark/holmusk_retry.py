"""Holmusk cross-GPU MedGemma-27B benchmark, c=100 — PLAIN notebook process.

No AMI baking, no custom AMI: uses the framework's normal DLAMI + fresh HF
download at boot, exactly like the notebook. Experiments (exp_3/4/8/9) already
carry: spot->on-demand->odcr fallback + 60-min generous readiness timeout.

Runs all 4 GPUs sequentially (g6e, g7, g7e, p6), tearing down each before the
next. Results persisted DURABLY to OneDrive/Holmusk (never /tmp), written the
instant each GPU finishes. Detached-safe.

This lives inside benchmark/ so `models.*` imports resolve (CWD-based).
"""
from __future__ import annotations
import json, logging, sys, time, traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
LOG = logging.getLogger("holmusk_run")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

REGION = "us-west-2"   # moved from us-east-2 (capacity outage + slow I/O on 2026-07-22)
HF_SECRET = "medgemma-27b-benchmark/hf-token"
DATA_DIR = Path("/Users/diponego/Projects/llm-deployment/models-deploy-and-benchmark/data/samples/medical-notes")
RESULTS_DIR = Path("/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/Holmusk/cross-gpu-benchmark-2026-07-21")
RESULTS = RESULTS_DIR / "results.json"
N_SAMPLES = 500
MAX_NEW_TOKENS = 256
CONCURRENCY = 100

ORDER = [("p6", "exp_8"), ("g7", "exp_9")]


def load_inputs(n):
    import random
    texts = []
    for shard in sorted(DATA_DIR.glob("*.jsonl")):
        with shard.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line).get("text")
                except json.JSONDecodeError:
                    continue
                if t:
                    texts.append(t)
        if len(texts) >= n * 5:
            break
    return random.Random(42).sample(texts, min(n, len(texts)))


def save_result(row):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    data = []
    if RESULTS.exists():
        try:
            data = json.loads(RESULTS.read_text())
        except json.JSONDecodeError:
            data = []
    data = [r for r in data if r.get("key") != row.get("key")]
    data.append(row)
    RESULTS.write_text(json.dumps(data, indent=2, default=str))
    LOG.info("[%s] result persisted -> %s", row.get("key"), RESULTS)


def run_one(key, exp_id, inputs, catalog):
    cfg = EXPERIMENTS[exp_id]
    d = cfg.deployment
    hw = catalog.hardware(d.instance_type)
    LOG.info("=== [%s] %s %dx%s TP=%d DP=%d cap=%s ready_to=%ds ===", key, d.instance_type,
             hw.num_accelerators, hw.accelerator_model, d.tensor_parallel, d.data_parallel,
             d.capacity_preference, d.vllm_ready_timeout_s)
    runner = DeploymentRunner(cfg, catalog=catalog, hf_secret_name=HF_SECRET)
    state = None
    try:
        state = runner.launch()
        LOG.info("[%s] LAUNCHED %s mode=%s cap_wait=%.0fs ready=%.0fs", key, state.instance_id,
                 state.capacity_mode, state.capacity_wait_s or 0, state.vllm_ready_wait_s or 0)
        ep = VLLMEndpoint(base_url=state.base_url, api_key=state.api_key, model_id=cfg.model_spec.served_model_name)
        sm = ep.invoke(VLLMEndpoint.create_payload(SYSTEM_PROMPT, inputs[0], max_tokens=MAX_NEW_TOKENS))
        LOG.info("[%s] SMOKE in=%s out=%s lat=%.2fs", key, sm.num_tokens_input, sm.num_tokens_output, sm.time_to_last_token)
        from llmeter.experiments import LoadTest
        import asyncio
        payloads = [VLLMEndpoint.create_payload(SYSTEM_PROMPT, x, max_tokens=MAX_NEW_TOKENS) for x in inputs]
        lt = LoadTest(endpoint=ep, payload=payloads, sequence_of_clients=[CONCURRENCY],
                      output_path=str(RESULTS_DIR / f"llmeter_{key}"),
                      min_requests_per_run=len(payloads), min_requests_per_client=2)
        t0 = time.time()
        results = asyncio.run(lt.run())
        bench_wall = time.time() - t0
        rd = getattr(results, "results", None) or {}
        stats = None
        for clients, r in rd.items():
            if int(clients) == CONCURRENCY:
                stats = getattr(r, "stats", None)
        od = catalog.price_od(d.instance_type, REGION)
        save_result({
            "key": key, "instance": d.instance_type, "gpus": hw.num_accelerators,
            "gpu_model": hw.accelerator_model, "vram_gib_per_gpu": hw.vram_gib_per_accelerator,
            "tp": d.tensor_parallel, "dp": d.data_parallel, "replicas": cfg.model_replicas,
            "capacity_mode": state.capacity_mode, "od_usd_hr": od,
            "est_spot_usd_hr": catalog.estimated_spot(d.instance_type, REGION),
            "image": cfg.model_spec.vllm_gpu_image,
            "capacity_wait_s": state.capacity_wait_s, "vllm_ready_wait_s": state.vllm_ready_wait_s,
            "benchmark_wall_s": round(bench_wall, 1), "concurrency": CONCURRENCY,
            "n_requests": len(payloads), "stats": stats,
        })
        LOG.info("[%s] DONE ready=%.0fs bench=%.0fs", key, state.vllm_ready_wait_s or 0, bench_wall)
    except Exception:
        LOG.error("[%s] FAILED:\n%s", key, traceback.format_exc())
        save_result({"key": key, "instance": d.instance_type, "error": traceback.format_exc()[-800:]})
    finally:
        LOG.info("[%s] teardown...", key)
        try:
            runner.terminate(); LOG.info("[%s] torn down", key)
        except Exception:
            LOG.error("[%s] teardown error:\n%s", key, traceback.format_exc())


def main():
    LOG.info("Holmusk PLAIN run (no AMI). results -> %s", RESULTS)
    catalog = load_catalog(auto_refresh=True, offline_ok=False, max_age_hours_prices=24, regions=[REGION])
    inputs = load_inputs(N_SAMPLES)
    LOG.info("loaded %d medical notes", len(inputs))
    for key, exp_id in ORDER:
        try:
            run_one(key, exp_id, inputs, catalog)
        except Exception:
            LOG.error("[%s] orchestrator error:\n%s", key, traceback.format_exc())
    LOG.info("ALL DONE.")


if __name__ == "__main__":
    main()
