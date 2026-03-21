# Session Log — March 21, 2026

## What We Built This Session

### 1. Analysis Framework (the operating model)

We designed and implemented a systematic **Run → Log → Analyze → Improve → Repeat** loop.

#### Logging Schema (agent/solver.py)
Every run now captures:
- `prompt_fingerprint` — SHA256 hash for tracking same task across runs
- `prompt_language` — detected language (nb/en/fr/es/de/pt/nn)
- `phase_times` — seconds spent in each phase: parse, prefetch, handler, agent_loop
- `call_phases` — API calls classified as: prefetch, action, wasted, lookup
- `mutations` — list of successful create/update/delete with entity IDs
- `status_counts` — HTTP status distribution (2xx, 4xx)
- Plus everything from before: prompt, parsed_task, path_taken, result, api_calls, errors

#### Analysis Tool (analyze_runs.py)
Enhanced with:
- **Root cause classification** — every failure gets one actionable category:
  PARSE, HANDLER_BUG, HANDLER_MISSING, API_FORMAT, API_PREREQ, AGENT_LOOP, CORRECTNESS, EFFICIENCY, OK
- **Impact scoring** — points left on the table per failure (max_possible - estimated_score)
- **Prioritized fix list** — sorted by recoverable points, highest first
- **Phase timing breakdown** — identifies bottlenecks (parse vs prefetch vs handler)

#### A/B Comparison Tool (compare_runs.py)
- Compare baseline vs candidate run sets by timestamp cutoff
- Per-task score delta with regression detection
- Automated KEEP/REVERT/INVESTIGATE recommendation
- Experiment logging with hypothesis/risk tracking
- `python compare_runs.py --regression-check` for quick regression detection

#### Baseline Management (baseline.py)
- `python baseline.py save "name" "description"` — tags git commit
- `python baseline.py list` — show all baselines
- `python baseline.py diff name` — diff current code vs baseline
- `python baseline.py score --task 1 0.75 --task 2 2.00` — record scoreboard scores
- `python baseline.py scores` — show score history with trends

### 2. Initial Scoreboard Snapshot

Recorded from the competition platform screenshot:
- **Total: 26.18 points** (24/30 tasks attempted)
- Zero-score tasks: 11, 12, 21, 24
- Perfect task: 14 (4.00)
- Saved as first entry in `scoreboard_history.json`

### 3. Baseline Saved

```
python baseline.py save "pre-first-batch" "Clean slate before first real runs"
```
Git tag: `baseline/pre-first-batch` (commit 4a92bdb3)

---

## Real Run Results (4 runs)

| # | Task | Tier | Score | Max | Path | Calls | Errors | Time |
|---|------|------|-------|-----|------|-------|--------|------|
| 1 | create_invoice (nb) | T2 | **7/7** | 4.0 | deterministic | 7 | 0 | 41.9s |
| 2 | create_invoice_with_payment (nn) | T2 | **2/7** | 4.0 | deterministic | 7 | 0 | 36.9s |
| 3 | create_credit_note (pt) | T2 | **1/8** | 4.0 | deterministic | 5 | 0 | 22.1s |
| 4 | bank_reconciliation (pt) | T3 | **0/10** | 6.0 | agent_loop | 17 | 1 | 537s |

### Run 1: create_invoice — PERFECT (7/7)
- Norwegian prompt, simple invoice for Nordhav AS
- Deterministic path, 7 calls, 0 errors
- Bank account setup worked, customer found, order+invoice created

### Run 2: create_invoice_with_payment — MISCLASSIFIED (2/7)
- Nynorsk prompt: "Kunden Strandvik AS har ein uteståande faktura... Registrer full betaling"
- **Root cause: PARSE** — prompt says customer HAS an existing invoice, register payment
- Parser classified as `create_invoice_with_payment` → created NEW invoice instead of finding existing
- Should have been `reverse_invoice_payment`

### Run 3: create_credit_note — WRONG INVOICE (1/8)
- Portuguese prompt: credit note for "Serviço de rede" at 29,150 NOK
- **Root cause: CORRECTNESS** — two invoices existed for Cascata Lda:
  - Invoice #1: 25,150 NOK (handler picked this — WRONG)
  - Invoice #2: 29,150 NOK (matches prompt — CORRECT)
- Handler used `values[0]` without matching by amount

### Run 4: bank_reconciliation — NO HANDLER (0/10)
- Portuguese prompt: reconcile bank statement CSV with open invoices
- **Root cause: HANDLER_MISSING** — parser classified as `unknown`, fell to agent loop
- Agent loop made progress (5 payments registered) but hit max iterations at 537s
- T3 task worth 6 points — deferred for now due to complexity

---

## Bugs Fixed This Session

