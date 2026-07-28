"""p6 VERIFIED c-sweep on the winning config (v0.26.0-cu129 + original flags).

Purpose (audit remediation, 2026-07-27):
  1. Measure the winner config at c=800,1200,1600 — the sweep the buggy arm-D
     never took (it swept the loser config). Also c=600 for the curve's left edge.
  2. VERIFY every tier: per-tier response count must be >= 98% of c*50, else the
     tier is marked INVALID (this catches LLMeter's silent whole-client drops,
     which report failed_requests=0).
  3. Cross-check tok/min three ways (llmeter stats time, our wall clock,
     raw responses.jsonl recompute) — divergence > 5% flags the tier.
  4. Scrape vLLM /metrics per tier: prefix-cache hit rate + preemptions, so
     cache accounting is no longer a blind spot.

Config comes entirely from the repo (model_spec now pins v0.26.0-cu129;
exp_8 = DP=8, max-model-len 4096, max-num-seqs 512): NO container swaps.

Run from benchmark/ detached:
  AWS_PROFILE=yudho-aiml nohup ../.venv/bin/python holmusk_p6_verified_sweep.py \
      >> "$RESDIR/p6-verified-sweep.log" 2>&1 < /dev/null & disown
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
LOG = logging.getLogger("p6_sweep")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

import boto3

REGION = "us-west-2"
HF_SECRET = "medgemma-27b-benchmark/hf-token"
DATA_DIR = Path("/Users/diponego/Projects/llm-deployment/models-deploy-and-benchmark/data/samples/medical-notes")
# LESSON (2026-07-27 run): the OneDrive CloudStorage mount stalled mid-run
# (Errno 60 write timeout) and killed the sweep after tier 1. ALL run I/O now
# goes to a LOCAL dir (home, not /tmp — /tmp wipes overnight); OneDrive is a
# best-effort mirror only, never on the critical path.
LOCAL_BASE = Path.home() / "holmusk-bench-local"
ONEDRIVE_BASE = Path("/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/"
                     "Holmusk/cross-gpu-benchmark-2026-07-21")
OUT = LOCAL_BASE / "p6-verified-sweep-results.json"
MAX_NEW_TOKENS = 512
REQS_PER_CLIENT = 50
TIERS = [800, 1200, 1600]      # c=600 already VALID from the 22:05 run (3,699,903)
COMPLETENESS_MIN = 0.98        # tier invalid below this response completeness


def load_inputs(n: int) -> list[str]:
    import random
    texts: list[str] = []
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
        if len(texts) >= n * 3:
            break
    return random.Random(42).sample(texts, min(n, len(texts)))


def save(rows: list) -> None:
    """Write locally (critical path); mirror to OneDrive best-effort."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    LOG.info("progress persisted -> %s", OUT)
    try:  # best-effort mirror; a stalled cloud mount must never kill the run
        mirror = ONEDRIVE_BASE / OUT.name
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(json.dumps(rows, indent=2, default=str))
    except OSError as exc:
        LOG.warning("OneDrive mirror failed (non-fatal): %s", exc)


def my_egress_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


def ensure_sg_access(ec2, sg_id: str) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8001,
                            "IpRanges": [{"CidrIp": f"{my_egress_ip()}/32",
                                          "Description": "sweep-runner self-heal"}]}])
    except Exception as exc:  # noqa: BLE001
        if "InvalidPermission.Duplicate" not in str(exc):
            LOG.warning("SG self-heal: %s", exc)


def scrape_metrics(base_url: str, api_key: str) -> dict:
    """Pull prefix-cache + preemption counters from vLLM /metrics."""
    out: dict = {}
    try:
        url = base_url.replace("/v1", "") + "/metrics"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for key in ("prefix_cache_queries_total", "prefix_cache_hits_total",
                        "num_preemptions_total", "prompt_tokens_total",
                        "generation_tokens_total"):
                if key in line:
                    try:
                        val = float(line.rsplit(" ", 1)[1])
                        out[key] = out.get(key, 0.0) + val   # sum across DP engines
                    except (ValueError, IndexError):
                        pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    if out.get("prefix_cache_queries_total"):
        out["prefix_cache_hit_rate"] = round(
            out.get("prefix_cache_hits_total", 0.0) / out["prefix_cache_queries_total"], 4)
    return out


def count_responses(outdir: Path) -> tuple[int, float, float]:
    """(n_responses, sum_input_tokens, sum_output_tokens) from responses.jsonl."""
    n = ti = to = 0
    for f in outdir.rglob("responses.jsonl"):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n += 1
                ti += r.get("num_tokens_input") or 0
                to += r.get("num_tokens_output") or 0
    return n, ti, to


