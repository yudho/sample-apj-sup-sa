"""p6 COMBINED RUN (2026-07-28): finish the c-sweep + the REAL-SHAPE benchmark.

One p6-b200 instance, two phases back-to-back so the ~10-min warmup and the
scarce-capacity acquisition are paid ONCE.

  PHASE 1 — close the plateau question (short synthetic notes, ~250 in/512 out)
      c = 1200, 1600 on the A/B winner config (v0.26.0-cu129 + original flags).
      Last night banked c=600 (3.70M) and c=800 (3.77M) VALID before the run was
      stopped; these two tiers are what's missing to prove c=800 is the plateau.
      Run with the SAME (payload-repeating) method as those tiers so the numbers
      are directly comparable — see PAYLOAD FIDELITY below.

  PHASE 2 — the decisive measurement: REAL SHAPE
      Holmusk's actual phase-1 workload (GPU info-Holmusk.xlsx): 36B input +
      12B output over 12M notes = ~3,000 in / ~1,000 out per note, only ~1% of
      the prompt cacheable. Every p6 number to date is on ~250-token notes, so
      all per-note economics so far are extrapolation. This phase measures it:
        - 40k synthetic ~2,940-token notes (holmusk_realshape_gen.py, real
          MedGemma tokenizer), each request a UNIQUE note.
        - Their verbatim prompt + 9-field per-drug JSON schema from the xlsx.
        - max_model_len 8192 (3k in + 1k out + headroom; the 4096 used for the
          short-note runs would truncate).
        - Its own c-sweep: the optimum WILL move — each request now carries ~12x
          the prefill work, so the KV/compute balance is completely different.

PAYLOAD FIDELITY (measurement bug found + fixed 2026-07-28)
-----------------------------------------------------------
LLMeter 0.1.12 seeds its per-client shuffle with a CONSTANT (random.seed(0) in
runner.py::_invoke_n_no_wait), so all C clients walk the same permutation from
the start: last night's c=800 tier served 40,000 requests from just 49 distinct
prompts (~816 repeats each), which is why /metrics showed a 96% prefix-cache
hit rate. Phase 2 therefore routes through UniquePayloadEndpoint (see
holmusk_unique_payload.py) so every request is a distinct note and the cache
hit rate reflects Holmusk's real ~1%.
Phase 1 deliberately KEEPS the old repeating behaviour: its purpose is to
extend last night's curve, and switching methods mid-curve would confound the
plateau comparison. Both modes are recorded per tier in the output JSON.

Safety / ops (all lessons from prior runs):
  - us-west-2 spot (~$39.75/hr), _SCARCE_GPU ladder spot->on-demand->odcr.
  - SG self-heal now lives INSIDE the framework ready-poll (fixed today), plus
    a belt-and-braces refresh before every tier here.
  - LOCAL-first IO; OneDrive mirror is best-effort only (it stalled Errno-60
    mid-run once and killed a whole sweep).
  - Per-tier verification: completeness gate >=98%, 3-way tok/min cross-check,
    /metrics prefix-cache + preemption scrape.
  - Durable JSON write after EVERY tier; guaranteed teardown in finally;
    in-guest 90-min self-terminate backstop from the user-data template.
  - PHASE 2 RUNS FIRST if RUN_REALSHAPE_FIRST is set — it's the more valuable
    measurement, so on a capacity/time squeeze it should not be the casualty.

Run from benchmark/ detached:
  AWS_PROFILE=yudho-aiml nohup ../.venv/bin/python holmusk_p6_combined_run.py \
      >> ~/holmusk-bench-local/p6-combined-run.log 2>&1 < /dev/null & disown
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import sys
import time
import traceback
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
LOG = logging.getLogger("p6_combined")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

from holmusk_realshape_gen import SYSTEM_PROMPT_REAL
from holmusk_unique_payload import UniquePayloadEndpoint

import boto3

REGION = "us-west-2"
HF_SECRET = "medgemma-27b-benchmark/hf-token"

SHORT_DATA_DIR = Path("/Users/diponego/Projects/llm-deployment/"
                      "models-deploy-and-benchmark/data/samples/medical-notes")
REALSHAPE_FILE = Path.home() / "holmusk-bench-local" / "real-shape-notes" / "notes.jsonl"

LOCAL_BASE = Path.home() / "holmusk-bench-local"
ONEDRIVE_BASE = Path("/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/"
                     "Holmusk/cross-gpu-benchmark-2026-07-21")
OUT = LOCAL_BASE / "p6-combined-run-results.json"
LLMETER_DIR = LOCAL_BASE / "p6_combined_llmeter"

# ---- phase 1: extend last night's curve ---------------------------------
PHASE1_TIERS = [1200, 1600]
PHASE1_MAX_NEW_TOKENS = 512
PHASE1_REQS_PER_CLIENT = 50
# LLMeter's 60s default per-response timeout is NOT safe even for short notes
# at these concurrencies (a timeout kills the whole client silently), and last
# night's c=800 tier already showed ttlt_p90 ~7s. Give plenty of headroom.
PHASE1_RESPONSE_TIMEOUT_S = 300

# ---- phase 2: real shape -------------------------------------------------
# Their sheet: 12B output / 12M notes = ~1,000 output tokens per note.
PHASE2_MAX_NEW_TOKENS = 1024
PHASE2_TIERS = [400, 800, 1200, 1600]
# Requests per tier are held ~CONSTANT rather than per-client, so a high-c tier
# doesn't balloon: at K=20 fixed, c=1600 would be 32,000 requests (~4x the work
# of the c=400 tier) for no extra statistical value. ceil(TARGET/c) per client
# gives every tier ~8,000 requests = 32M input tokens, several minutes of
# steady state at any plausible throughput. It also keeps the largest tier
# inside the 40k unique-note pool (the pool is the hard constraint: every
# request must get a distinct note).
PHASE2_TARGET_REQS_PER_TIER = 8000
PHASE2_MAX_MODEL_LEN = 8192      # 3k in + 1k out + headroom

# The in-guest backstop must outlive this run's worst case, or it kills the
# instance mid-sweep and the remaining tiers are lost. Budget: p6 cold start
# has run as long as ~50 min (57 GB HF download + 8 DP engines + CUDA graphs),
# plus ~60 min of tiers, plus slack.
BACKSTOP_S = 4 * 3600

COMPLETENESS_MIN = 0.98
DIVERGENCE_MAX = 0.05
# A few stragglers on a 32k-request tier are noise; a systematic loss is not.
# (The audit case was 9% of requests vanishing while LLMeter said 0 failures.)
ERROR_RATE_MAX = 0.01
# Real-shape requests are ~12x heavier; a 1,024-token generation at high
# concurrency can legitimately exceed llmeter's 60s default per-response
# timeout, and a timeout SILENTLY DROPS THE WHOLE CLIENT (audit finding:
# arm-C lost 9% of responses while reporting failed_requests=0).
PHASE2_RESPONSE_TIMEOUT_S = 600


def save(rows: list) -> None:
    """Write locally (critical path); mirror to OneDrive best-effort."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    LOG.info("progress persisted -> %s", OUT)
    try:
        mirror = ONEDRIVE_BASE / OUT.name
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(json.dumps(rows, indent=2, default=str))
    except OSError as exc:
        LOG.warning("OneDrive mirror failed (non-fatal): %s", exc)


