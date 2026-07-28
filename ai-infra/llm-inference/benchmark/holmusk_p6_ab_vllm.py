"""p6 A/B: vLLM upgrade + scheduler flags (L2 lever) — one instance, 4 arms.

Arms (same p6, same data, container swapped in place via SSM):
  A  v0.25.1  current flags                       c=800   (baseline replication)
  B  v0.26.0-cu129  current flags                 c=800   (version effect)
  C  v0.26.0-cu129  +async-sched +batched-tokens  c=800   (flags effect)
  D  winner of B/C                                c=600,1200,1600 (plateau re-sweep)

Design notes:
- Weights stay in the host page cache (/opt/hf-cache) across swaps -> each new
  container skips the 57 GB download; restart is load+CUDA-graph only.
- New image is pre-pulled in the background DURING arm A to save billed time.
- SG self-heal before every poll (NAT egress IP rotated mid-run on 2026-07-24
  and silently wedged the ready-poll for 44 min — never again).
- If a container fails to become ready (bad flag / driver mismatch), we retry
  once with a degraded config (drop --async-scheduling; then fall back to the
  non-cu129 image) instead of dying.
- Durable JSON after every arm; guaranteed teardown in finally.

Run from benchmark/ detached:
  AWS_PROFILE=yudho-aiml nohup ../.venv/bin/python holmusk_p6_ab_vllm.py \
      >> "$RESDIR/p6-ab-vllm.log" 2>&1 < /dev/null & disown
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
LOG = logging.getLogger("p6_ab")

from models.medgemma_27b import EXPERIMENTS, load_catalog, SYSTEM_PROMPT
from vllm_ec2_bench import DeploymentRunner
from vllm_ec2_bench.endpoint import VLLMEndpoint

import boto3

REGION = "us-west-2"
HF_SECRET = "medgemma-27b-benchmark/hf-token"
DATA_DIR = Path("/Users/diponego/Projects/llm-deployment/models-deploy-and-benchmark/data/samples/medical-notes")
OUT = Path("/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/Holmusk/"
           "cross-gpu-benchmark-2026-07-21/p6-ab-vllm-results.json")
MAX_NEW_TOKENS = 512           # parity with the 3.80M tok/min Phase-3 baseline
REQS_PER_CLIENT = 50           # fixed-per-client (proven): total = c * 50

IMG_OLD = "vllm/vllm-openai:v0.25.1"
IMG_NEW = "vllm/vllm-openai:v0.26.0-cu129-ubuntu2404"
IMG_NEW_FALLBACK = "vllm/vllm-openai:v0.26.0-ubuntu2404"  # if cu129 trips on host driver

BASE_FLAGS = "--max-num-seqs 512"
TUNED_FLAGS = "--max-num-seqs 512 --async-scheduling --max-num-batched-tokens 16384"

CONTAINER = "medgemma-27b-vllm"
MODEL_ID = "google/medgemma-27b-text-it"
SERVED = "medgemma-27b"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    LOG.info("progress persisted -> %s", OUT)


def my_egress_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


def ensure_sg_access(ec2, sg_id: str) -> None:
    """Add the CURRENT egress IP to the SG (idempotent). NAT rotation guard."""
    ip = my_egress_ip()
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8001,
                "IpRanges": [{"CidrIp": f"{ip}/32",
                              "Description": "ab-runner egress self-heal"}],
            }],
        )
        LOG.info("SG %s: added %s/32", sg_id, ip)
    except Exception as exc:  # noqa: BLE001
        if "InvalidPermission.Duplicate" in str(exc):
            pass
        else:
            LOG.warning("SG self-heal: %s", exc)


def ssm_run(ssm, instance_id: str, commands: list[str], timeout_s: int = 240) -> str:
    cid = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
    )["Command"]["CommandId"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            out = inv.get("StandardOutputContent", "")
            if inv["Status"] != "Success":
                LOG.warning("SSM %s: %s\n%s", inv["Status"],
                            inv.get("StandardErrorContent", "")[:500], out[:500])
            return out
    raise TimeoutError(f"SSM command {cid} did not finish in {timeout_s}s")


def docker_run_cmd(image: str, flags: str, api_key: str) -> str:
    """Mirror the user-data template's docker run, with image/flags swapped."""
    return (
        "HF_TOKEN=$(aws secretsmanager get-secret-value "
        f"--secret-id '{HF_SECRET}' --region '{REGION}' "
        "--query SecretString --output text 2>/dev/null || true); "
        f"docker run -d --name {CONTAINER} --restart unless-stopped --gpus all "
        "--shm-size=16g -p 8000:8000 "
        "-e HF_TOKEN=\"$HF_TOKEN\" -e HUGGING_FACE_HUB_TOKEN=\"$HF_TOKEN\" "
        "-e HF_HOME=/root/.cache/huggingface "
        "-v /opt/hf-cache:/root/.cache/huggingface "
        f"{image} "
        f"--model '{MODEL_ID}' --served-model-name '{SERVED}' "
        "--tensor-parallel-size 1 --data-parallel-size 8 "
        "--pipeline-parallel-size 1 --dtype bfloat16 --max-model-len 4096 "
        f"--gpu-memory-utilization 0.90 --api-key '{api_key}' "
        f"--trust-remote-code --enable-prefix-caching {flags}"
    )


