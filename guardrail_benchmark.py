#!/usr/bin/env python3
"""
Bedrock Guardrails performance/cost benchmark harness.

Purpose
-------
Sends synthetic Claude Code / Cowork-shaped payloads through Amazon Bedrock's
standalone ApplyGuardrail API and measures:
  - added latency per call, plus p50/p95/p99 across a run
  - actual cost, computed from the real per-policy "usage" units AWS returns
    in the API response (not estimated from character count)
  - throttling behavior under concurrency

It compares two evaluation strategies head-to-head, which is the core
question this benchmark exists to answer:
  - "naive"  : re-send the ENTIRE session context (system prompt + all prior
               turns) to the guardrail on every turn. This is what you get
               by default if you don't explicitly scope what gets evaluated.
  - "scoped" : only send the NEWEST turn to the guardrail each time, mirroring
               what GuardrailConverseContentBlock ("guardContent") does on
               the Converse API. The system prompt is evaluated once, up
               front, on the assumption that a static prompt only needs
               checking when it changes, not on every turn.

This measures the guardrail in isolation (ApplyGuardrail, not Converse), so
none of these numbers include model inference latency/cost - only the
guardrail overhead itself. That's deliberate: it isolates the thing this
benchmark is about.

Prerequisites
-------------
1. An existing Bedrock guardrail (see HOW_TO_RUN.md for how to create one)
   with its ID and a version (or 'DRAFT').
2. AWS credentials with bedrock:ApplyGuardrail permission, configured via
   the normal boto3 credential chain (env vars, ~/.aws/credentials, SSO, etc).
3. pip install -r requirements.txt   (boto3)

Usage
-----
python guardrail_benchmark.py \
    --guardrail-id abcd1234 \
    --guardrail-version 1 \
    --region us-east-1 \
    --system-prompt-chars 30000 \
    --turns 10 \
    --turn-chars 4000 \
    --iterations 5 \
    --concurrency 1 4 8 \
    --out-dir ./results

See HOW_TO_RUN.md for a full walkthrough, including how to create a test
guardrail and what IAM permissions you need.

A note on very large payloads: AWS's own long-context guidance for
ApplyGuardrail recommends chunking extremely large inputs into multiple
content blocks rather than one giant block. This script sends each
system-prompt/turn as a single content block, which is fine at the sizes
this research is concerned with (tens of thousands of characters). If you
push --system-prompt-chars much higher and hit size-related errors from the
API, that's the place to add chunking.
"""

import argparse
import concurrent.futures
import csv
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("This script requires boto3. Install with: pip install boto3", file=sys.stderr)
    raise

# ---------------------------------------------------------------------------
# Pricing (Bedrock Guardrails, on-demand, as published at
# https://aws.amazon.com/bedrock/pricing/ and checked against that page as
# part of this research in August 2026). VERIFY against the live pricing
# page before trusting cost numbers for budgeting - AWS has changed these
# before (an ~85% cut in Dec 2024) and may again.
# ---------------------------------------------------------------------------
PRICE_PER_1000_UNITS = {
    "contentPolicyUnits": 0.15,
    "topicPolicyUnits": 0.15,
    "sensitiveInformationPolicyUnits": 0.10,
    "sensitiveInformationPolicyFreeUnits": 0.0,  # regex-based custom entities: free
    "contextualGroundingPolicyUnits": 0.10,
    "wordPolicyUnits": 0.0,
    "automatedReasoningPolicyUnits": 0.17,
}
PRICE_PER_IMAGE = 0.00075  # contentPolicyImageUnits - priced per image, not per 1000


def compute_cost(usage: dict) -> float:
    """Compute $ cost of one ApplyGuardrail call from its returned usage block."""
    cost = 0.0
    for key, price in PRICE_PER_1000_UNITS.items():
        cost += usage.get(key, 0) / 1000 * price
    cost += usage.get("contentPolicyImageUnits", 0) * PRICE_PER_IMAGE
    return cost


