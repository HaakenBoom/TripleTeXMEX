# Tripletex — Scoring

## Correctness (Field-by-Field Verification)
After our agent responds, the platform queries Tripletex API to verify what was created/modified. Each task has specific checks worth different point values.

**Example — "Create employee" task (max 10 points):**
| Check | Points |
|-------|--------|
| Employee found | 2 |
| Correct first name | 1 |
| Correct last name | 1 |
| Correct email | 1 |
| Administrator role assigned | 5 |

Raw score normalized to 0–1: `correctness = points_earned / max_points`

## Tier Multiplier
| Tier | Multiplier | Examples |
|------|-----------|----------|
| Tier 1 | x1 | Create employee, create customer |
| Tier 2 | x2 | Create invoice, register payment |
| Tier 3 | x3 | Complex multi-step workflows |

Perfect Tier 2 = 1.0 x 2 = 2.0 base score.

## Efficiency Bonus
**Only applies to perfect submissions (correctness = 1.0).** Can up to **double** your tier score.

Two factors:
1. **Call efficiency** — fewer API calls vs best known solution = higher bonus
2. **Error cleanliness** — fewer 4xx errors (400, 404, 422) = higher bonus

| Scenario (Tier 2 task) | Score |
|------------------------|-------|
| Failed all checks | 0.0 |
| 80% of checks passed | 1.6 |
| Perfect, but many errors and extra calls | ~2.1 |
| Perfect, efficient, a few errors | ~2.6 |
| Perfect, best-in-class efficiency, zero errors | 4.0 |

**Max possible scores:** Tier 1 = 2.0, Tier 2 = 4.0, Tier 3 = 6.0

Efficiency benchmarks recalculated periodically. Best scores recalculated every 12 hours.

## Best Score Per Task
- Your score per task = **all-time best** — bad runs never lower it
- Each of the 30 tasks tracked independently
- Leaderboard = sum of best scores across all task types

## Task Assignment
Each submission gets one task, weighted toward tasks you've attempted less. Over many submissions you'll encounter all types.

## Rate Limits
| Limit | Verified teams | Unverified teams |
|-------|---------------|-----------------|
| Concurrent submissions | 3 | 1 |
| Per task per day | 10 | 3 |
