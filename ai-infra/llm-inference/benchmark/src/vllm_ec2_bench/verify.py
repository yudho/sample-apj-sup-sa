"""Per-tier verification gates for a benchmark run.

A throughput number is only worth quoting if you can show the run that produced
it was healthy. These helpers are model- and GPU-agnostic checks that turn a
plausible-looking tier into a *verified* one, and they exist because each of
them caught a real measurement error in this repo's own results:

* :func:`check_completeness` — LLMeter reports ``failed_requests=0`` even when
  clients time out and are silently dropped, because a dropped client returns
  an empty list rather than an error. One run lost 9% of its responses (36,394
  of 40,000) while reporting a clean sweep. Gate on responses actually on disk.

* :func:`cross_check_throughput` — compare LLMeter's own stats against
  wall-clock and against a recount from disk. Divergence means the reported
  window and the real one disagree; the usual cause is a straggler tail
  (dense work finishes early, a handful of slow requests stretch the window,
  and naive tok/min understates the achieved rate).

* :func:`scrape_vllm_metrics` — vLLM's ``usage.cached_tokens`` is not populated
  even with ``--enable-prefix-caching``, so the only way to see cache behaviour
  is the Prometheus endpoint. It also exposes the preemption counter, which is
  the single most important health signal at high concurrency: a tier with
  hundreds of preemptions is the engine evicting running sequences and
  recomputing their prefill later, and its throughput is neither stable nor
  reproducible no matter how good the headline number looks.

Nothing here is specific to one model or GPU family; the thresholds are
deliberately conservative defaults you can tighten per run.
"""
from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Counters worth pulling from vLLM's /metrics. Summed across data-parallel
# replicas, since each DP engine exports its own series.
_METRIC_KEYS = (
    "prefix_cache_queries_total",
    "prefix_cache_hits_total",
    "num_preemptions_total",
    "prompt_tokens_total",
    "generation_tokens_total",
)

# Defaults chosen from observed failures: a healthy tier in this harness lands
# at 100% completeness and <2% timing divergence, so 98%/5% flags real trouble
# without tripping on noise.
DEFAULT_MIN_COMPLETENESS = 0.98
DEFAULT_MAX_DIVERGENCE = 0.05

# Below this request count the divergence check is not meaningful: the
# first-to-last-request window covers only N-1 of N request durations, a ~1/N
# error that swamps the threshold at small N (27% at N=3). Tiers smaller than
# this are latency probes; their divergence is reported but not gated.
MIN_REQUESTS_FOR_DIVERGENCE = 30

# Minimum fraction of successful responses that must carry a DISTINCT prompt.
# UniquePayloadEndpoint should deliver 100%; anything materially below that means
# payloads were replayed and prefix caching turned the repeats into near-free
# cache hits. Set at 0.95 rather than 1.0 to tolerate genuinely duplicated inputs
# in a caller-supplied corpus.
DEFAULT_MIN_UNIQUENESS = 0.95


@dataclass
class TierVerdict:
    """Outcome of verifying one concurrency tier."""

    concurrency: int
    valid: bool
    reasons: list[str] = field(default_factory=list)
    completeness: float | None = None
    divergence: float | None = None
    preemptions: float | None = None
    prefix_cache_hit_rate: float | None = None
    distinct_prompts: int | None = None
    successful_responses: int | None = None
    failed_responses: int | None = None
    uniqueness: float | None = None

    def as_dict(self) -> dict:
        return {
            "concurrency": self.concurrency,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "completeness": self.completeness,
            "divergence": self.divergence,
            "preemptions": self.preemptions,
            "prefix_cache_hit_rate": self.prefix_cache_hit_rate,
            "distinct_prompts": self.distinct_prompts,
            "successful_responses": self.successful_responses,
            "failed_responses": self.failed_responses,
            "uniqueness": self.uniqueness,
        }


