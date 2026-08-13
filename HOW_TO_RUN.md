# How to run the guardrail benchmarks

There are two scripts here, for two different questions:

- **`litellm_guardrail_benchmark.py`** — hits your **live LiteLLM deployment**, which is what production traffic actually goes through. Since LiteLLM is already calling Presidio for you today, **start here** — this measures your real, currently-deployed stack.
- **`guardrail_benchmark.py`** — hits **Bedrock's `ApplyGuardrail` API directly**, bypassing LiteLLM entirely. Useful as a reference point for Bedrock's overhead in isolation (e.g. if you're evaluating adding it behind LiteLLM later), but it is *not* measuring your production path.

Both produce the same shape of output (naive vs. scoped comparison, latency percentiles, CSV/JSON) so results are comparable side by side.

## Part A: `litellm_guardrail_benchmark.py` (your live stack — start here)

### 1. Prerequisites

- Python 3.9+, `pip install -r requirements.txt` (installs `requests` and `boto3`)
- Your LiteLLM proxy already running and reachable (you said it's already deployed — just need its base URL, e.g. `http://localhost:4000` or wherever it's hosted).
- The exact `guardrail_name` string as configured in your LiteLLM `config.yaml`'s `guardrails:` section for the Presidio hook (e.g. `presidio-pii-guard` — check your actual config for the real name).
- A LiteLLM virtual key / API key that's allowed to call it.

### 2. Run it

Start small first:

```bash
python litellm_guardrail_benchmark.py \
  --base-url http://localhost:4000 \
  --api-key <your-litellm-key> \
  --guardrail-name <your-guardrail-name> \
  --system-prompt-chars 30000 \
  --turns 5 \
  --turn-chars 4000 \
  --iterations 3 \
  --concurrency 1 \
  --out-dir ./results
```

Once that runs cleanly, scale up concurrency to find where your actual Presidio+LiteLLM deployment starts throwing errors or slowing down — that's your real capacity ceiling (there's no AWS-style published quota for a self-hosted stack; you have to find it empirically):

```bash
python litellm_guardrail_benchmark.py \
  --base-url http://localhost:4000 \
  --api-key <your-litellm-key> \
  --guardrail-name <your-guardrail-name> \
  --system-prompt-chars 30000 \
  --turns 20 \
  --turn-chars 5000 \
  --iterations 10 \
  --concurrency 1 4 8 16 32 \
  --out-dir ./results
```

Read the output the same way as described in Part B below (scenario comparison, percentiles) — the columns differ slightly (`blocked`/`errors`/`throttled` instead of a `$` cost column, since Presidio has no per-call fee; the thing worth watching here is where those columns start climbing under concurrency).

### 3. If you also want to test Bedrock's guardrails through LiteLLM specifically

If/when a Bedrock guardrail gets added to your LiteLLM config (via `litellm_params.guardrail: bedrock`), this same script works unchanged — just point `--guardrail-name` at that guardrail's configured name instead. That gets you a same-methodology comparison of Bedrock-via-LiteLLM vs. Presidio-via-LiteLLM vs. Bedrock-direct (Part B), all measured the same way.

---

## Part B: `guardrail_benchmark.py` (Bedrock direct — reference/comparison only)

This walks through everything needed to actually execute `guardrail_benchmark.py` and get real numbers, end to end.

## 1. Prerequisites

- Python 3.9+
- An AWS account with access to Amazon Bedrock Guardrails, in a region where Bedrock is available (e.g. `us-east-1` or `us-west-2` — those two also currently have the higher 50 RPS / 200 TUPS quota per the research doc).
- AWS credentials available to boto3 (env vars, `~/.aws/credentials` profile, or SSO) with permission to call `bedrock:ApplyGuardrail` (see IAM policy below).

Install the one dependency:

```bash
pip install -r requirements.txt
```

## 2. Create a test guardrail

The script evaluates against an *existing* guardrail — it doesn't create one for you, since guardrail policy configuration (which content filters, PII entities, denied topics) is a real decision your team should make deliberately, not something a benchmark script should default silently.

Easiest path: AWS Console → Amazon Bedrock → Guardrails → Create guardrail.
- Give it a name (e.g. `perf-test-guardrail`).
- Enable the policies you actually intend to use in production (at minimum, content filters + PII/sensitive-info filters, since those are the two the cost/scaling analysis was built around).
- Save, then create a version (or just use `DRAFT` for testing — note `DRAFT` still bills normally).
- Note the **Guardrail ID** and **Guardrail Version** shown on the guardrail's detail page — you'll pass both to the script.