# ---------------------------------------------------------------------------
# Synthetic payload generation - mimics a Claude Code / Cowork-shaped
# session: a large, mostly-static system prompt, plus conversation history
# that grows every turn (user message + tool output + assistant reply).
# Deterministic (seeded) so runs are comparable across scenarios/dates.
# ---------------------------------------------------------------------------
_WORDS = (
    "the quick brown fox jumps over lazy dog while agent reviews pull request "
    "and runs unit tests before committing changes to the repository branch "
    "function returns list of items after validating each field against schema "
    "user asked to refactor module and update documentation accordingly please "
    "read file write edit search grep tool call result output error traceback"
).split()


def _filler_text(chars: int, seed: int) -> str:
    """Deterministic filler text of approximately `chars` characters."""
    rng = random.Random(seed)
    out = []
    length = 0
    while length < chars:
        w = rng.choice(_WORDS)
        out.append(w)
        length += len(w) + 1
    return " ".join(out)[:chars]


def build_system_prompt(chars: int) -> str:
    return _filler_text(chars, seed=1)


def build_turn(turn_index: int, chars: int) -> str:
    # Simulate a user request + tool output + assistant reply for one turn.
    return f"[turn {turn_index}] " + _filler_text(chars, seed=1000 + turn_index)


@dataclass
class CallResult:
    scenario: str  # "naive" or "scoped"
    turn: int
    concurrency: int
    latency_ms: float
    chars_sent: int
    cost_usd: float
    action: str  # "NONE", "GUARDRAIL_INTERVENED", or "ERROR"
    throttled: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Guardrail client
# ---------------------------------------------------------------------------
class GuardrailBenchmarkClient:
    def __init__(self, guardrail_id: str, guardrail_version: str, region: str):
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.guardrail_id = guardrail_id
        self.guardrail_version = guardrail_version

    def apply(self, text: str, source: str = "INPUT"):
        """Call ApplyGuardrail once.

        Returns (latency_ms, usage_dict, action, throttled, error).
        """
        start = time.perf_counter()
        try:
            resp = self.client.apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
            latency_ms = (time.perf_counter() - start) * 1000
            usage = resp.get("usage", {})
            action = resp.get("action", "NONE")
            return latency_ms, usage, action, False, ""
        except ClientError as e:
            latency_ms = (time.perf_counter() - start) * 1000
            code = e.response.get("Error", {}).get("Code", "")
            throttled = "Throttl" in code
            return latency_ms, {}, "ERROR", throttled, str(code)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------
def run_session(client, scenario, system_prompt, turns, concurrency_label):
    """Run one full session (system prompt + N turns) in the given scenario mode."""
    results = []

    # The system prompt is checked once, up front, regardless of scenario -
    # this mirrors "evaluate the static system prompt once, not every turn,"
    # which is the whole point of the "scoped" strategy.
    latency_ms, usage, action, throttled, err = client.apply(system_prompt, source="INPUT")
    results.append(
        CallResult(
            scenario=scenario,
            turn=0,
            concurrency=concurrency_label,
            latency_ms=latency_ms,
            chars_sent=len(system_prompt),
            cost_usd=compute_cost(usage),
            action=action,
            throttled=throttled,
            error=err,
        )
    )

    accumulated = system_prompt
    for i, turn_text in enumerate(turns, start=1):
        if scenario == "naive":
            accumulated += " " + turn_text
            text_to_check = accumulated
        else:  # scoped
            text_to_check = turn_text

        latency_ms, usage, action, throttled, err = client.apply(text_to_check, source="INPUT")
        results.append(
            CallResult(
                scenario=scenario,
                turn=i,
                concurrency=concurrency_label,
                latency_ms=latency_ms,
                chars_sent=len(text_to_check),
                cost_usd=compute_cost(usage),
                action=action,
                throttled=throttled,
                error=err,
            )
        )
    return results


