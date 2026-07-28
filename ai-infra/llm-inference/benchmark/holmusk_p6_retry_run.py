"""p6 RETRY (2026-07-28 evening): finish what the spot reclaim took.

The 2026-07-28 afternoon run banked all four real-shape tiers, then AWS
reclaimed the instance (`Server.SpotInstanceTermination`, no capacity) 2m24s
into the short-note phase. Two things are outstanding, and one of the banked
results turned out to be unsafe to trust at face value:

  A. **Real-shape tiers above c=1000 were CLIENT-THROTTLED.** The OpenAI SDK
     defaults to max_connections=1000, so the c=1200 and c=1600 tiers only ever
     had ~1,085 requests in flight. The measured "optimum at c≈1200"
     (5.89M tok/min) is therefore a floor, not a peak — the real plateau could
     be higher. This run re-measures c=1200/1600 with the cap lifted to 4096
     and adds c=2000 to find where it actually turns over.
  B. **Short-note plateau c=1200/1600** — the original open question (is c=800
     the plateau for v0.26.0?). Lower value now that the real-shape curve
     exists, so it runs LAST.

ORDERING IS THE POINT: highest-value work first, because a second reclaim is
likely (the pool was contended — the afternoon run needed 7 acquisition
attempts, and was then reclaimed inside an hour). Everything is written to
disk the instant each tier completes.

Cross-run comparability: same image (v0.26.0-cu129), same DP=8, same flags,
same max_model_len 8192, same note corpus, same 8,000-requests-per-tier target,
same verification gates. The ONLY deliberate change is the connection cap —
which is why c=1200/1600 are re-measured rather than reused.

Run from benchmark/ detached:
  AWS_PROFILE=yudho-aiml nohup ../.venv/bin/python holmusk_p6_retry_run.py \
      >> ~/holmusk-bench-local/p6-retry-run.log 2>&1 < /dev/null & disown
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
LOG = logging.getLogger("p6_retry")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

# Reuse the afternoon run's verified machinery verbatim — same gates, same
# metrics, same economics maths, so the two runs are directly comparable.
from holmusk_p6_combined_run import (
    BACKSTOP_S,
    HF_SECRET,
    LOCAL_BASE,
    ONEDRIVE_BASE,
    PHASE1_MAX_NEW_TOKENS,
    PHASE1_REQS_PER_CLIENT,
    PHASE1_RESPONSE_TIMEOUT_S,
    PHASE2_MAX_MODEL_LEN,
    PHASE2_MAX_NEW_TOKENS,
    PHASE2_RESPONSE_TIMEOUT_S,
    PHASE2_TARGET_REQS_PER_TIER,
    REGION,
    bench_tier,
    ensure_sg_access,
    load_realshape_notes,
    load_short_inputs,
)
from holmusk_realshape_gen import SYSTEM_PROMPT_REAL
from holmusk_unique_payload import UniquePayloadEndpoint, make_http_client

import boto3
import holmusk_p6_combined_run as combined

OUT = LOCAL_BASE / "p6-retry-run-results.json"
LLMETER_DIR = LOCAL_BASE / "p6_retry_llmeter"
# Point the shared bench_tier at THIS run's output dir (module-level global).
combined.LLMETER_DIR = LLMETER_DIR

MAX_CONNECTIONS = 4096          # lift the SDK's 1000 default (the whole point)

# Real-shape re-sweep: the throttled tiers, plus one beyond to find the turn.
REALSHAPE_TIERS = [1200, 1600, 2000]
# Short-note plateau: the tiers the reclaim took.
SHORTNOTE_TIERS = [1200, 1600]


def save(rows: list) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    LOG.info("progress persisted -> %s", OUT)
    try:
        mirror = ONEDRIVE_BASE / OUT.name
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(json.dumps(rows, indent=2, default=str))
    except OSError as exc:
        LOG.warning("OneDrive mirror failed (non-fatal): %s", exc)


def main() -> None:
    cat = load_catalog(auto_refresh=True, offline_ok=False,
                       max_age_hours_prices=24, regions=[REGION])
    cfg = EXPERIMENTS["exp_8"]
    assert "0.26.0" in cfg.model_spec.vllm_gpu_image, "spec must pin the A/B winner image"
    cfg = cfg.model_copy(update={
        "deployment": cfg.deployment.model_copy(update={
            "max_model_len": PHASE2_MAX_MODEL_LEN,
            "self_terminate_backstop_s": BACKSTOP_S})})
    cfg.validate_against(cat)

    notes, note_stats = load_realshape_notes()
    LOG.info("real-shape pool: %s", note_stats)
    # Largest tier must fit the unique-note pool: 2000 * ceil(8000/2000) = 8,000.
    for c in REALSHAPE_TIERS:
        need = c * max(1, math.ceil(PHASE2_TARGET_REQS_PER_TIER / c))
        assert need <= len(notes), f"c={c} needs {need} unique notes, pool has {len(notes)}"
    short_inputs = load_short_inputs(20000)

    rows: list = [{"RUN_META": {
        "date": "2026-07-28 (retry)", "purpose":
            "re-measure real-shape c>=1200 with the client connection cap "
            "lifted (prior run throttled at ~1085 in flight), then finish the "
            "short-note plateau tiers lost to the spot reclaim",
        "prior_run": "p6-combined-run-results.json",
        "prior_run_ended": "Server.SpotInstanceTermination 18:01:25 SGT",
        "instance": cfg.deployment.instance_type, "region": REGION,
        "image": cfg.model_spec.vllm_gpu_image,
        "data_parallel": cfg.deployment.data_parallel,
        "max_model_len": cfg.deployment.max_model_len,
        "extra_serve_flags": cfg.deployment.extra_serve_flags,
        "max_connections": MAX_CONNECTIONS,
        "realshape_tiers": REALSHAPE_TIERS, "shortnote_tiers": SHORTNOTE_TIERS,
        "real_shape_notes": note_stats,
        "comparability": "identical to the afternoon run except max_connections",
    }}]
    save(rows)

    runner = DeploymentRunner(cfg, catalog=cat, hf_secret_name=HF_SECRET)
    ec2 = boto3.client("ec2", region_name=REGION)
    http_client = None
    try:
        state = runner.launch()
        LOG.info("LAUNCHED %s mode=%s az=%s ready=%.0fs", state.instance_id,
                 state.capacity_mode, state.placement_az,
                 state.vllm_ready_wait_s or 0)
        hourly = cat.live_spot(cfg.deployment.instance_type, REGION,
                               state.placement_az)
        LOG.info("live spot in %s: $%s/hr", state.placement_az, hourly)
        rows[0]["RUN_META"].update({"instance_id": state.instance_id,
                                    "capacity_mode": state.capacity_mode,
                                    "az": state.placement_az,
                                    "hourly_usd": hourly})
        save(rows)

        sg_id = ec2.describe_instances(InstanceIds=[state.instance_id])[
            "Reservations"][0]["Instances"][0]["SecurityGroups"][0]["GroupId"]
        ensure_sg_access(ec2, sg_id)

        # ---- PHASE A: real-shape re-sweep, uncapped ------------------------
        http_client = make_http_client(MAX_CONNECTIONS, PHASE2_RESPONSE_TIMEOUT_S)
        ep_real = UniquePayloadEndpoint(
            base_url=state.base_url, api_key=state.api_key,
            model_id=cfg.model_spec.served_model_name,
            notes=notes, system_prompt=SYSTEM_PROMPT_REAL,
            max_tokens=PHASE2_MAX_NEW_TOKENS,
            timeout=float(PHASE2_RESPONSE_TIMEOUT_S),
            http_client=http_client)
        # Prove the cap actually lifted — otherwise this run measures nothing new.
        pool = ep_real._client._client._transport._pool
        LOG.info("HTTP pool max_connections=%s (was 1000 by default)",
                 pool._max_connections)
        assert pool._max_connections == MAX_CONNECTIONS, "connection cap NOT lifted"
        rows[0]["RUN_META"]["verified_pool_max_connections"] = pool._max_connections

        smoke = ep_real.invoke(VLLMEndpoint.create_payload(
            SYSTEM_PROMPT_REAL, notes[0], max_tokens=PHASE2_MAX_NEW_TOKENS))
        LOG.info("REAL-SHAPE SMOKE in=%s out=%s", smoke.num_tokens_input,
                 smoke.num_tokens_output)
        save(rows)

        placeholder = [VLLMEndpoint.create_payload(
            SYSTEM_PROMPT_REAL, notes[0], max_tokens=PHASE2_MAX_NEW_TOKENS)]
        for c in REALSHAPE_TIERS:
            ensure_sg_access(ec2, sg_id)
            k = max(1, math.ceil(PHASE2_TARGET_REQS_PER_TIER / c))
            try:
                rows.append(bench_tier(
                    ep_real, placeholder, c, k, "retry_realshape_uncapped",
                    f"real 3k/1k, unique notes, max_conn={MAX_CONNECTIONS}",
                    state.base_url, state.api_key, hourly,
                    timeout_s=PHASE2_RESPONSE_TIMEOUT_S))
            except Exception:  # noqa: BLE001
                LOG.error("realshape c=%d FAILED:\n%s", c, traceback.format_exc())
                rows.append({"phase": "retry_realshape_uncapped", "c": c,
                             "error": traceback.format_exc()[-600:]})
            save(rows)

        # ---- PHASE B: short-note plateau (the lower-value leftover) --------
        ep_short = VLLMEndpoint(base_url=state.base_url, api_key=state.api_key,
                                model_id=cfg.model_spec.served_model_name,
                                http_client=make_http_client(
                                    MAX_CONNECTIONS, PHASE1_RESPONSE_TIMEOUT_S))
        short_payloads = [VLLMEndpoint.create_payload(
            SYSTEM_PROMPT, x, max_tokens=PHASE1_MAX_NEW_TOKENS)
            for x in short_inputs]
        for c in SHORTNOTE_TIERS:
            ensure_sg_access(ec2, sg_id)
            try:
                rows.append(bench_tier(
                    ep_short, short_payloads, c, PHASE1_REQS_PER_CLIENT,
                    "retry_shortnote_plateau",
                    "short synthetic, repeating payloads (matches c=600/800)",
                    state.base_url, state.api_key, hourly,
                    timeout_s=PHASE1_RESPONSE_TIMEOUT_S))
            except Exception:  # noqa: BLE001
                LOG.error("shortnote c=%d FAILED:\n%s", c, traceback.format_exc())
                rows.append({"phase": "retry_shortnote_plateau", "c": c,
                             "error": traceback.format_exc()[-600:]})
            save(rows)

        # ---- summary -------------------------------------------------------
        rs = [r for r in rows if r.get("phase") == "retry_realshape_uncapped"
              and r.get("valid")]
        sn = [r for r in rows if r.get("phase") == "retry_shortnote_plateau"
              and r.get("valid")]
        summary: dict = {"hourly_usd": hourly,
                         "max_connections": MAX_CONNECTIONS}
        if rs:
            best = max(rs, key=lambda r: r["total_tok_min_stats"])
            summary["realshape_uncapped"] = {
                "curve": [(r["c"], r["total_tok_min_stats"],
                           r.get("usd_per_1k_notes")) for r in rs],
                "best_c": best["c"],
                "best_tok_min": best["total_tok_min_stats"],
                "usd_per_1k_notes": best.get("usd_per_1k_notes"),
                "hours_for_12m_notes": best.get("hours_for_12m_notes"),
                "usd_for_12m_notes": best.get("usd_for_12m_notes"),
                "vs_capped_c1200_5885746": round(
                    best["total_tok_min_stats"] / 5_885_746, 3),
                "note": "compare against the CAPPED afternoon run: c=1200 "
                        "5,885,746 tok/min / $0.4647 per 1k notes",
            }
        if sn:
            summary["shortnote_plateau"] = {
                "new_tiers": [(r["c"], r["total_tok_min_stats"]) for r in sn],
                "prior_c600": 3_699_903, "prior_c800": 3_767_875,
                "ab_arm_b_c800": 3_889_975,
                "note": "c=800 is the plateau iff these don't exceed ~3.89M "
                        "by more than cross-run variance (~3%)",
            }
        summary["invalid_tiers"] = [(r.get("phase"), r.get("c"))
                                    for r in rows if r.get("valid") is False]
        rows.append({"SUMMARY": summary})
        save(rows)
        LOG.info("SUMMARY:\n%s", json.dumps(summary, indent=2, default=str))
        print("RESULT=SUCCESS")
    except Exception:
        LOG.error("FAILED:\n%s", traceback.format_exc())
        rows.append({"error": traceback.format_exc()[-1500:]})
        save(rows)
        print("RESULT=FAILED")
    finally:
        LOG.info("TEARDOWN...")
        try:
            runner.terminate()
            LOG.info("torn down")
        except Exception:  # noqa: BLE001
            LOG.error("teardown err:\n%s", traceback.format_exc())
        if http_client is not None:
            try:
                http_client.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