# -----------------------------------------------------------------------------
# Response-set inspection
# -----------------------------------------------------------------------------
def count_responses(output_dir: Path) -> tuple[int, int, int]:
    """Count SUCCESSFUL responses, distinct prompts, and failures on disk.

    Returns ``(n_successful, n_distinct_prompts, n_failed)``.

    A record is successful only if it carries no ``error`` **and** has a non-null
    ``num_tokens_output``. That distinction matters: LLMeter writes a record for
    every attempt, including ones that only say ``{"error": "Request timed
    out."}`` with null text and null token counts. Counting those as delivered
    responses inverts the completeness gate — an observed tier wrote 640
    timed-out records plus 64 real ones and would have reported 1100%
    completeness instead of failing.

    A distinct-prompt count far below the successful count means payloads were
    replayed — see :class:`~vllm_ec2_bench.endpoint.UniquePayloadEndpoint`.
    Only successful records contribute prompts, so a replay verdict is never
    based on requests the server never answered.

    Missing or unreadable files count as zero rather than raising, so a
    verification pass never masks the underlying run error.
    """
    n_successful = 0
    n_failed = 0
    prompts: set[str] = set()
    for path in sorted(Path(output_dir).rglob("responses*.jsonl")):
        try:
            with path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        # Unparseable line: real work may have happened but we
                        # cannot confirm it, so count it against the run.
                        n_failed += 1
                        continue
                    if rec.get("error") is not None or rec.get("num_tokens_output") is None:
                        n_failed += 1
                        continue
                    n_successful += 1
                    # LLMeter's InvocationResponse stores the request it sent as
                    # ``input_payload`` (and a flattened ``input_prompt``). Read
                    # the user message from the payload, falling back to the
                    # flattened form, so a distinct-prompt count is always
                    # available to detect payload replay.
                    payload = rec.get("input_payload") or rec.get("payload") or {}
                    found = False
                    for msg in payload.get("messages", []) or []:
                        if msg.get("role") == "user":
                            prompts.add(str(msg.get("content"))[:512])
                            found = True
                    if not found and rec.get("input_prompt"):
                        # The flattened form concatenates system + user, so trim
                        # from the END where the per-request note actually differs.
                        prompts.add(str(rec["input_prompt"])[-512:])
        except OSError:
            continue
    return n_successful, len(prompts), n_failed


def check_completeness(
    n_responses: int,
    n_expected: int,
    *,
    minimum: float = DEFAULT_MIN_COMPLETENESS,
) -> tuple[bool, float]:
    """Return ``(ok, ratio)`` for responses actually captured vs expected.

    ``n_expected`` is the tier's request budget (usually ``c * K``). Guards
    against a zero budget so a misconfigured tier fails loudly instead of
    dividing by zero.
    """
    if n_expected <= 0:
        return False, 0.0
    ratio = n_responses / n_expected
    return ratio >= minimum, ratio


# -----------------------------------------------------------------------------
# Authoritative rate computation
# -----------------------------------------------------------------------------
def rates_from_responses(output_dir: Path | str) -> dict:
    """Recompute throughput from the response records, ignoring LLMeter's rates.

    **Why not just use LLMeter's stats.** ``RunningStats.to_stats`` divides the
    two token rates by two *different* windows:

    * ``average_input_tokens_per_minute`` uses ``_send_window()`` — first to last
      *dispatch* timestamp (``utils.py`` lines 195-200, 215-226). That excludes
      the entire duration of the final wave of requests, so for K requests per
      client it covers only K-1 of K request durations.
    * ``average_output_tokens_per_minute`` uses ``end_time - first_send_time``
      (``utils.py`` lines 202-211), a genuinely different denominator.

    Summing the two — the natural thing to do to get total tok/min — therefore
    adds two rates measured over different periods. Measured on a real g7e c=32
    tier: LLMeter reported 117,620 tok/min against a true 107,531, an inflation
    of **9.4%**. The error grows with concurrency, which bends the throughput
    curve upward exactly where the headline figure is taken and understates
    cost per token.

    This function instead uses one window for everything — first dispatch to
    last completion — and counts only successful responses. Returns a dict with
    ``total_tokens_per_min``, ``input_tokens_per_min``,
    ``output_tokens_per_min``, ``requests_per_min``, ``window_s``,
    ``n_successful``, ``total_input_tokens``, ``total_output_tokens``, and
    ``mean_input_tokens`` / ``mean_output_tokens``. Empty on no usable records.
    """
    n = 0
    t_in = 0
    t_out = 0
    starts: list[datetime] = []
    ends: list[datetime] = []
    for path in sorted(Path(output_dir).rglob("responses*.jsonl")):
        try:
            with path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("error") is not None:
                        continue
                    n_out = rec.get("num_tokens_output")
                    n_inp = rec.get("num_tokens_input")
                    if n_out is None or n_inp is None:
                        continue
                    n += 1
                    t_in += n_inp
                    t_out += n_out
                    rt = rec.get("request_time")
                    ttlt = rec.get("time_to_last_token")
                    if rt is None:
                        continue
                    try:
                        start = datetime.fromisoformat(str(rt))
                    except (TypeError, ValueError):
                        continue
                    # Normalise to UTC-aware. A file mixing naive and aware
                    # timestamps otherwise raises TypeError on comparison
                    # ("can't compare offset-naive and offset-aware datetimes"),
                    # which would abort the analysis of a run that already cost
                    # GPU time. Naive values are treated as UTC, matching how
                    # LLMeter stamps them.
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    else:
                        start = start.astimezone(timezone.utc)
                    starts.append(start)
                    if ttlt is not None:
                        with contextlib.suppress(TypeError, ValueError, OverflowError):
                            ends.append(start + timedelta(seconds=float(ttlt)))
        except OSError:
            continue

    if not n or not starts:
        return {}
    # One window for every rate: first dispatch to last completion. This is the
    # period during which the server was actually doing this tier's work.
    last = max(ends) if ends else max(starts)
    window = (last - min(starts)).total_seconds()
    if window <= 0:
        return {}
    return {
        "window_s": window,
        "n_successful": n,
        "total_input_tokens": t_in,
        "total_output_tokens": t_out,
        "mean_input_tokens": t_in / n,
        "mean_output_tokens": t_out / n,
        "input_tokens_per_min": t_in * 60 / window,
        "output_tokens_per_min": t_out * 60 / window,
        "total_tokens_per_min": (t_in + t_out) * 60 / window,
        "requests_per_min": n * 60 / window,
    }