def run_benchmark(args):
    client = GuardrailBenchmarkClient(args.guardrail_id, args.guardrail_version, args.region)
    system_prompt = build_system_prompt(args.system_prompt_chars)
    turns = [build_turn(i, args.turn_chars) for i in range(1, args.turns + 1)]

    all_results = []
    for concurrency in args.concurrency:
        for scenario in ("naive", "scoped"):
            for iteration in range(args.iterations):
                # Run `concurrency` simultaneous sessions for this iteration.
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = [
                        pool.submit(run_session, client, scenario, system_prompt, turns, concurrency)
                        for _ in range(concurrency)
                    ]
                    for f in concurrent.futures.as_completed(futures):
                        all_results.extend(f.result())
                print(
                    f"  done: concurrency={concurrency} scenario={scenario} "
                    f"iteration={iteration + 1}/{args.iterations}",
                    file=sys.stderr,
                )
    return all_results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def percentile(data, pct):
    if not data:
        return float("nan")
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(data) - 1)
    if f == c:
        return data[f]
    return data[f] + (data[c] - data[f]) * (k - f)


def summarize(results):
    groups = {}
    for r in results:
        groups.setdefault((r.scenario, r.concurrency), []).append(r)

    print("\n=== Summary (guardrail-only latency; excludes model inference) ===")
    header = (
        f"{'scenario':<8} {'concurrency':<11} {'n':<6} {'p50 ms':<9} {'p95 ms':<9} "
        f"{'p99 ms':<9} {'avg chars':<10} {'total $':<10} {'throttled':<10}"
    )
    print(header)
    for (scenario, concurrency), rows in sorted(groups.items()):
        latencies = [r.latency_ms for r in rows if not r.error]
        chars = [r.chars_sent for r in rows]
        cost = sum(r.cost_usd for r in rows)
        throttled = sum(1 for r in rows if r.throttled)
        print(
            f"{scenario:<8} {concurrency:<11} {len(rows):<6} "
            f"{percentile(latencies, 50):<9.1f} {percentile(latencies, 95):<9.1f} "
            f"{percentile(latencies, 99):<9.1f} {statistics.mean(chars):<10.0f} "
            f"{cost:<10.4f} {throttled:<10}"
        )
    print(
        "\nRead this as: for the same session shape, does 'scoped' meaningfully beat "
        "'naive' on p95/p99 latency and total $? And at what concurrency does the "
        "'throttled' column start climbing?"
    )


def write_outputs(results, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "guardrail_benchmark_results.csv"
    json_path = out_dir / "guardrail_benchmark_results.json"

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    with open(json_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    print(f"\nRaw results written to:\n  {csv_path}\n  {json_path}")


# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--guardrail-id", required=True, help="Bedrock guardrail identifier")
    p.add_argument("--guardrail-version", default="DRAFT", help="Guardrail version, e.g. '1' or 'DRAFT'")
    p.add_argument("--region", default="us-east-1", help="AWS region")
    p.add_argument(
        "--system-prompt-chars",
        type=int,
        default=30000,
        help="Simulated system prompt size in characters (Claude Code/Cowork is roughly 30K)",
    )
    p.add_argument("--turns", type=int, default=10, help="Number of conversation turns to simulate")
    p.add_argument(
        "--turn-chars", type=int, default=4000, help="Characters added to the conversation per turn"
    )
    p.add_argument(
        "--iterations", type=int, default=5, help="Repetitions per scenario/concurrency combo"
    )
    p.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=[1],
        help="One or more concurrency levels to test, e.g. --concurrency 1 4 8",
    )
    p.add_argument(
        "--out-dir", type=Path, default=Path("./results"), help="Where to write CSV/JSON output"
    )
    return p.parse_args()


def main():
    args = parse_args()
    print(
        f"Running guardrail benchmark: guardrail={args.guardrail_id} region={args.region} "
        f"system_prompt_chars={args.system_prompt_chars} turns={args.turns} "
        f"turn_chars={args.turn_chars} iterations={args.iterations} concurrency={args.concurrency}",
        file=sys.stderr,
    )
    results = run_benchmark(args)
    if not results:
        print("No results collected.", file=sys.stderr)
        sys.exit(1)
    summarize(results)
    write_outputs(results, args.out_dir)


if __name__ == "__main__":
    main()
