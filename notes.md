# Guardrails Research: Bedrock Guardrails, Presidio, and LiteLLM

*Prepared for: scaling/cost assessment of applying guardrails to agentic, long-context workloads (e.g. Claude Code / Cowork-style clients with ~30K character system prompts and growing conversation history)*

## TL;DR

"Just flip on Bedrock Guardrails / Presidio" is true for a simple chatbot with short turns. It is **not** free once the client is an agentic tool like Claude Code/Cowork, because:

- By default, both the standard guardrail attachment (`InvokeModel`/`Converse`) and naive `ApplyGuardrail` usage re-evaluate the **entire payload** — system prompt + full conversation history — on **every single turn**, even though 95%+ of that text hasn't changed since the last call.
- Bedrock Guardrails bills per 1,000 characters ("text unit"), per policy, per evaluation. Cost scales roughly linearly with total context size, and context grows every turn in a multi-turn agent session — so **cumulative cost across a session grows faster than linearly** if you don't scope evaluation.
- There's a **shared, account-wide throughput quota** (text-units-per-second), not just a per-request cost. Large-context re-evaluation eats this quota fast — this is the "slow" risk, separate from the "expensive" risk.
- The fix isn't to abandon Bedrock Guardrails, it's to **scope what gets evaluated** (new turn only, not full history) using features AWS already provides for exactly this problem. That needs to be deliberately configured — it is not the default behavior.
- Presidio has no per-call fee (self-hosted, open source) but you own the compute and scaling; latency depends on which recognizers you enable.
- LiteLLM is not its own detection engine — it's an orchestration layer that calls out to Bedrock, Presidio, or other providers. It contributes its own (small) hook overhead on top of whichever engine you point it at.

---

## 1. What each option actually provides

### 1.1 Amazon Bedrock Guardrails

A managed set of policies you attach to a guardrail resource, then reference at inference time. Available policies:

| Policy | What it does |
|---|---|
| Content filters | Blocks hate, insults, sexual, violence, misconduct, prompt-attack categories (configurable strength) |
| Denied topics | Blocks free-text-defined topics (e.g. "financial advice") |
| Word filters | Static blocklist / profanity list |
| Sensitive information filters (PII/PHI) | Detects and blocks or masks ~30 built-in PII entity types, plus custom regex entities |
| Contextual grounding checks | Flags model responses that aren't grounded in provided source content, or aren't relevant to the query (RAG-oriented) |
| Automated Reasoning checks | Validates/corrects factual claims in model responses against a formal policy you define (newer, priced separately) |
| Image content filters | Same categories as text, applied to image inputs/outputs |

It can be attached two ways:
1. **Inline with inference** (`InvokeModel`, `Converse`, and streaming variants) — guardrail runs automatically as part of the model call.
2. **Standalone `ApplyGuardrail` API** — you call the guardrail independently of any model invocation (useful for evaluating content that never goes near a model, or for pre-checking before you spend money on inference).

Evaluation order: input is checked first (in parallel across policies); if it passes, inference happens; the output is then checked before being returned. If input is blocked, you're charged for the guardrail check but not the model call. If output is blocked, you're charged for both (model already ran).

### 1.2 Microsoft Presidio

Open-source (MIT), self-hosted. Two main services: **Analyzer** (detects PII via NER models + regex + checksum recognizers) and **Anonymizer** (redacts/masks/encrypts what the Analyzer finds). No per-call fee — cost is entirely the compute you run it on (typically Docker/Kubernetes, one or more containers per service). It only does PII/PHI detection and redaction — no content-safety, no denied-topics, no jailbreak/prompt-injection detection. Detection quality and latency both depend heavily on which recognizers/NLP models you enable (regex-only is fast and shallow; the default spaCy-based NER model is slower but catches more, e.g. names and addresses in free text).

### 1.3 LiteLLM

