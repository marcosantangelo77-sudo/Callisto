Callisto
A locally-hosted autonomous agent system governed by the Aluft Gianne Protocol.

Overview
Callisto is an autonomous multi-agent reasoning system built on a fork of NousResearch's Hermes function-calling framework. It runs entirely on consumer hardware using open-weight local models served through Ollama. No cloud API. No external data transmission. No ongoing cost beyond electricity.
Callisto is structured reasoning infrastructure. It classifies information, enforces epistemological discipline, and accumulates domain-separated institutional knowledge over time.

Hermes
Callisto is built on a fork of NousResearch/hermes-function-calling — an open-source framework for structured function calling and agentic tool use with large language models.
The upstream repository is treated as a parts supplier. It is tracked as a fetch-only remote. Upstream commits are evaluated individually before any cherry pick decision is made. The core Callisto divergences — the AGP module, memory system, and orchestrator — are never upstreamed.

Local Models
Callisto runs three specialized agents locally via Ollama:
The Architect — Qwen3.5-35B-A3B
Handles complex reasoning, code generation, synthesis, and architecture decisions. Mixture-of-Experts architecture with only 3.5B active parameters per token — high intelligence at manageable inference cost.
The Manager — GPT-OSS 20B
Enforces domain separation, routes tasks, and catches logical errors the Architect misses. Architectural diversity between Manager and Architect is intentional — different training biases produce different failure modes. Disagreement between agents is a feature.
The Sentinel — DeepSeek-R1 14B
Monitors continuously, classifies every incoming signal, assigns domain tags, and escalates without drawing conclusions. Always loaded. Always watching.
All models run locally. All inference stays on device. Models are swappable — the system depends on the AGP contract, not on any specific model.

The Aluft Gianne Protocol
Every inference Callisto makes is governed by the Aluft Gianne Protocol — a structured research methodology that enforces disciplined, trustworthy analysis at every step.
The Protocol is named after Aluft Gianne Sr. — a gnome master baker who follows his recipes with exactness, never mixing flavours carelessly, always working clean. So too does Callisto govern the preparation of knowledge.
The Four Pillars:
I. Domain Separation
Knowledge is compartmentalized by domain. FINANCIAL findings cannot contaminate TECHNICAL conclusions. SIGNAL evidence cannot be promoted to a conclusion without Primary corroboration. Cross-domain contamination is architecturally impossible — enforced in code, not just policy.
II. Session Integrity
Every research session opens with a declared scope and closes with a written summary. A session with no summary never happened. All sessions are timestamped, logged, and permanently archived.
III. Source Trustworthiness
Every piece of evidence carries a source class — Primary, Secondary, Signal, or Inferred — and a confidence tier. UNVERIFIED findings cannot be stored anywhere in the system.
IV. Output Honesty
Conclusions must accurately reflect the confidence of the underlying evidence. No conclusion may overstate certainty. False confidence is treated as a greater failure than acknowledged uncertainty.
The Seven Step Session Structure:

Declare Scope — one question per session
Assign Domain Tag — FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, or GENERAL
Source Enumeration — list sources before consulting them
Primary Collection — gather evidence with confidence tiers
Contradiction Check — actively search for contradictions. Absence is a flag not a comfort.
Synthesis — conclusion must match the confidence of the evidence
Session Close — write the summary, seal the session, archive permanently


Compartmentalized Memory
Callisto's memory system is a provenance catalogue with segregated domain worlds, backed by a single SQLite database in WAL mode.
Every memory entry carries a unique entry ID, origin agent, session ID, source class, confidence tier, domain tag, and full promotion history. Entries are immutable once written.
Domain worlds are fully isolated — enforced by CHECK constraints at the database layer and the application API. Nothing crosses boundaries without authorization. Every cross-domain synthesis access is logged permanently.
Any world is reconstructible from the underlying catalogue at any time. The catalogue is the source of truth. Domain views (world_financial, world_technical, etc.) ARE the worlds.
UNVERIFIED findings (confidence_score < 0.30) are physically impossible to store — the database rejects them.

Front door

The operator CLI is `callisto.py` (ask / runs / show / status / doctor), not a hand-wired pipeline:

```bash
python callisto.py doctor
python callisto.py ask --backend gpu1 "Did the 2009 federal minimum wage increase to $7.25?"
python callisto.py runs
python callisto.py show <run_id>
```

`CALLISTO_LOCAL_ONLY=1` strips hosted inference (OpenRouter / ox_alpha). Pin `--backend` to a local tier such as `gpu1`. Live execution stays OFF unless `CALLISTO_ALLOW_LIVE_EXECUTE=1`. Paper-trading signals never include live hypotheses.

Quick Start

Prerequisites: Python 3.11+, Ollama running with all three models loaded.

```bash
# Install dependencies
pip install -r requirements.txt

# Fix the Manager model (replaces "You are ChatGPT" identity)
ollama create manager:latest -f modelfiles/Manager.Modelfile

# Configure
cp .env .env.local  # edit with your Brave API key

# Start the API server (port 8420)
python api.py
```

API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/task` | Submit a query for AGP session processing. Body: `{"query": "...", "priority": 0}` |
| GET | `/task/{id}` | Get task status and result |
| GET | `/session/{id}` | Get a sealed AGP session with full provenance |
| GET | `/world/{domain}` | Query a domain world. Params: `keyword`, `min_confidence`, `limit` |
| GET | `/health` | Health check for all three agents |

Upstream Review

```bash
python upstream_review.py
```

Fetches new commits from hermes-function-calling, shows diffs, and has the Architect evaluate each under AGP TECHNICAL domain rules. Outputs PULL / SKIP / MODIFY_THEN_PULL with reasoning.

Repository Structure
Callisto/
  agp/__init__.py              ← AGP protocol core — enums, session lifecycle, sealing
  tools/brave_search.py        ← Brave Search API tool
  modelfiles/Manager.Modelfile ← fixed Manager system prompt (no more ChatGPT)
  hermes-function-calling/     ← upstream pinned at ea3c4723, fetch-only
  inference.py                 ← Ollama adapter (replaces Hermes transformers layer)
  orchestrator.py              ← AGP 7-step session runner
  memory.py                    ← compartmentalized SQLite memory
  task_queue.py                ← persistent task queue
  api.py                       ← FastAPI REST layer (port 8420)
  monitor.py                   ← agent health monitor
  upstream_review.py           ← upstream commit evaluator CLI
  .env                         ← config (Ollama host, Brave key, port, DB path)
  requirements.txt             ← dependencies (no torch/transformers)
  mcp_config.json              ← MCP server skeleton
  UPSTREAM.md                  ← upstream tracking docs

The Protocol is the recipe. The agents follow it exactly.