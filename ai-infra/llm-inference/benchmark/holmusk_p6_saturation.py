"""Phase 2: find p6-b200 (DP=8) optimal concurrency — standalone saturation sweep.

Launches ONE p6 (exp_8, TP=1/DP=8, 8 replicas) via the framework, then runs
LLMeter against the SAME live endpoint at rising concurrency tiers, recording
total(in+out) tok/min at each. Finds where throughput plateaus → that's c_opt
for Phase 3. Results discarded except the chosen c. Writes progress to a durable
JSON the instant each tier completes (survives process kill). Guaranteed teardown.

Runs from benchmark/ so `models.*` imports resolve. Detached-safe.
"""
from __future__ import annotations
import json, logging, sys, time, traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
LOG = logging.getLogger("p6_sat")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

REGION="us-west-2"; HF_SECRET="medgemma-27b-benchmark/hf-token"
DATA_DIR=Path("/Users/diponego/Projects/llm-deployment/models-deploy-and-benchmark/data/samples/medical-notes")
OUT=Path("/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/Holmusk/cross-gpu-benchmark-2026-07-21/phase2-p6-saturation.json")
MAX_NEW_TOKENS=256
# Rising concurrency tiers to find the plateau. 8× B200 DP=8 (8 replicas) — start
# where c=100 left off, climb until total tok/min stops rising.
TIERS=[100, 200, 400, 600, 800, 1200, 1600]
# per-tier request budget: enough that each tier runs ~1-2 min at steady state.
REQS_PER_TIER=6000

def load_inputs(n):
    import random
    texts=[]
    for shard in sorted(DATA_DIR.glob("*.jsonl")):
        with shard.open() as fh:
            for line in fh:
                line=line.strip()
                if not line: continue
                try: t=json.loads(line).get("text")
                except json.JSONDecodeError: continue
                if t: texts.append(t)
        if len(texts)>=n*3: break
    return random.Random(42).sample(texts, min(n,len(texts)))

def save(rows):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    LOG.info("progress persisted -> %s", OUT)

def main():
    cat=load_catalog(auto_refresh=True, offline_ok=False, max_age_hours_prices=24, regions=[REGION])
    cfg=EXPERIMENTS["exp_8"]  # p6 DP=8
    d=cfg.deployment
    LOG.info("p6 saturation sweep: %s TP=%d DP=%d tiers=%s reqs/tier=%d",
             d.instance_type, d.tensor_parallel, d.data_parallel, TIERS, REQS_PER_TIER)
    inputs=load_inputs(max(REQS_PER_TIER, 6000))
    runner=DeploymentRunner(cfg, catalog=cat, hf_secret_name=HF_SECRET)
    rows=[]; state=None
    try:
        state=runner.launch()
        LOG.info("LAUNCHED %s mode=%s ready=%.0fs", state.instance_id, state.capacity_mode, state.vllm_ready_wait_s or 0)
        ep=VLLMEndpoint(base_url=state.base_url, api_key=state.api_key, model_id=cfg.model_spec.served_model_name)
        # smoke
        sm=ep.invoke(VLLMEndpoint.create_payload(SYSTEM_PROMPT, inputs[0], max_tokens=MAX_NEW_TOKENS))
        LOG.info("SMOKE ok in=%s out=%s", sm.num_tokens_input, sm.num_tokens_output)
        from llmeter.experiments import LoadTest
        import asyncio
        payloads=[VLLMEndpoint.create_payload(SYSTEM_PROMPT, x, max_tokens=MAX_NEW_TOKENS) for x in inputs]
        prev_total=0.0
        for c in TIERS:
            lt=LoadTest(endpoint=ep, payload=payloads, sequence_of_clients=[c],
                        output_path=str(OUT.parent/f"phase2_llmeter_c{c}"),
                        min_requests_per_run=REQS_PER_TIER,
                        min_requests_per_client=max(1, REQS_PER_TIER//c))
            t0=time.time(); res=asyncio.run(lt.run()); wall=time.time()-t0
            rd=getattr(res,"results",None) or {}
            st=None
            for cl,r in rd.items():
                if int(cl)==c: st=getattr(r,"stats",None)
            in_tpm=(st or {}).get("average_input_tokens_per_minute") or 0
            out_tpm=(st or {}).get("average_output_tokens_per_minute") or 0
            total=in_tpm+out_tpm
            gain=(total-prev_total)/prev_total*100 if prev_total else None
            rows.append({"c":c,"total_tok_min":round(total,1),"out_tpm":round(out_tpm,1),
                         "ttlt_p50":(st or {}).get("time_to_last_token-p50"),
                         "ttlt_p90":(st or {}).get("time_to_last_token-p90"),
                         "req_per_min":(st or {}).get("requests_per_minute"),
                         "wall_s":round(wall,1),"gain_pct_vs_prev":round(gain,1) if gain is not None else None})
            save(rows)
            LOG.info("c=%d -> total_tok/min=%.0f (gain %s%%) ttlt_p50=%.2f wall=%.0fs",
                     c, total, f"{gain:.1f}" if gain is not None else "n/a",
                     (st or {}).get("time_to_last_token-p50") or 0, wall)
            # plateau detection: <8% gain over previous tier → we've saturated; stop.
            if gain is not None and gain < 8.0:
                LOG.info("PLATEAU: c=%d gained only %.1f%% over previous — saturation ~ c=%d", c, gain, c)
                break
            prev_total=total
        # pick c_opt = tier with max total_tok_min
        best=max(rows, key=lambda r:r["total_tok_min"])
        LOG.info("C_OPT = %d (total_tok/min=%.0f). All tiers: %s", best["c"], best["total_tok_min"],
                 [(r["c"],r["total_tok_min"]) for r in rows])
        rows.append({"C_OPT": best["c"], "note":"tier with max total tok/min"})
        save(rows)
        print(f"RESULT=SUCCESS C_OPT={best['c']}")
    except Exception:
        LOG.error("FAILED:\n%s", traceback.format_exc())
        rows.append({"error": traceback.format_exc()[-800:]}); save(rows)
        print("RESULT=FAILED")
    finally:
        LOG.info("TEARDOWN...")
        try: runner.terminate(); LOG.info("torn down")
        except Exception: LOG.error("teardown err:\n%s", traceback.format_exc())
        if state and state.instance_id: print(f"TORNDOWN={state.instance_id}")

if __name__=="__main__":
    main()