def load_short_inputs(n: int) -> list[str]:
    import random
    texts: list[str] = []
    for shard in sorted(SHORT_DATA_DIR.glob("*.jsonl")):
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


def load_realshape_notes() -> tuple[list[str], dict]:
    if not REALSHAPE_FILE.exists():
        raise FileNotFoundError(
            f"{REALSHAPE_FILE} missing — run holmusk_realshape_gen.py first")
    notes: list[str] = []
    toks: list[int] = []
    with REALSHAPE_FILE.open() as fh:
        for line in fh:
            rec = json.loads(line)
            notes.append(rec["text"])
            toks.append(rec["tokens"])
    stats = {"n_notes": len(notes), "mean_note_tokens": round(statistics.mean(toks), 1),
             "p50_note_tokens": int(statistics.median(toks)),
             "min_note_tokens": min(toks), "max_note_tokens": max(toks)}
    return notes, stats


def my_egress_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


def ensure_sg_access(ec2, sg_id: str) -> None:
    """Belt-and-braces: the framework now self-heals inside the ready-poll too."""
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8001,
                            "IpRanges": [{"CidrIp": f"{my_egress_ip()}/32",
                                          "Description": "combined-run self-heal"}]}])
    except Exception as exc:  # noqa: BLE001
        if "InvalidPermission.Duplicate" not in str(exc):
            LOG.warning("SG self-heal: %s", exc)


def scrape_metrics(base_url: str, api_key: str) -> dict:
    """Pull prefix-cache + preemption counters from vLLM /metrics (summed over DP)."""
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
                        out[key] = out.get(key, 0.0) + float(line.rsplit(" ", 1)[1])
                    except (ValueError, IndexError):
                        pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


