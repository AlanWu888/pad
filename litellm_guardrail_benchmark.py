#!/usr/bin/env python3
"""
LiteLLM guardrail performance benchmark harness.

Purpose
-------
Companion to guardrail_benchmark.py, but hits your LIVE LiteLLM deployment
instead of calling Bedrock directly. This is the path production traffic
actually goes through, so it captures LiteLLM's own hook overhead plus
whatever guardrail is configured behind it - Presidio today, and Bedrock
too if that ever gets wired in alongside it.

It calls LiteLLM's standalone `/guardrails/apply_guardrail` endpoint, which
runs just the configured guardrail without invoking an LLM. That isolates
guardrail overhead the same way the Bedrock-direct script does, rather than
conflating it with model inference latency - see the "what this doesn't
cover" note at the bottom for why full /chat/completions timing is a
deliberately separate, not-yet-built thing.

Same comparison as guardrail_benchmark.py, run through LiteLLM instead of
directly against Bedrock:
  - "naive"  : re-send the FULL accumulated session context every turn -
               what you get by default if nothing scopes it down.
  - "scoped" : send only the NEWEST turn each time.

Prerequisites
-------------
1. Your LiteLLM proxy running and reachable (e.g. http://localhost:4000).
2. A guardrail already configured in LiteLLM's config.yaml and loaded -
   you're already running "presidio-pii-guard" or similarly named today;
   pass whatever your guardrail_name actually is via --guardrail-name.
3. A LiteLLM virtual key / API key with permission to call it.
4. pip install -r requirements.txt   (requests)

Usage
-----
python litellm_guardrail_benchmark.py \
    --base-url http://localhost:4000 \
    --api-key sk-1234 \
    --guardrail-name presidio-pii-guard \
    --system-prompt-chars 30000 \
    --turns 10 \
    --turn-chars 4000 \
    --iterations 5 \
    --concurrency 1 4 8 \
    --out-dir ./results

See HOW_TO_RUN.md for the equivalent Bedrock-direct walkthrough - the setup
here is the same shape, just pointed at your LiteLLM instance instead of AWS.
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
    import requests
except ImportError:
    print("This script requires requests. Install with: pip install requests", file=sys.stderr)
    raise

# ---------------------------------------------------------------------------
# Synthetic payload generation - identical approach to guardrail_benchmark.py
# so results are comparable across the two scripts: a large, mostly-static
# system prompt, plus conversation history that grows every turn.
# ---------------------------------------------------------------------------
_WORDS = (
    "the quick brown fox jumps over lazy dog while agent reviews pull request "
    "and runs unit tests before committing changes to the repository branch "
    "function returns list of items after validating each field against schema "
    "user asked to refactor module and update documentation accordingly please "
    "read file write edit search grep tool call result output error traceback "
    "my name is John Doe email john.doe@example.com phone 555-123-4567"
).split()


def _filler_text(chars: int, seed: int) -> str:
    """Deterministic filler text of approximately `chars` characters.
    Includes a few PII-shaped tokens (name/email/phone) so Presidio has
    something realistic to actually detect, rather than empty passes."""
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
    return f"[turn {turn_index}] " + _filler_text(chars, seed=1000 + turn_index)


@dataclass
class CallResult:
    scenario: str  # "naive" or "scoped"
    turn: int
    concurrency: int
    latency_ms: float
    chars_sent: int
    status_code: int
    action: str  # "OK", "BLOCKED", or "ERROR"
    throttled: bool
    error: str = ""


# ---------------------------------------------------------------------------
# LiteLLM guardrail client
# ---------------------------------------------------------------------------
class LiteLLMGuardrailClient:
    def __init__(self, base_url: str, api_key: str, guardrail_name: str, language: str = "en"):
        self.url = base_url.rstrip("/") + "/guardrails/apply_guardrail"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        self.guardrail_name = guardrail_name
        self.language = language

    def apply(self, text: str):
        """Call /guardrails/apply_guardrail once.

        Returns (latency_ms, status_code, action, throttled, error).
        """
        body = {
            "guardrail_name": self.guardrail_name,
            "text": text,
            "language": self.language,
        }
        start = time.perf_counter()
        try:
            resp = requests.post(self.url, headers=self.headers, json=body, timeout=30)
            latency_ms = (time.perf_counter() - start) * 1000
            if resp.status_code == 200:
                return latency_ms, resp.status_code, "OK", False, ""
            if resp.status_code == 429:
                return latency_ms, resp.status_code, "ERROR", True, "rate limited"
            # Non-200: guardrail may have blocked the content (e.g. Bedrock
            # deny, or a Presidio BLOCK-mode entity), or something else
            # failed - either way, record it rather than guessing.
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except ValueError:
                detail = resp.text[:200]
            action = "BLOCKED" if "block" in str(detail).lower() else "ERROR"
            return latency_ms, resp.status_code, action, False, str(detail)
        except requests.exceptions.RequestException as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return latency_ms, 0, "ERROR", False, str(e)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------
def run_session(client, scenario, system_prompt, turns, concurrency_label):
    """Run one full session (system prompt + N turns) in the given scenario mode."""
    results = []

    latency_ms, status_code, action, throttled, err = client.apply(system_prompt)
    results.append(
        CallResult(
            scenario=scenario,
            turn=0,
            concurrency=concurrency_label,
            latency_ms=latency_ms,
            chars_sent=len(system_prompt),
            status_code=status_code,
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

        latency_ms, status_code, action, throttled, err = client.apply(text_to_check)
        results.append(
            CallResult(
                scenario=scenario,
                turn=i,
                concurrency=concurrency_label,
                latency_ms=latency_ms,
                chars_sent=len(text_to_check),
                status_code=status_code,
                action=action,
                throttled=throttled,
                error=err,
            )
        )
    return results


def run_benchmark(args):
    client = LiteLLMGuardrailClient(args.base_url, args.api_key, args.guardrail_name, args.language)
    system_prompt = build_system_prompt(args.system_prompt_chars)
    turns = [build_turn(i, args.turn_chars) for i in range(1, args.turns + 1)]

    all_results = []
    for concurrency in args.concurrency:
        for scenario in ("naive", "scoped"):
            for iteration in range(args.iterations):
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

    print("\n=== Summary (LiteLLM -> guardrail round trip; excludes model inference) ===")
    header = (
        f"{'scenario':<8} {'concurrency':<11} {'n':<6} {'p50 ms':<9} {'p95 ms':<9} "
        f"{'p99 ms':<9} {'avg chars':<10} {'blocked':<9} {'errors':<8} {'throttled':<10}"
    )
    print(header)
    for (scenario, concurrency), rows in sorted(groups.items()):
        latencies = [r.latency_ms for r in rows if r.action != "ERROR" or r.throttled]
        chars = [r.chars_sent for r in rows]
        blocked = sum(1 for r in rows if r.action == "BLOCKED")
        errors = sum(1 for r in rows if r.action == "ERROR" and not r.throttled)
        throttled = sum(1 for r in rows if r.throttled)
        print(
            f"{scenario:<8} {concurrency:<11} {len(rows):<6} "
            f"{percentile(latencies, 50):<9.1f} {percentile(latencies, 95):<9.1f} "
            f"{percentile(latencies, 99):<9.1f} {statistics.mean(chars):<10.0f} "
            f"{blocked:<9} {errors:<8} {throttled:<10}"
        )
    print(
        "\nRead this as: does 'scoped' meaningfully beat 'naive' on p95/p99 latency at the "
        "same session shape? And at what concurrency do errors/throttling start climbing - "
        "that's your real Presidio+LiteLLM capacity ceiling, not a published AWS quota."
    )


def write_outputs(results, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "litellm_guardrail_benchmark_results.csv"
    json_path = out_dir / "litellm_guardrail_benchmark_results.json"

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
    p.add_argument("--base-url", required=True, help="Your LiteLLM proxy base URL, e.g. http://localhost:4000")
    p.add_argument("--api-key", required=True, help="LiteLLM virtual key / API key")
    p.add_argument(
        "--guardrail-name",
        required=True,
        help="The guardrail_name configured in your LiteLLM config.yaml (e.g. 'presidio-pii-guard')",
    )
    p.add_argument("--language", default="en", help="Language code passed to the guardrail")
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
        f"Running LiteLLM guardrail benchmark: base_url={args.base_url} "
        f"guardrail_name={args.guardrail_name} system_prompt_chars={args.system_prompt_chars} "
        f"turns={args.turns} turn_chars={args.turn_chars} iterations={args.iterations} "
        f"concurrency={args.concurrency}",
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