# -----------------------------------------------------------------------------
# Throughput cross-check
# -----------------------------------------------------------------------------
def cross_check_throughput(
    *,
    stats_tokens_per_min: float,
    total_tokens: float,
    wall_clock_s: float,
    maximum_divergence: float = DEFAULT_MAX_DIVERGENCE,
) -> tuple[bool, float, float]:
    """Compare LLMeter's reported rate against a wall-clock recomputation.

    Returns ``(ok, divergence, wall_clock_tokens_per_min)`` where divergence is
    the absolute relative gap between the two rates. Both figures are legitimate
    measurements of different windows — LLMeter times its own request window,
    wall-clock includes tier setup and the straggler tail — so a gap is not
    automatically an error. It *is* a signal that you must decide which window
    you mean before quoting a number.
    """
    if wall_clock_s <= 0 or stats_tokens_per_min <= 0:
        return False, float("inf"), 0.0
    wall_rate = total_tokens / wall_clock_s * 60.0
    divergence = abs(wall_rate - stats_tokens_per_min) / stats_tokens_per_min
    return divergence <= maximum_divergence, divergence, wall_rate


# -----------------------------------------------------------------------------
# vLLM /metrics
# -----------------------------------------------------------------------------
def scrape_vllm_metrics(base_url: str, api_key: str, *, timeout_s: float = 15.0) -> dict:
    """Sum the interesting counters from vLLM's Prometheus ``/metrics``.

    ``base_url`` is the OpenAI-style base (``http://<ip>:8000/v1``); the
    ``/v1`` suffix is stripped. Values are summed across data-parallel
    replicas. Network problems are reported as an ``"error"`` key rather than
    raised, so a scrape failure degrades a verdict instead of aborting a run
    that has already cost GPU time.

    Derived keys are added when computable: ``prefix_cache_hit_rate``.
    """
    out: dict = {}
    url = base_url.replace("/v1", "").rstrip("/") + "/metrics"
    if not url.startswith(("http://", "https://")):  # pragma: no cover
        return {"error": f"unexpected scheme in metrics URL: {url!r}"}
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310
            text = resp.read().decode()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": str(exc)[:200]}

    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for key in _METRIC_KEYS:
            if key in line:
                try:
                    out[key] = out.get(key, 0.0) + float(line.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    # A malformed or truncated sample line is not worth failing
                    # a whole verification pass over; the counters we can parse
                    # are still useful and a missing key surfaces downstream.
                    continue

    queries = out.get("prefix_cache_queries_total")
    hits = out.get("prefix_cache_hits_total")
    if queries:
        out["prefix_cache_hit_rate"] = hits / queries if hits is not None else 0.0
    return out


# -----------------------------------------------------------------------------
# One-call tier verdict
# -----------------------------------------------------------------------------
def verify_tier(
    *,
    concurrency: int,
    output_dir: Path | str,
    n_expected: int,
    stats_tokens_per_min: float,
    total_tokens: float,
    wall_clock_s: float,
    metrics_before: dict | None = None,
    metrics_after: dict | None = None,
    min_completeness: float = DEFAULT_MIN_COMPLETENESS,
    max_divergence: float = DEFAULT_MAX_DIVERGENCE,
    max_preemptions: int = 0,
    min_uniqueness: float = DEFAULT_MIN_UNIQUENESS,
) -> TierVerdict:
    """Run every gate over one tier and return a single verdict.

    ``metrics_before``/``metrics_after`` are :func:`scrape_vllm_metrics` results
    bracketing the tier; the deltas are what matter, since the counters are
    cumulative for the life of the server.

    ``max_preemptions`` defaults to 0 — the strictest setting, and the right one
    when you intend to quote the result. Raise it only if you are deliberately
    characterising the engine past its stable concurrency ceiling.

    ``min_uniqueness`` guards against payload replay. It is a hard gate rather
    than a note because replay is silent, inflates throughput via prefix-cache
    hits, and has already produced two unusable measurements.
    """
    verdict = TierVerdict(concurrency=concurrency, valid=True)

    n_responses, distinct, n_failed = count_responses(Path(output_dir))
    verdict.distinct_prompts = distinct
    verdict.successful_responses = n_responses
    verdict.failed_responses = n_failed
    ok, ratio = check_completeness(n_responses, n_expected, minimum=min_completeness)
    verdict.completeness = ratio
    if not ok:
        verdict.valid = False
        verdict.reasons.append(
            f"completeness {ratio:.1%} < {min_completeness:.0%} "
            f"({n_responses} successful of {n_expected} expected"
            + (f", {n_failed} failed/timed-out" if n_failed else "")
            + ")"
        )
    elif n_failed:
        verdict.reasons.append(
            f"{n_failed} failed/timed-out record(s) present but completeness "
            f"still met ({n_responses}/{n_expected})"
        )

    # Payload replay: the whole point of UniquePayloadEndpoint. If distinct
    # prompts fall well below successful responses, prefix caching served
    # repeats and the throughput is not a real-workload number.
    if n_responses:
        uniqueness = distinct / n_responses
        verdict.uniqueness = uniqueness
        if uniqueness < min_uniqueness:
            verdict.valid = False
            verdict.reasons.append(
                f"payload uniqueness {uniqueness:.1%} < {min_uniqueness:.0%} "
                f"({distinct} distinct prompts across {n_responses} responses) — "
                "replayed payloads inflate throughput via prefix-cache hits"
            )

    ok, divergence, _ = cross_check_throughput(
        stats_tokens_per_min=stats_tokens_per_min,
        total_tokens=total_tokens,
        wall_clock_s=wall_clock_s,
        maximum_divergence=max_divergence,
    )
    verdict.divergence = divergence
    # The cross-check compares two rates over the first-request-to-last-request
    # window. That window spans only N-1 of a tier's N request durations, so at
    # tiny N it under-measures by ~1/N no matter how healthy the run is: a
    # 3-request tier diverges 27% by construction. Only apply the gate once N is
    # large enough for that edge effect to be negligible.
    if n_expected < MIN_REQUESTS_FOR_DIVERGENCE:
        verdict.reasons.append(
            f"divergence {divergence:.1%} not gated: only {n_expected} requests "
            f"(<{MIN_REQUESTS_FOR_DIVERGENCE}); this tier is a latency probe, "
            "not a throughput measurement"
        )
    elif not ok:
        verdict.valid = False
        verdict.reasons.append(
            f"timing divergence {divergence:.1%} > {max_divergence:.0%} "
            "(stats window and wall-clock disagree; check for a straggler tail)"
        )

    if metrics_before is not None and metrics_after is not None:
        before_p = metrics_before.get("num_preemptions_total")
        after_p = metrics_after.get("num_preemptions_total")
        if before_p is not None and after_p is not None:
            preemptions = after_p - before_p
            verdict.preemptions = preemptions
            if preemptions > max_preemptions:
                verdict.valid = False
                verdict.reasons.append(
                    f"{preemptions:.0f} preemptions > {max_preemptions} — the "
                    "engine evicted running sequences; this tier is past its "
                    "stable concurrency ceiling and is not reproducible"
                )
        q_before = metrics_before.get("prefix_cache_queries_total", 0.0) or 0.0
        q_after = metrics_after.get("prefix_cache_queries_total", 0.0) or 0.0
        h_before = metrics_before.get("prefix_cache_hits_total", 0.0) or 0.0
        h_after = metrics_after.get("prefix_cache_hits_total", 0.0) or 0.0
        if q_after > q_before:
            verdict.prefix_cache_hit_rate = (h_after - h_before) / (q_after - q_before)

    return verdict


__all__ = [
    "TierVerdict",
    "count_responses",
    "check_completeness",
    "cross_check_throughput",
    "rates_from_responses",
    "scrape_vllm_metrics",
    "verify_tier",
    "DEFAULT_MIN_COMPLETENESS",
    "DEFAULT_MAX_DIVERGENCE",
    "DEFAULT_MIN_UNIQUENESS",
    "MIN_REQUESTS_FOR_DIVERGENCE",
]