def count_responses(outdir: Path) -> tuple[int, int, int, int]:
    """(n_ok_responses, sum_in_tok, sum_out_tok, n_errors) from responses.jsonl.

    Errored rows are EXCLUDED from the success count: LLMeter writes a row for
    a timed-out request too, so counting them would let a tier where every
    request failed still report 100% completeness.
    """
    ok = ti = to = errs = 0
    for f in outdir.rglob("responses.jsonl"):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("error"):
                    errs += 1
                    continue
                ok += 1
                ti += r.get("num_tokens_input") or 0
                to += r.get("num_tokens_output") or 0
    return ok, ti, to, errs


def distinct_prompt_count(outdir: Path) -> int:
    """How many DISTINCT prompts actually hit the model (the bug detector)."""
    seen: set = set()
    for f in outdir.rglob("responses.jsonl"):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = r.get("input_prompt")
                if p is None:
                    p = json.dumps(r.get("input_payload"), sort_keys=True)[:400]
                seen.add(hash(p))
    return len(seen)


def bench_tier(endpoint, payloads, c: int, k: int, phase: str, tag: str,
               base_url: str, api_key: str, hourly: float | None,
               timeout_s: int) -> dict:
    """Run one concurrency tier with full verification."""
    import asyncio
    outdir = LLMETER_DIR / phase / f"c{c}"
    # A stale dir from an earlier attempt would be double-counted by rglob.
    if outdir.exists():
        import shutil
        shutil.rmtree(outdir)
    m0 = scrape_metrics(base_url, api_key)

    if isinstance(endpoint, UniquePayloadEndpoint):
        needed = c * k
        if endpoint.remaining() < needed:
            endpoint.reset_pool()
        if endpoint.remaining() < needed:
            raise RuntimeError(
                f"pool too small: need {needed} unique notes, have "
                f"{endpoint.remaining()}")
        endpoint.reset_pool()

    # Drive Runner directly rather than via LoadTest: LoadTest exposes no
    # `timeout`, and its 60s Runner default silently drops an ENTIRE client on
    # one slow response (the audit's 9%-loss-with-0-failures bug). LoadTest is
    # only a thin per-concurrency wrapper around exactly this call, and
    # min_requests_per_client=k maps to n_requests=k, so the measurement method
    # is unchanged from last night's banked tiers.
    from llmeter.runner import Runner
    t0 = time.time()
    runner = Runner(endpoint=endpoint, output_path=str(outdir),
                    timeout=float(timeout_s))
    res_single = asyncio.run(runner.run(clients=c, n_requests=k,
                                        payload=payloads, run_name=f"c{c}"))
    res = type("R", (), {"results": {c: res_single}})()
    wall = time.time() - t0
    m1 = scrape_metrics(base_url, api_key)

    st = {}
    for cl, r in (getattr(res, "results", None) or {}).items():
        if int(cl) == c:
            st = getattr(r, "stats", None) or {}
    in_tpm = st.get("average_input_tokens_per_minute") or 0
    out_tpm = st.get("average_output_tokens_per_minute") or 0

    expected = c * k
    n_disk, ti_disk, to_disk, errs = count_responses(outdir)
    n_distinct = distinct_prompt_count(outdir)
    completeness = n_disk / expected if expected else 0
    tokmin_stats = in_tpm + out_tpm
    # Cross-check against LLMeter's own load-test window, NOT our outer wall
    # clock: `wall` also covers payload serialization + stats assembly, which
    # for a 590 MB real-shape pool is minutes of pure setup and would show as
    # a bogus divergence. Fall back to the outer clock only if unavailable.
    test_window = None
    for cl, r in (getattr(res, "results", None) or {}).items():
        if int(cl) == c:
            test_window = getattr(r, "total_test_time", None)
    basis = test_window or wall
    tokmin_wall = (ti_disk + to_disk) / basis * 60 if basis else 0
    divergence = (abs(tokmin_stats - tokmin_wall) / tokmin_stats
                  if tokmin_stats else 1.0)
    error_rate = errs / expected if expected else 0.0
    valid = (completeness >= COMPLETENESS_MIN
             and divergence <= DIVERGENCE_MAX
             and error_rate <= ERROR_RATE_MAX)

    delta = {key: round((m1.get(key, 0) or 0) - (m0.get(key, 0) or 0), 1)
             for key in ("prefix_cache_queries_total", "prefix_cache_hits_total",
                         "num_preemptions_total")}
    hit_rate = (delta["prefix_cache_hits_total"] / delta["prefix_cache_queries_total"]
                if delta.get("prefix_cache_queries_total") else None)

    mean_in = ti_disk / n_disk if n_disk else 0
    mean_out = to_disk / n_disk if n_disk else 0
    # Throughput economics must use the steady-state serving window, not our
    # outer clock (which includes client-side setup the customer wouldn't pay
    # a GPU for).
    notes_per_hr = (n_disk / basis * 3600) if basis else 0
    row = {
        "phase": phase, "tag": tag, "c": c, "reqs_per_client": k,
        "valid": valid,
        "completeness": round(completeness, 4),
        "responses": n_disk, "expected": expected, "errors": errs,
        "error_rate": round(error_rate, 5),
        "distinct_prompts": n_distinct,
        "unique_payloads": isinstance(endpoint, UniquePayloadEndpoint),
        "total_tok_min_stats": round(tokmin_stats, 1),
        "total_tok_min_wallclock": round(tokmin_wall, 1),
        "divergence": round(divergence, 4),
        "in_tok_min": round(in_tpm, 1), "out_tok_min": round(out_tpm, 1),
        "mean_input_tokens": round(mean_in, 1),
        "mean_output_tokens": round(mean_out, 1),
        "req_per_min": st.get("requests_per_minute"),
        "notes_per_hour": round(notes_per_hr, 1),
        "ttlt_p50": st.get("time_to_last_token-p50"),
        "ttlt_p90": st.get("time_to_last_token-p90"),
        "ttlt_p99": st.get("time_to_last_token-p99"),
        "failed_llmeter": st.get("failed_requests"),
        "wall_s": round(wall, 1),
        "test_window_s": round(basis, 1),
        "prefix_cache_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "preemptions": delta.get("num_preemptions_total"),
    }
    if hourly and notes_per_hr:
        row["usd_per_1k_notes"] = round(hourly / notes_per_hr * 1000, 5)
        row["usd_per_1m_total_tokens"] = (
            round(hourly / (tokmin_stats * 60) * 1e6, 5) if tokmin_stats else None)
        row["hours_for_12m_notes"] = round(12_000_000 / notes_per_hr, 2)
        row["usd_for_12m_notes"] = round(12_000_000 / notes_per_hr * hourly, 2)

    LOG.info("[%s c=%d] %s tok/min=%.0f (div %.1f%%) complete=%.1f%% err=%.2f%% "
             "distinct=%d in=%.0f out=%.0f cache=%s preempt=%s $/1k=%s",
             phase, c, "VALID" if valid else "INVALID", tokmin_stats,
             divergence * 100, completeness * 100, error_rate * 100,
             n_distinct, mean_in, mean_out,
             row["prefix_cache_hit_rate"], row["preemptions"],
             row.get("usd_per_1k_notes"))
    return row