Equivalent via AWS CLI, if you'd rather script the setup too:

```bash
aws bedrock create-guardrail \
  --name perf-test-guardrail \
  --blocked-input-messaging "blocked" \
  --blocked-outputs-messaging "blocked" \
  --content-policy-config file://content-policy.json \
  --sensitive-information-policy-config file://pii-policy.json \
  --region us-east-1
```

(See the [CreateGuardrail API reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateGuardrail.html) for the JSON shape of those policy config files.)

## 3. IAM permission needed

The credentials the script runs under need at minimum:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:ApplyGuardrail",
      "Resource": "arn:aws:bedrock:*:*:guardrail/*"
    }
  ]
}
```

## 4. Run it

Start small to confirm everything's wired up correctly — one concurrency level, few iterations:

```bash
python guardrail_benchmark.py \
  --guardrail-id <your-guardrail-id> \
  --guardrail-version DRAFT \
  --region us-east-1 \
  --system-prompt-chars 30000 \
  --turns 5 \
  --turn-chars 4000 \
  --iterations 3 \
  --concurrency 1 \
  --out-dir ./results
```

If that runs cleanly and prints a summary table, scale it up to something closer to what the research doc's test matrix recommends:

```bash
python guardrail_benchmark.py \
  --guardrail-id <your-guardrail-id> \
  --guardrail-version DRAFT \
  --region us-east-1 \
  --system-prompt-chars 30000 \
  --turns 20 \
  --turn-chars 5000 \
  --iterations 10 \
  --concurrency 1 4 8 16 \
  --out-dir ./results
```

This runs both the `naive` (full context re-sent every turn) and `scoped` (only the newest turn sent) strategies automatically, at every concurrency level you list, and prints a single comparison table at the end. Raw per-call data also gets written to `results/guardrail_benchmark_results.csv` and `.json` for further analysis (pivot tables, charts, whatever your dev lead wants to see it as).

## 5. What "good" output looks like

The printed summary table has one row per (scenario, concurrency) combination:

```
scenario concurrency n      p50 ms    p95 ms    p99 ms    avg chars  total $     throttled
naive    1           21     420.3     680.1     712.4     94500      0.0850      0
scoped   1           21     180.2     240.5     255.0     14200      0.0210      0
naive    8           168    610.7     1450.2    2100.8    94500      0.6800      12
scoped   8           168    195.4     260.1     290.3     14200      0.1680      0
```

(Numbers above are illustrative, not real — the point is what to look for.) Read it as: does `scoped` meaningfully beat `naive` on p95/p99 latency and total cost at the same session shape, and at what concurrency does the `throttled` count start climbing for one strategy before the other? That comparison is the actual deliverable for your dev lead — it turns "guardrails might be slow/expensive at scale" into a specific, defensible number.

## 6. Tuning it toward your real traffic

- `--system-prompt-chars` / `--turn-chars`: set these to match your actual Claude Code/Cowork system prompt size and typical per-turn payload (tool output, file contents, etc.) if you have real numbers, rather than the ~30K default assumption from the research doc.
- `--concurrency`: push this up until the `throttled` column starts climbing — that's where you find the account's real TUPS/RPS ceiling for your region, empirically, rather than trusting the published default quota.
- Run the whole thing more than once on different days/times if you want confidence the numbers aren't a one-off network blip — the script doesn't currently loop across multiple calendar days automatically, that's a manual re-run.

## What neither script covers

- **End-to-end `/chat/completions` or `Converse` latency.** Both scripts measure the guardrail in isolation, not a full model call with a guardrail attached (which adds real inference time and token cost on top). That's deliberate — it isolates guardrail overhead specifically, which is what was in question. If you also want the full "user hits send, response comes back" number, that's a reasonable next script — it would need to go through `/chat/completions` on LiteLLM with `"guardrails": [...]` in the request, and the naive/scoped distinction gets murkier there since a chat API always sends the full message list anyway.
- **Detection accuracy.** Both scripts measure speed and errors/blocking, not whether the guardrail actually catches what it's supposed to catch, or how often it wrongly blocks legitimate content. That's a separate, non-performance question (see "What's still unanswered" in the research doc).