def bench_verified(endpoint, payloads, c: int, base_url: str, api_key: str) -> dict:
    from llmeter.experiments import LoadTest
    import asyncio
    outdir = LOCAL_BASE / "p6_sweep_llmeter" / f"c{c}"
    m0 = scrape_metrics(base_url, api_key)
    lt = LoadTest(endpoint=endpoint, payload=payloads, sequence_of_clients=[c],
                  output_path=str(outdir),
                  min_requests_per_run=1, min_requests_per_client=REQS_PER_CLIENT)
    t0 = time.time()
    res = asyncio.run(lt.run())
    wall = time.time() - t0
    m1 = scrape_metrics(base_url, api_key)

    st = {}
    for cl, r in (getattr(res, "results", None) or {}).items():
        if int(cl) == c:
            st = getattr(r, "stats", None) or {}
    in_tpm = st.get("average_input_tokens_per_minute") or 0
    out_tpm = st.get("average_output_tokens_per_minute") or 0

    # --- verification layer -------------------------------------------------
    expected = c * REQS_PER_CLIENT
    n_disk, ti_disk, to_disk = count_responses(outdir)
    completeness = n_disk / expected if expected else 0
    # 3-way tok/min: llmeter stats | our wall clock | disk recompute over wall
    tokmin_stats = in_tpm + out_tpm
    tokmin_wall = (ti_disk + to_disk) / wall * 60 if wall else 0
    divergence = abs(tokmin_stats - tokmin_wall) / tokmin_stats if tokmin_stats else 1
    valid = completeness >= COMPLETENESS_MIN and divergence <= 0.05

    delta = {k: round((m1.get(k, 0) or 0) - (m0.get(k, 0) or 0), 1)
             for k in ("prefix_cache_queries_total", "prefix_cache_hits_total",
                       "num_preemptions_total")}
    hit_rate = (delta["prefix_cache_hits_total"] / delta["prefix_cache_queries_total"]
                if delta.get("prefix_cache_queries_total") else None)

    row = {"c": c, "valid": valid,
           "completeness": round(completeness, 4),
           "responses": n_disk, "expected": expected,
           "total_tok_min_stats": round(tokmin_stats, 1),
           "total_tok_min_wallclock": round(tokmin_wall, 1),
           "divergence": round(divergence, 4),
           "out_tok_min": round(out_tpm, 1),
           "req_per_min": st.get("requests_per_minute"),
           "ttlt_p50": st.get("time_to_last_token-p50"),
           "ttlt_p90": st.get("time_to_last_token-p90"),
           "failed_llmeter": st.get("failed_requests"),
           "wall_s": round(wall, 1),
           "prefix_cache_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
           "preemptions": delta.get("num_preemptions_total")}
    LOG.info("[c=%d] %s tok/min=%.0f (wallck %.0f, div %.1f%%) complete=%.1f%% "
             "cache_hit=%s preempt=%s",
             c, "VALID" if valid else "INVALID", tokmin_stats, tokmin_wall,
             divergence * 100, completeness * 100, row["prefix_cache_hit_rate"],
             row["preemptions"])
    return row


def main() -> None:
    cat = load_catalog(auto_refresh=True, offline_ok=False,
                       max_age_hours_prices=24, regions=[REGION])
    cfg = EXPERIMENTS["exp_8"]
    LOG.info("verified sweep: %s DP=%d image=%s flags=%r",
             cfg.deployment.instance_type, cfg.deployment.data_parallel,
             cfg.model_spec.vllm_gpu_image, cfg.deployment.extra_serve_flags)
    assert "0.26.0" in cfg.model_spec.vllm_gpu_image, "spec must pin the A/B winner image"
    inputs = load_inputs(20000)
    runner = DeploymentRunner(cfg, catalog=cat, hf_secret_name=HF_SECRET)
    rows: list = []
    ec2 = boto3.client("ec2", region_name=REGION)
    try:
        state = runner.launch()
        LOG.info("LAUNCHED %s mode=%s ready=%.0fs", state.instance_id,
                 state.capacity_mode, state.vllm_ready_wait_s or 0)
        sg_id = ec2.describe_instances(InstanceIds=[state.instance_id])[
            "Reservations"][0]["Instances"][0]["SecurityGroups"][0]["GroupId"]
        ensure_sg_access(ec2, sg_id)
        ep = VLLMEndpoint(base_url=state.base_url, api_key=state.api_key,
                          model_id=cfg.model_spec.served_model_name)
        sm = ep.invoke(VLLMEndpoint.create_payload(SYSTEM_PROMPT, inputs[0],
                                                   max_tokens=MAX_NEW_TOKENS))
        LOG.info("SMOKE ok in=%s out=%s", sm.num_tokens_input, sm.num_tokens_output)
        payloads = [VLLMEndpoint.create_payload(SYSTEM_PROMPT, x,
                                                max_tokens=MAX_NEW_TOKENS)
                    for x in inputs]
        for c in TIERS:
            ensure_sg_access(ec2, sg_id)
            rows.append(bench_verified(ep, payloads, c, state.base_url, state.api_key))
            save(rows)
        valid_rows = [r for r in rows if r.get("valid")]
        if valid_rows:
            best = max(valid_rows, key=lambda r: r["total_tok_min_stats"])
            rows.append({"SUMMARY": {
                "best_c": best["c"],
                "best_tok_min": best["total_tok_min_stats"],
                "vs_c800_ab_winner_3889975": round(
                    best["total_tok_min_stats"] / 3_889_975, 3),
                "all_valid": [(r["c"], r["total_tok_min_stats"]) for r in valid_rows],
                "invalid_tiers": [r["c"] for r in rows if r.get("valid") is False],
                "note": "c=600 VALID from the 22:05 run: 3,699,903 tok/min "
                        "(complete=100%, div 2.0%, cache_hit 0.96)"}})
            save(rows)
            LOG.info("SUMMARY: best c=%s -> %.0f tok/min",
                     best["c"], best["total_tok_min_stats"])
        print("RESULT=SUCCESS")
    except Exception:
        LOG.error("FAILED:\n%s", traceback.format_exc())
        rows.append({"error": traceback.format_exc()[-800:]}); save(rows)
        print("RESULT=FAILED")
    finally:
        LOG.info("TEARDOWN...")
        try:
            runner.terminate(); LOG.info("torn down")
        except Exception:  # noqa: BLE001
            LOG.error("teardown err:\n%s", traceback.format_exc())


if __name__ == "__main__":
    main()