LiteLLM (the proxy/gateway) does **not** ship its own ML-based detection engine. Its guardrails feature is an orchestration/hook framework: `pre_call`, `during_call` (streaming), and `post_call` hooks that call out to a provider — Bedrock Guardrails, Presidio, Lakera, Azure Content Safety, PANW, OpenAI Moderation, etc. It does include one thing natively: a regex-based PII filter (fast, but only catches structured patterns like credit card/SSN formats — not contextual PII like names in prose). Free-tier open source covers custom guardrails and the Presidio integration; some polished built-ins (e.g. certain LLM Guard integrations) sit behind LiteLLM's paid Enterprise tier. Net effect: LiteLLM adds a thin, generally low-overhead hook layer on top of whichever engine(s) you wire in — it doesn't remove the cost/latency characteristics of Bedrock Guardrails or Presidio, it just gives you one place to configure and chain them.

---

## 2. Bedrock Guardrails: cost

Billed in "text units" = up to 1,000 characters, per policy type, per evaluation (input and output are billed separately if both are checked):

| Policy | Price per 1,000 text units |
|---|---|
| Content filters | $0.15 |
| Denied topics | $0.15 |
| Sensitive information (PII) filters | $0.10 |
| Sensitive information filters (regex-based custom entities) | Free |
| Contextual grounding checks | $0.10 |
| Automated Reasoning checks | $0.17 (per policy) |
| Word filters | Free |
| Image content filters | $0.00075 per image |

Confirmed directly against the [official Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) (checked August 2026). A text unit is up to 1,000 characters — a 5,600-character input is billed as 6 text units (rounds up).