### Fix 1: 0% VAT Override Bug (handlers.py)
**Commit**: `53e92dc` — "Fix 0% VAT override bug: trust pre-seeded product VAT type"

When a product was pre-seeded with vatType id=6 (0% exempt), the safety check was incorrectly overriding it to 25% because the parser's raw string "0%" didn't contain exempt keywords. Now trusts the product's own VAT type. Also added Portuguese ("isento") and Nynorsk ("friteken") exempt keywords.

### Fix 2: Agent Loop Reliability (solver.py, handlers.py)
**Commit**: `534decc` — "Improve agent loop reliability and observability"

- Auto-fix interceptor for vatType strings, quantity→count, bare ints
- Duplicate entity prevention in agent loop
- Bank account RuntimeError on failure
- deterministic_error saved in run logs

### Fix 3: Credit Note Wrong Invoice (handlers.py)
**Commit**: `f906697` — "Fix credit note invoice matching and parser payment classification"

Added `_match_invoice()` function that matches invoices by `amountExcludingVat` and description instead of blindly picking `values[0]`. Falls back to first non-credit-note invoice if no match.

### Fix 4: Parser Payment Misclassification (parser.py)
**Commit**: `f906697` — same commit as Fix 3

Added Rule 5 in `_post_validate_classification()`: if classified as `create_invoice_with_payment` but prompt has outstanding/unpaid keywords and NO create keywords, reclassify to `reverse_invoice_payment`. Also added system prompt hint for the LLM classifier.

---

## Improvement Loop Process

```
1. SUBMIT RUNS    → Start server, submit on platform
2. COLLECT LOGS   → run_logs/run_YYYYMMDD_HHMMSS.json (automatic)
3. ANALYZE        → python analyze_runs.py -v
                    Look at: root cause breakdown, prioritized fix list
4. PICK TARGET    → Choose top-1 fix by impact score
5. SAVE BASELINE  → python baseline.py save "name" "description"
6. IMPLEMENT      → Make ONE focused change
7. TEST OFFLINE   → python -m agent.test_handlers
8. COMMIT + PUSH  → git commit, git push
9. RE-SUBMIT      → Run another batch
10. COMPARE       → python compare_runs.py --regression-check
11. DECIDE        → KEEP (promote to baseline) / REVERT (git checkout baseline/name)
```

## Prioritization Model

```
priority = impact x tier_weight

where:
  impact = max_possible_score - estimated_score
  tier_weight = T3=3, T2=2, T1=1

Example:
  Broken T3 task (6 pts max, 0 scored) = 6.0 impact — highest priority
  Broken T2 task (4 pts max, 1 scored) = 3.0 impact
  Inefficient T1 (2 pts max, 1.5 scored) = 0.5 impact — lowest priority
```

## Regression Prevention

1. **Git tags** — `python baseline.py save` creates `baseline/name` tag
2. **A/B comparison** — `python compare_runs.py --regression-check`
3. **Experiment log** — `python compare_runs.py --log-experiment` tracks what changed
4. **Best-score-kept** — competition keeps all-time best per task, so bad runs can't lower score
5. **One variable at a time** — per AGENTS.md methodology

## Key Files

| File | Purpose |
|------|---------|
| `agent/solver.py` | Main orchestrator: parse → prefetch → handler → agent loop |
| `agent/handlers.py` | Deterministic handlers for each task type |
| `agent/parser.py` | LLM-based prompt parser (task_type + entities) |
| `agent/server.py` | FastAPI server receiving tasks from platform |
| `agent/tripletex_client.py` | HTTP client + API call logging |
| `analyze_runs.py` | Run log analyzer with root cause classification |
| `compare_runs.py` | A/B comparison + experiment tracking |
| `baseline.py` | Baseline management + scoreboard tracking |
| `agent/test_handlers.py` | Offline handler tests with fake API |
| `agent/test_simulator.py` | Fake Tripletex API for testing |

## Scoring Formula

```
score = tier x (1.0 + efficiency_bonus)   [if correctness = 1.0]
score = correctness x tier                 [if correctness < 1.0]

efficiency_bonus = (call_efficiency + error_cleanliness) / 2
call_efficiency = min(1.0, optimal_calls / actual_calls)
error_cleanliness = max(0.0, 1.0 - (errors_4xx / total_calls) x 2)

Max scores: T1 = 2.0, T2 = 4.0, T3 = 6.0
```

## What's Next

Highest-impact items remaining:
1. **Verify fixes** — submit runs, check if credit_note and payment scores improved
2. **Bank reconciliation handler** — T3 task worth 6 pts, needs CSV parsing + invoice/payment matching
3. **Prefetch optimization** — 12-22s spent on prefetch, could be reduced
4. **Cover zero-score tasks** — Tasks 11, 12, 21, 24 still at 0 points