def main() -> None:
    cat = load_catalog(auto_refresh=True, offline_ok=False,
                       max_age_hours_prices=24, regions=[REGION])
    cfg = EXPERIMENTS["exp_8"]
    assert "0.26.0" in cfg.model_spec.vllm_gpu_image, "spec must pin the A/B winner image"

    # Phase 2 needs a bigger context window than the short-note runs used.
    # DeploymentPlan is frozen, so rebuild it rather than mutating in place.
    cfg = cfg.model_copy(update={
        "deployment": cfg.deployment.model_copy(update={
            "max_model_len": PHASE2_MAX_MODEL_LEN,
            "self_terminate_backstop_s": BACKSTOP_S})})
    cfg.validate_against(cat)

    notes, note_stats = load_realshape_notes()
    LOG.info("real-shape pool: %s", note_stats)
    short_inputs = load_short_inputs(20000)

    rows: list = [{"RUN_META": {
        "date": "2026-07-28", "instance": cfg.deployment.instance_type,
        "region": REGION, "image": cfg.model_spec.vllm_gpu_image,
        "data_parallel": cfg.deployment.data_parallel,
        "max_model_len": cfg.deployment.max_model_len,
        "extra_serve_flags": cfg.deployment.extra_serve_flags,
        "phase1_tiers": PHASE1_TIERS, "phase2_tiers": PHASE2_TIERS,
        "real_shape_notes": note_stats,
        "phase2_prompt": "verbatim from GPU info-Holmusk.xlsx B52",
        "known_llmeter_bug": "random.seed(0) per-client shuffle -> phase1 tiers "
                             "repeat payloads (kept for curve comparability); "
                             "phase2 uses UniquePayloadEndpoint",
    }}]
    save(rows)

    runner = DeploymentRunner(cfg, catalog=cat, hf_secret_name=HF_SECRET)
    ec2 = boto3.client("ec2", region_name=REGION)
    hourly: float | None = None
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

        # ---------------- PHASE 2 FIRST (the decisive measurement) ----------
        ep_real = UniquePayloadEndpoint(
            base_url=state.base_url, api_key=state.api_key,
            model_id=cfg.model_spec.served_model_name,
            notes=notes, system_prompt=SYSTEM_PROMPT_REAL,
            max_tokens=PHASE2_MAX_NEW_TOKENS,
            timeout=float(PHASE2_RESPONSE_TIMEOUT_S))
        smoke = ep_real.invoke(VLLMEndpoint.create_payload(
            SYSTEM_PROMPT_REAL, notes[0], max_tokens=PHASE2_MAX_NEW_TOKENS))
        LOG.info("REAL-SHAPE SMOKE in=%s out=%s | extract preview: %s",
                 smoke.num_tokens_input, smoke.num_tokens_output,
                 (smoke.response_text or "")[:300].replace("\n", " "))
        rows.append({"REALSHAPE_SMOKE": {
            "input_tokens": smoke.num_tokens_input,
            "output_tokens": smoke.num_tokens_output,
            "response_preview": (smoke.response_text or "")[:1200]}})
        save(rows)

        placeholder = [VLLMEndpoint.create_payload(
            SYSTEM_PROMPT_REAL, notes[0], max_tokens=PHASE2_MAX_NEW_TOKENS)]
        for c in PHASE2_TIERS:
            ensure_sg_access(ec2, sg_id)
            k = max(1, math.ceil(PHASE2_TARGET_REQS_PER_TIER / c))
            try:
                rows.append(bench_tier(ep_real, placeholder, c,
                                       k, "phase2_realshape",
                                       "real 3k-in/1k-out, unique notes",
                                       state.base_url, state.api_key, hourly,
                                       timeout_s=PHASE2_RESPONSE_TIMEOUT_S))
            except Exception:  # noqa: BLE001 — one bad tier must not lose the rest
                LOG.error("phase2 c=%d FAILED:\n%s", c, traceback.format_exc())
                rows.append({"phase": "phase2_realshape", "c": c,
                             "error": traceback.format_exc()[-600:]})
            save(rows)

        # ---------------- PHASE 1: finish last night's curve ----------------
        ep_short = VLLMEndpoint(base_url=state.base_url, api_key=state.api_key,
                                model_id=cfg.model_spec.served_model_name)
        short_payloads = [VLLMEndpoint.create_payload(
            SYSTEM_PROMPT, x, max_tokens=PHASE1_MAX_NEW_TOKENS)
            for x in short_inputs]
        for c in PHASE1_TIERS:
            ensure_sg_access(ec2, sg_id)
            try:
                rows.append(bench_tier(ep_short, short_payloads, c,
                                       PHASE1_REQS_PER_CLIENT, "phase1_plateau",
                                       "short synthetic, repeating payloads "
                                       "(matches c=600/800 method)",
                                       state.base_url, state.api_key, hourly,
                                       timeout_s=PHASE1_RESPONSE_TIMEOUT_S))
            except Exception:  # noqa: BLE001
                LOG.error("phase1 c=%d FAILED:\n%s", c, traceback.format_exc())
                rows.append({"phase": "phase1_plateau", "c": c,
                             "error": traceback.format_exc()[-600:]})
            save(rows)

        # ---------------- summary ------------------------------------------
        p2 = [r for r in rows if r.get("phase") == "phase2_realshape" and r.get("valid")]
        p1 = [r for r in rows if r.get("phase") == "phase1_plateau" and r.get("valid")]
        summary: dict = {"hourly_usd": hourly}
        if p2:
            best = max(p2, key=lambda r: r["total_tok_min_stats"])
            summary["realshape"] = {
                "best_c": best["c"],
                "best_total_tok_min": best["total_tok_min_stats"],
                "mean_input_tokens": best["mean_input_tokens"],
                "mean_output_tokens": best["mean_output_tokens"],
                "notes_per_hour": best["notes_per_hour"],
                "usd_per_1k_notes": best.get("usd_per_1k_notes"),
                "hours_for_12m_notes": best.get("hours_for_12m_notes"),
                "usd_for_12m_notes": best.get("usd_for_12m_notes"),
                "prefix_cache_hit_rate": best["prefix_cache_hit_rate"],
                "curve": [(r["c"], r["total_tok_min_stats"],
                           r.get("usd_per_1k_notes")) for r in p2],
            }
        if p1:
            summary["plateau_check"] = {
                "new_tiers": [(r["c"], r["total_tok_min_stats"]) for r in p1],
                "prior_c600": 3_699_903, "prior_c800": 3_767_875,
                "ab_arm_b_c800": 3_889_975,
                "note": "c=800 is the plateau iff these do not exceed "
                        "~3.89M by more than cross-run variance (~3%)",
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


if __name__ == "__main__":
    main()