def swap_container(ssm, ec2, instance_id: str, sg_id: str, base_url: str,
                   api_key: str, image: str, flags: str, label: str) -> bool:
    """Replace the vLLM container in place; wait until /v1/models is 200."""
    LOG.info("[%s] swapping container -> %s | flags: %s", label, image, flags)
    ssm_run(ssm, instance_id, [
        f"docker rm -f {CONTAINER} || true",
        docker_run_cmd(image, flags, api_key),
        # re-attach the log streamer so failures stay diagnosable
        f"nohup docker logs -f {CONTAINER} > /var/log/ab-{label}-vllm.log 2>&1 &",
        "echo swapped",
    ], timeout_s=300)
    # poll ready (weights come from page cache -> expect 2-6 min)
    deadline = time.time() + 15 * 60
    url = f"{base_url}/models"
    while time.time() < deadline:
        ensure_sg_access(ec2, sg_id)
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    LOG.info("[%s] container READY", label)
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(12)
    # not ready — grab the container log tail for diagnosis
    tail = ssm_run(ssm, instance_id,
                   [f"docker logs --tail 25 {CONTAINER} 2>&1 || echo no-container"],
                   timeout_s=120)
    LOG.error("[%s] NOT READY in 15 min. log tail:\n%s", label, tail[-1500:])
    return False


def bench(endpoint, payloads, c: int, label: str, phase_dir: str) -> dict:
    from llmeter.experiments import LoadTest
    import asyncio
    lt = LoadTest(endpoint=endpoint, payload=payloads, sequence_of_clients=[c],
                  output_path=str(OUT.parent / "p6_ab_llmeter" / phase_dir),
                  min_requests_per_run=1, min_requests_per_client=REQS_PER_CLIENT)
    t0 = time.time()
    res = asyncio.run(lt.run())
    wall = time.time() - t0
    st = None
    for cl, r in (getattr(res, "results", None) or {}).items():
        if int(cl) == c:
            st = getattr(r, "stats", None)
    st = st or {}
    in_tpm = st.get("average_input_tokens_per_minute") or 0
    out_tpm = st.get("average_output_tokens_per_minute") or 0
    row = {"arm": label, "c": c,
           "total_tok_min": round(in_tpm + out_tpm, 1),
           "out_tok_min": round(out_tpm, 1),
           "req_per_min": st.get("requests_per_minute"),
           "ttlt_p50": st.get("time_to_last_token-p50"),
           "ttlt_p90": st.get("time_to_last_token-p90"),
           "failed": st.get("failed_requests"),
           "wall_s": round(wall, 1)}
    LOG.info("[%s c=%d] total=%.0f tok/min  out=%.0f  fails=%s  ttlt_p50=%.2fs",
             label, c, in_tpm + out_tpm, out_tpm, st.get("failed_requests"),
             st.get("time_to_last_token-p50") or 0)
    return row


