"""Phase 3: run the MedGemma-27B cross-GPU benchmark NOTEBOOK headless.

Per Yudho's requirement, Phase 3 MUST use the notebook (not a bespoke
orchestrator). This wrapper:

  1. Copies the committed notebook to a run-specific file (never mutates the
     tracked .ipynb).
  2. Patches ONLY the interactive HF-token cell: the secret already exists in
     Secrets Manager (us-west-2), and instances fetch it by NAME at boot via
     their instance role — the notebook never sends the token to the instance.
     So we skip the paste/assert/upsert and just verify the secret is present.
  3. Executes the notebook end-to-end with nbconvert (ExecutePreprocessor) from
     the benchmark/ dir so `models.*` / `src` imports resolve. Each experiment
     cell already self-tears-down (try/finally), so at most one GPU is alive at
     a time; an in-guest `shutdown -h +90` is the final backstop.
  4. Copies the executed notebook + outputs/ to the durable OneDrive folder.
  5. ALWAYS runs an emergency teardown sweep (all tagged instances, 3 regions)
     in finally, so nothing is left billing even if execution dies.

Run detached:
  AWS_PROFILE=yudho-aiml nohup ../.venv/bin/python holmusk_phase3_run.py \
      >> "$RESDIR/phase3-run.log" 2>&1 < /dev/null & disown
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import time
import traceback
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
LOG = logging.getLogger("phase3")

BENCH_DIR = Path(__file__).resolve().parent
NB_SRC = BENCH_DIR / "models" / "medgemma_27b" / "medgemma-27b-vllm-ec2-benchmark.ipynb"
NB_RUN = BENCH_DIR / "models" / "medgemma_27b" / "medgemma-27b-vllm-ec2-benchmark.EXECUTED.ipynb"
OUT_DIR = Path(
    "/Users/diponego/Library/CloudStorage/OneDrive-amazon.com/Holmusk/"
    "cross-gpu-benchmark-2026-07-21"
)
REGION = "us-west-2"
ALT_REGIONS = ["us-east-2", "us-east-1"]
HF_SECRET_NAME = "medgemma-27b-benchmark/hf-token"
PROJECT_TAG = "medgemma-27b-benchmark"
# Multi-hour ceiling per CELL. The p6 c=800 tier + 4 sequential GPU launches
# (each ~15 min warmup + spot wait) is the long pole; 4h/cell is generous.
CELL_TIMEOUT_S = 4 * 3600


def verify_hf_secret() -> None:
    import boto3

    sm = boto3.client("secretsmanager", region_name=REGION)
    meta = sm.describe_secret(SecretId=HF_SECRET_NAME)
    LOG.info("HF secret present: %s", meta["ARN"])


def patch_notebook() -> None:
    """Copy NB_SRC -> NB_RUN and neutralize the interactive HF-token cell."""
    nb = json.loads(NB_SRC.read_text())
    patched = 0
    pip_neutralized = 0
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        # Neutralize the `%pip install -U ...` cell: the venv already has the
        # exact validated deps (llmeter 0.1.12 — its min_requests semantics are
        # what the fixed-per-client request counts were validated against). A
        # mid-run `-U` upgrade could silently change that behavior.
        if src.lstrip().startswith("%pip"):
            cell["source"] = [
                "# [headless] skipped %pip install -U: venv already has the\n",
                "# validated dependency set; avoid a mid-run upgrade.\n",
                "print('deps: using pre-installed venv (pip install skipped)')\n",
            ]
            cell["outputs"] = []
            cell["execution_count"] = None
            pip_neutralized += 1
            continue
        if "upsert_hf_token(" in src and "assert HF_TOKEN" in src:
            cell["source"] = [
                "# [headless] HF token already in Secrets Manager; instances fetch\n",
                "# it by name at boot. Skip the interactive paste/assert/upsert.\n",
                "import boto3\n",
                f'_sm = boto3.client("secretsmanager", region_name=REGION)\n',
                '_arn = _sm.describe_secret(SecretId=HF_SECRET_NAME)["ARN"]\n',
                'print(f"HF token already stored in: {_arn}")\n',
                'HF_TOKEN = "(stored in Secrets Manager)"\n',
            ]
            cell["outputs"] = []
            cell["execution_count"] = None
            patched += 1
    if patched != 1:
        raise RuntimeError(f"expected to patch exactly 1 HF cell, patched {patched}")
    NB_RUN.write_text(json.dumps(nb, indent=1))
    LOG.info("patched notebook -> %s (HF cells=%d, pip neutralized=%d)",
             NB_RUN.name, patched, pip_neutralized)


def execute_notebook() -> bool:
    import nbformat
    from nbconvert.preprocessors import ExecutePreprocessor

    nb = nbformat.read(str(NB_RUN), as_version=4)
    # Use the holmusk-bench kernelspec: it pins the ABSOLUTE venv python
    # (the bare "python3" kernelspec resolves argv[0]="python" via PATH, which
    # is unreliable under nohup/detached and may miss llmeter/vllm_ec2_bench).
    ep = ExecutePreprocessor(timeout=CELL_TIMEOUT_S, kernel_name="holmusk-bench")
    ok = True
    t0 = time.time()
    try:
        # resources.metadata.path sets the execution CWD to benchmark/ so that
        # `from models...` and `sys.path` insertion in the notebook resolve.
        ep.preprocess(nb, {"metadata": {"path": str(BENCH_DIR)}})
        LOG.info("notebook executed OK in %.0fs", time.time() - t0)
    except Exception:
        ok = False
        LOG.error("notebook execution FAILED after %.0fs:\n%s",
                  time.time() - t0, traceback.format_exc())
    finally:
        nbformat.write(nb, str(NB_RUN))
        LOG.info("executed notebook saved -> %s", NB_RUN)
    return ok


def publish_results() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # executed notebook
    try:
        shutil.copy2(NB_RUN, OUT_DIR / NB_RUN.name)
    except Exception:
        LOG.error("copy notebook failed:\n%s", traceback.format_exc())
    # outputs/ (comparison_table.csv, per-exp llmeter dirs)
    out_local = BENCH_DIR / "outputs"
    if out_local.is_dir():
        dst = OUT_DIR / "phase3-outputs"
        try:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(out_local, dst)
            LOG.info("outputs copied -> %s", dst)
        except Exception:
            LOG.error("copy outputs failed:\n%s", traceback.format_exc())
    # surface the comparison table at top level for easy access
    csv = out_local / "comparison_table.csv"
    if csv.is_file():
        shutil.copy2(csv, OUT_DIR / "phase3-comparison_table.csv")
        LOG.info("comparison table -> %s", OUT_DIR / "phase3-comparison_table.csv")


def emergency_teardown() -> None:
    """Terminate any instance tagged Project=<tag> in all regions. Idempotent."""
    import boto3

    for r in [REGION, *ALT_REGIONS]:
        try:
            ec2 = boto3.client("ec2", region_name=r)
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Project", "Values": [PROJECT_TAG]},
                    {"Name": "instance-state-name",
                     "Values": ["pending", "running", "stopping", "stopped"]},
                ]
            )
            ids = [
                i["InstanceId"]
                for res in resp["Reservations"]
                for i in res["Instances"]
            ]
            if ids:
                ec2.terminate_instances(InstanceIds=ids)
                LOG.warning("[%s] emergency-terminated %s", r, ids)
            else:
                LOG.info("[%s] no tagged instances to terminate", r)
        except Exception:
            LOG.error("[%s] emergency teardown error:\n%s", r, traceback.format_exc())


def summarize_results() -> tuple[list[str], list[str]]:
    """Read durable outputs/<exp>/result.json files; log per-GPU status and
    rebuild comparison_table.csv from them if the notebook's own is missing.

    Returns (ok_exp_ids, failed_exp_ids). This is the source of truth for
    whether the RUN succeeded — a notebook that finishes with p6 done but g6e
    out-of-capacity is a SUCCESS, not a failure.
    """
    out_local = BENCH_DIR / "outputs"
    ok_ids: list[str] = []
    failed: list[str] = []
    records: list[dict] = []
    if out_local.is_dir():
        for rj in sorted(out_local.glob("*/result.json")):
            try:
                rec = json.loads(rj.read_text())
            except Exception:
                continue
            if rec.get("status") == "ok":
                ok_ids.append(rec["exp_id"])
                records.append(rec)
            elif rec.get("status") == "failed":
                failed.append(rec["exp_id"])

    LOG.info("RESULT SUMMARY: %d ok %s | %d failed %s",
             len(ok_ids), ok_ids, len(failed), failed)
    for rec in records:
        tiers = rec.get("per_tier", {})
        best = None
        for c, m in tiers.items():
            t = m.get("total_tok_min") or 0
            if best is None or t > best[1]:
                best = (c, t, m.get("cost_per_1m_total"))
        if best:
            LOG.info("  %s (%s, %s): peak %s tok/min @ c=%s, $%s/1M total  [%s]",
                     rec["exp_id"], rec["instance_type"], rec.get("gpu_model"),
                     f"{best[1]:,.0f}", best[0], best[2], rec.get("hourly_source"))

    # Fallback: if the notebook never wrote the CSV (e.g. analysis cell didn't
    # run), rebuild a per-tier table from the durable result.json files.
    csv = out_local / "comparison_table.csv"
    if records and not csv.is_file():
        try:
            import csv as _csv
            all_tiers = sorted({int(c) for r in records for c in r.get("per_tier", {})})
            cols = (["exp_id", "instance_type", "gpu_model", "tp", "dp",
                     "capacity_mode", "hourly_usd", "hourly_source"]
                    + [f"c={t} tok/min" for t in all_tiers]
                    + [f"c={t} $/1M" for t in all_tiers])
            with csv.open("w", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                for r in records:
                    row = {k: r.get(k) for k in cols if k in r}
                    for t in all_tiers:
                        m = r.get("per_tier", {}).get(str(t)) or r.get("per_tier", {}).get(t) or {}
                        row[f"c={t} tok/min"] = m.get("total_tok_min")
                        row[f"c={t} $/1M"] = m.get("cost_per_1m_total")
                    w.writerow(row)
            LOG.info("rebuilt comparison_table.csv from result.json files -> %s", csv)
        except Exception:
            LOG.error("CSV rebuild failed:\n%s", traceback.format_exc())
    return ok_ids, failed


def main() -> int:
    LOG.info("Phase 3 headless notebook run starting")
    LOG.info("  notebook: %s", NB_SRC)
    LOG.info("  outputs : %s", OUT_DIR)
    nb_ok = False
    ok_ids: list[str] = []
    failed: list[str] = []
    try:
        verify_hf_secret()
        patch_notebook()
        nb_ok = execute_notebook()
        # summarize BEFORE publish so a rebuilt CSV gets copied to OneDrive too
        ok_ids, failed = summarize_results()
        publish_results()
    except Exception:
        LOG.error("FATAL:\n%s", traceback.format_exc())
    finally:
        LOG.info("EMERGENCY TEARDOWN SWEEP (safety net)...")
        emergency_teardown()
    # Success verdict = p6 (the whole point) completed. PARTIAL if some GPUs ran
    # but not p6; FAILED if nothing produced results. A g6e capacity miss alone
    # is acceptable and does not make the run a failure.
    p6 = "exp_8 (p6) OK" if "exp_8" in ok_ids else "exp_8 (p6) MISSING"
    verdict = "SUCCESS" if "exp_8" in ok_ids else ("PARTIAL" if ok_ids else "FAILED")
    print(f"RESULT={verdict} | ok={ok_ids} failed={failed} | {p6} | nbconvert_ok={nb_ok}")
    return 0 if ok_ids else 1


if __name__ == "__main__":
    sys.exit(main())