(Standalone `ApplyGuardrail`-only calls, outside `InvokeModel`/`Converse`, are priced slightly differently — roughly $0.07–$0.10 per 1,000 units depending on check type. Confirm current numbers against the [official pricing page](https://aws.amazon.com/bedrock/pricing/) before budgeting, as these are periodically revised.)

Key billing rule: **you're charged for the evaluation regardless of outcome.** If the guardrail blocks the input, you pay for the guardrail check (but not model inference). If it passes and the output is later blocked, you pay for both the guardrail checks and the model inference that already happened.

Text units round up per policy — a 600-character chunk is billed as a full 1,000-character unit, so unaligned chunking wastes money.

---

## 3. The scaling problem, with real numbers

Take a Claude Code/Cowork-style session: ~30,000-character system prompt, plus conversation history that grows every turn (tool calls, file contents, prior responses).

**If guardrails are attached the naive way** (no scoping — the default for `Converse`/`InvokeModel` when you don't explicitly scope content), the guardrail re-evaluates the *entire* payload every turn:

- Turn 1: 30K chars of system prompt alone ≈ 30 text units.
- Turn 10, with accumulated history: could easily be 80–150K characters ≈ 80–150 text units, evaluated on **every single call**, even though the system prompt has been identical since turn 1.
- With content filters + PII filters both enabled ($0.15 + $0.10 = $0.25/1K units) on input alone, that's $0.02–0.04 per turn just for guardrail overhead — before output evaluation, before model cost, and this grows every turn within the session.
- Across a long agentic session (Claude Code-style flows can issue many model calls per user request as it plans and uses tools), this compounds fast: the cumulative guardrail spend for one session can rival or exceed the guardrail cost of a hundred short, stateless chatbot exchanges.

**The throughput risk is arguably worse than the cost risk.** Bedrock enforces account-wide (not per-request) quotas:
- Requests per second (RPS) for `ApplyGuardrail`: 50/sec (confirmed via [AWS's Feb 2025 quota-increase announcement](https://aws.amazon.com/about-aws/whats-new/2025/02/amazon-bedrock-guardrails-increase-service-quota-limits/) — up from 25/sec).
- Text-units-per-second (TUPS) for content filters/sensitive-info filters/word filters: 200/sec account-wide (up from 25/sec).
- Per that same announcement, these higher limits were rolled out to **US East (N. Virginia) and US West (Oregon) only** at the time — confirm current limits for your actual deployment region via Service Quotas before relying on this number, since other regions may still be on the lower default and quotas may have shifted further since Feb 2025.

200 TUPS ≈ 200,000 characters/second, shared across **everything** using guardrails in that account/region. If a single agentic session sends ~100K characters of context per guardrail check (input + output), that's ~100–200 text units consumed per call — meaning the account could be throttled with as few as **1–2 concurrent large-context guardrail evaluations per second**, well before you'd expect to hit typical LLM invocation rate limits. Multiple concurrent Cowork/Claude Code users hitting the same guardrail would compete for this same shared quota.

This matches what AWS's own guidance for coding-assistant workloads says: guardrails on code-gen tools need explicit throughput/capacity planning — it's called out as a distinct concern from general chatbot use, precisely because of large system prompts, streaming output, and concurrent sessions ([AWS: Best practices for applying Bedrock Guardrails to code generation workflows](https://aws.amazon.com/blogs/machine-learning/best-practices-for-applying-amazon-bedrock-guardrails-to-code-generation-workflows/)).

---

## 4. How to avoid re-scanning the whole context every turn

This is the actual lever — Bedrock has purpose-built mechanisms for this, they're just opt-in:

1. **`GuardrailConverseContentBlock` (Converse API) — scope to the new turn only.** If you wrap only the newest user message (or newest model output) in a `guardContent` block, the guardrail evaluates *only that block*, not the rest of the conversation. If you omit `guardContent` entirely, Bedrock defaults to evaluating the **entire** message list — this is almost certainly the behavior producing the "applies to the whole context every message" concern. Scoping to the last turn is the single biggest lever here, since the system prompt and prior history were (presumably) already evaluated when they were first introduced.
2. **Trust boundaries / selective evaluation with the standalone `ApplyGuardrail` API.** AWS's own guidance: you don't need to re-evaluate content at every step of an agentic loop — evaluate at trust boundaries (e.g., raw user input in, final output out) rather than every intermediate reasoning/tool-call token.
3. **1,000-character batching alignment.** Because billing rounds up per policy to the next 1,000-character unit, chunk/batch content to 1,000-char boundaries rather than sending arbitrary-sized fragments — AWS's code-gen guidance cites up to a 20x reduction in guardrail API calls by increasing the streaming-evaluation interval from the 50-character default to 1,000 characters.
4. **Risk-tiered policy selection.** Not every policy needs to run on every piece of content — e.g., run PII/content filters on user input and final output, but skip denied-topics or contextual-grounding checks on intermediate tool output that never reaches the user.
5. **Cache the "this hasn't changed" evaluation.** If the system prompt is static across a session (or across all sessions until you change it), there's no correctness reason to guardrail-check it on every turn — evaluate it once (or once per system-prompt version) and treat it as trusted for the rest of the session, applying guardrails only to the delta (new user input + new model output).

None of this is automatic — it requires deliberately building the "only scan what's new" pattern into however guardrails get wired into the pipeline (directly, or via LiteLLM's guardrail hooks).

---

## 5. Presidio: cost and scaling notes

- No per-request fee; cost = infrastructure (Analyzer + Anonymizer containers, CPU/memory, and optionally a GPU/larger CPU allocation if you want the more accurate NER model instead of regex-only).
- Latency is a direct function of which recognizers are enabled — regex/checksum recognizers (credit cards, SSNs, emails) are fast and cheap; the default NLP-based recognizer (spaCy) for contextual entities like names/addresses is meaningfully slower.
- Self-hosting means you control scaling (horizontal pod autoscaling, batching, caching) but also own the operational burden — no AWS-managed throughput ceiling, but no AWS-managed elasticity either.
- Same "don't re-scan the whole context every turn" problem applies if you run all conversation text through Presidio on every call — the fix is the same in spirit: only run Presidio over the new turn, not history that's already been checked.

---

## 6. Recommended approach for testing performance implications

Goal: get real numbers (latency and cost) for guardrails under Claude Code/Cowork-shaped traffic, rather than relying on vendor-quoted averages, before committing to an approach.

**What to measure**
- End-to-end added latency (guardrail time on top of baseline model-call latency), not just guardrail-service-reported time — measure from the calling application's perspective.
- p50/p95/p99, not just averages — tail latency is what a user actually notices, and guardrail latency is more prone to outliers (external service calls, model-based evaluators) than raw LLM inference.
- Cost per session (not just per call) — model a realistic session shape (system prompt + N turns of growing history) rather than a single flat request.
- Throughput ceiling — how many concurrent sessions before hitting Bedrock's RPS/TUPS quotas, and what the failure mode looks like (throttling error vs. queuing vs. silent delay).

**Test matrix to run**
- Naive (full context re-evaluated every turn) vs. scoped (`guardContent`/last-turn-only) — this is the comparison that answers the original question directly.
- Varying context sizes: representative of turn 1 (system prompt only), a mid-session turn, and a "long session" turn (e.g. 30K / 80K / 150K+ characters) to see how cost and latency scale with context size.
- Varying policy combinations: content filters only, PII only, content+PII+topics combined — since each policy is billed and evaluated independently, isolating which policy actually drives cost/latency matters for tuning.
- Bedrock Guardrails vs. Presidio vs. both — same input set through each, so the numbers are comparable rather than anecdotal.
- Concurrency sweep: single session, then increasing concurrent sessions, to find where account-wide TUPS/RPS throttling kicks in.

**How to structure it**
- Build a small harness that replays realistic Claude Code/Cowork-shaped payloads (real or synthetic system prompt + incrementally growing history) against each guardrail path, recording latency and computing cost from the actual text-unit counts used.
- Run in a controlled environment (consistent network path, no other traffic competing for the account's guardrail quota) to get clean baseline numbers, then repeat under concurrent load to see quota/throttling behavior — averaging a handful of repeated runs per scenario rather than trusting a single sample.
- Keep the harness output as raw numbers (CSV/JSON) so the comparison (naive vs. scoped, Bedrock vs. Presidio) can be re-run cheaply if AWS changes pricing or quotas.

This gives a Confluence-ready before/after answer: "naive guardrail attachment costs $X and adds Yms per turn at this session size; scoped evaluation costs $X' and adds Y'ms" — which is the concrete number the "it's just an easy switch" claim is missing.

---

## 7. Open questions worth resolving before committing

- Does the target architecture route through LiteLLM, or call Bedrock/Presidio directly? This determines whether `GuardrailConverseContentBlock` scoping is available out of the box (native Bedrock SDK) or needs to be added to LiteLLM's guardrail hook config (check current LiteLLM support for `GuardrailConverseContentBlock` — as of this research it was an open feature request, not yet native).
- What's the actual compliance requirement — is guardrail coverage on system-prompt/history genuinely needed (e.g. is untrusted content ever injected into history), or is user-input + final-output sufficient? This materially changes both cost and design.
- Confirm current Bedrock Guardrails pricing and quota numbers against the [AWS pricing page](https://aws.amazon.com/bedrock/pricing/) and your account's service quotas before finalizing a cost model — both have changed within the past year and may change again.

---

## Sources

- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [How Amazon Bedrock Guardrails works](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-how.html)
- [Include a guardrail with the Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html)
- [Use the ApplyGuardrail API in your application](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html)
- [Use the ApplyGuardrail API with long-context inputs and streaming outputs](https://aws.amazon.com/blogs/machine-learning/use-the-applyguardrail-api-with-long-context-inputs-and-streaming-outputs-in-amazon-bedrock/)
- [Best practices for applying Amazon Bedrock Guardrails to code generation workflows](https://aws.amazon.com/blogs/machine-learning/best-practices-for-applying-amazon-bedrock-guardrails-to-code-generation-workflows/)
- [Amazon Bedrock Guardrails announces an increase in service quota limits](https://aws.amazon.com/about-aws/whats-new/2025/02/amazon-bedrock-guardrails-increase-service-quota-limits/)
- [Microsoft Presidio (GitHub)](https://github.com/microsoft/presidio)
- [LiteLLM: Guardrail Providers](https://docs.litellm.ai/docs/guardrail_providers)
- [LiteLLM: PII/PHI Masking with Presidio](https://docs.litellm.ai/docs/proxy/guardrails/pii_masking_v2)
- [LiteLLM: Bedrock Guardrails integration](https://docs.litellm.ai/docs/proxy/guardrails/bedrock)
- [LLM Guardrails Latency: Performance Impact and Optimization](https://modelmetry.com/blog/latency-of-llm-guardrails)