# --------------------------------------------------------------------------
def main() -> None:
    cat = load_catalog(auto_refresh=True, offline_ok=False,
                       max_age_hours_prices=24, regions=[REGION])
    cfg = EXPERIMENTS["exp_8"]  # p6 DP=8, v0.25.1 image, BASE_FLAGS via extra_serve_flags
    LOG.info("A/B start: %s DP=%d region=%s", cfg.deployment.instance_type,
             cfg.deployment.data_parallel, REGION)
    inputs = load_inputs(20000)
    runner = DeploymentRunner(cfg, catalog=cat, hf_secret_name=HF_SECRET)
    rows: list = []
    ec2 = boto3.client("ec2", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    try:
        state = runner.launch()
        iid = state.instance_id
        LOG.info("LAUNCHED %s mode=%s ready=%.0fs ip=%s",
                 iid, state.capacity_mode, state.vllm_ready_wait_s or 0,
                 state.public_ip)
        # find the SG for self-heal
        sg_id = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0][
            "Instances"][0]["SecurityGroups"][0]["GroupId"]
        ensure_sg_access(ec2, sg_id)

        ep = VLLMEndpoint(base_url=state.base_url, api_key=state.api_key,
                          model_id=SERVED)
        sm = ep.invoke(VLLMEndpoint.create_payload(SYSTEM_PROMPT, inputs[0],
                                                   max_tokens=MAX_NEW_TOKENS))
        LOG.info("SMOKE ok in=%s out=%s", sm.num_tokens_input, sm.num_tokens_output)
        payloads = [VLLMEndpoint.create_payload(SYSTEM_PROMPT, x,
                                                max_tokens=MAX_NEW_TOKENS)
                    for x in inputs]

        # pre-pull the new image in the background while arm A runs
        ssm_run(ssm, iid, [f"nohup docker pull {IMG_NEW} > /var/log/ab-pull.log 2>&1 &",
                           "echo pull-started"], timeout_s=60)

        # ---- Arm A: baseline replication (v0.25.1, current flags) ----------
        rows.append(bench(ep, payloads, 800, "A_v0251_base", "armA")); save(rows)

        # ---- Arm B: new image, same flags ----------------------------------
        img_b = IMG_NEW
        ok = swap_container(ssm, ec2, iid, sg_id, state.base_url, state.api_key,
                            img_b, BASE_FLAGS, "B")
        if not ok:
            LOG.warning("cu129 image failed; falling back to %s", IMG_NEW_FALLBACK)
            img_b = IMG_NEW_FALLBACK
            ok = swap_container(ssm, ec2, iid, sg_id, state.base_url,
                                state.api_key, img_b, BASE_FLAGS, "B_fallback")
        if ok:
            rows.append(bench(ep, payloads, 800, f"B_{img_b.split(':')[1]}_base", "armB"))
            save(rows)

            # ---- Arm C: new image + tuned flags -----------------------------
            okc = swap_container(ssm, ec2, iid, sg_id, state.base_url,
                                 state.api_key, img_b, TUNED_FLAGS, "C")
            flags_c = TUNED_FLAGS
            if not okc:
                LOG.warning("tuned flags failed; retrying without --async-scheduling")
                flags_c = "--max-num-seqs 512 --max-num-batched-tokens 16384"
                okc = swap_container(ssm, ec2, iid, sg_id, state.base_url,
                                     state.api_key, img_b, flags_c, "C_degraded")
            if okc:
                rows.append(bench(ep, payloads, 800, f"C_tuned[{flags_c}]", "armC"))
                save(rows)

                # ---- Arm D: plateau re-sweep on the WINNER of B vs C --------
                # BUGFIX (audit 2026-07-27): the original version swept while
                # the arm-C container was still running, so D measured the
                # LOSER config. Now we pick the better of B/C by measured
                # tok/min and swap back to that config before sweeping.
                b_row = next((r for r in rows if r["arm"].startswith("B_")), None)
                c_row = next((r for r in rows if r["arm"].startswith("C_")), None)
                d_flags = flags_c
                if b_row and c_row and \
                        (b_row.get("total_tok_min") or 0) >= (c_row.get("total_tok_min") or 0):
                    d_flags = BASE_FLAGS
                    if not swap_container(ssm, ec2, iid, sg_id, state.base_url,
                                          state.api_key, img_b, d_flags, "D_winner"):
                        LOG.error("could not restore winner config for arm D; skipping sweep")
                        d_flags = None
                if d_flags is not None:
                    for c in (600, 1200, 1600):
                        rows.append(bench(ep, payloads, c, f"D_sweep[{d_flags}]",
                                          f"armD_c{c}"))
                        save(rows)
        else:
            rows.append({"arm": "B", "error": "new image never became ready"})
            save(rows)

        best = max((r for r in rows if r.get("total_tok_min")),
                   key=lambda r: r["total_tok_min"])
        base = next((r for r in rows if r["arm"].startswith("A_")), None)
        if base and base.get("total_tok_min"):
            gain = best["total_tok_min"] / base["total_tok_min"]
            rows.append({"SUMMARY": {"baseline": base["total_tok_min"],
                                     "best": best["total_tok_min"],
                                     "best_arm": best["arm"], "best_c": best["c"],
                                     "gain_x": round(gain, 3)}})
            save(rows)
            LOG.info("SUMMARY: best=%s c=%s -> %.2fx over baseline",
                     best["arm"], best["c"], gain)
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
