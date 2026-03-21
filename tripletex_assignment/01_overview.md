# Tripletex AI Accounting Agent — Overview

## What We're Building
An AI agent that receives accounting task prompts (in 7 languages), interprets them, and executes the correct Tripletex API calls to complete the task.

## Flow
1. Submit HTTPS endpoint URL on the platform
2. Platform provisions a **fresh** Tripletex sandbox account (empty every time)
3. Platform sends a randomly selected accounting task to our `/solve` endpoint
4. Our agent reads the prompt, optionally processes attached files (PDFs, images)
5. Our agent calls the Tripletex API via a proxy to complete the task
6. Platform verifies the result field-by-field against expected values
7. Score updates on the rolling leaderboard

## Key Numbers
| Fact | Value |
|------|-------|
| Task types | 30 different accounting tasks |
| Variants per task | 56 (7 languages x 8 data sets) |
| Languages | Norwegian (nb), English, Spanish, Portuguese, Nynorsk, German, French |
| Timeout | 5 minutes per submission |
| API | Tripletex v2 REST API via authenticated proxy |
| Score range | 0.0 — 6.0 (perfect Tier 3 + best efficiency) |
| Best score kept | Yes — bad runs never lower your score |

## Task Categories
| Category | Examples |
|----------|----------|
| Employees | Create employees, set roles, update contact info |
| Customers & Products | Register customers, create products |
| Invoicing | Create invoices, register payments, issue credit notes |
| Travel Expenses | Register or delete travel expense reports |
| Projects | Create projects linked to customers |
| Corrections | Delete or reverse incorrect entries |
| Departments | Create departments, enable accounting modules |

Tasks range from simple single-API-call operations to multi-step workflows requiring several resources to be created and linked together.

## Tier Release Schedule
- **Tier 1** — open now (foundational: create employee, customer, invoice)
- **Tier 2** — open now (multi-step: invoice with payment, credit notes, project billing)
- **Tier 3** — opens early Saturday (complex: bank reconciliation, error correction, year-end closing)

## Submission URL
https://app.ainm.no/submit/tripletex
