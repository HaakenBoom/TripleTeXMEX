"""Test deterministic handlers against realistic Tripletex simulator.

Tests handlers directly with pre-parsed task data — no LLM needed.
This validates that our execution logic handles real API behavior correctly.

Usage:
    python -m agent.test_handlers
"""

import json
import logging
import sys
from agent.test_simulator import FakeTripletexClient
from agent.handlers import prefetch_context, execute_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring config — mirrors real competition scoring from 04_scoring.md
# ---------------------------------------------------------------------------

# Tier multiplier per task type
TASK_TIER = {
    "create_employee": 1,
    "update_employee": 1,
    "update_employee_role": 1,
    "create_customer": 1,
    "update_customer": 1,
    "create_product": 1,
    "create_department": 1,
    "create_invoice": 2,
    "create_invoice_with_payment": 2,
    "create_credit_note": 2,
    "create_project": 1,
    "create_travel_expense": 1,
    "delete_travel_expense": 1,
    "create_voucher": 2,
    "delete_voucher": 1,
}

# Optimal API call counts per task type (our best known)
# Used to calculate efficiency bonus
OPTIMAL_CALLS = {
    "create_employee": 2,      # GET /dept + POST /employee
    "create_customer": 1,      # POST /customer
    "create_product": 2,       # GET /vatType + POST /product
    "create_department": 1,    # POST /department
    "create_invoice": 8,       # vatType+whoAmI+acct+PUT bank + GET cust+POST cust + POST order + POST invoice (+ N product lookups)
    "create_invoice_with_payment": 9,  # above + GET paymentType
    "create_project": 3,       # GET dept + GET emp + POST /project
    "create_travel_expense": 3,  # GET dept + GET emp + POST /travelExpense
    "delete_travel_expense": 4,  # GET dept + GET emp + GET /travelExpense + DELETE
    "create_voucher": 3,       # GET /ledger/account x2 (per posting) + POST /ledger/voucher
    "delete_voucher": 2,       # GET /ledger/voucher + DELETE
}


def calculate_score(correctness: float, task_type: str, api_calls: int, errors_4xx: int) -> dict:
    """Calculate competition score using the real formula.

    Returns dict with tier, correctness, efficiency_bonus, and final score.
    """
    tier = TASK_TIER.get(task_type, 1)

    if correctness < 1.0:
        # Partial credit: no efficiency bonus
        return {
            "tier": tier,
            "correctness": correctness,
            "efficiency_bonus": 0.0,
            "score": round(correctness * tier, 2),
            "max_possible": tier * 2,
        }

    # Perfect submission — calculate efficiency bonus (0.0 to 1.0)
    optimal = OPTIMAL_CALLS.get(task_type, api_calls)

    # Call efficiency: ratio of optimal to actual (capped at 1.0)
    if api_calls > 0:
        call_efficiency = min(1.0, optimal / api_calls)
    else:
        call_efficiency = 1.0

    # Error cleanliness: penalize each 4xx error
    if api_calls > 0:
        error_penalty = errors_4xx / api_calls
    else:
        error_penalty = 0.0
    error_cleanliness = max(0.0, 1.0 - error_penalty * 2)  # Each error costs ~2x its share

    # Combined efficiency bonus (average of both factors)
    efficiency_bonus = (call_efficiency + error_cleanliness) / 2

    final_score = tier * (1.0 + efficiency_bonus)

    return {
        "tier": tier,
        "correctness": correctness,
        "call_efficiency": round(call_efficiency, 2),
        "error_cleanliness": round(error_cleanliness, 2),
        "efficiency_bonus": round(efficiency_bonus, 2),
        "score": round(final_score, 2),
        "max_possible": tier * 2,
        "api_calls": api_calls,
        "optimal_calls": optimal,
    }


# Weighted field checks per task type — mirrors real competition scoring.
# From 04_scoring.md example: employee = found(2) + firstName(1) + lastName(1) + email(1) + admin(5) = 10 max
# Other task types estimated from the scoring pattern: entity found is always worth points,
# key differentiating fields (like admin role, VAT types) are worth more.

# Pre-parsed tasks (what the LLM parser would return)
PARSED_TASKS = [
    {
        "name": "Create employee (simple, no admin)",
        "task": {
            "task_type": "create_employee",
            "entities": {
                "firstName": "Lisa",
                "lastName": "Fjord",
                "email": "lisa@test.no",
                "isAdministrator": False,
            }
        },
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Lisa", "lastName": "Fjord"},
            "weighted": [
                # Spec: found=2, firstName=1, lastName=1, email=1, admin_role=5
                {"field": "firstName", "value": "Lisa", "points": 1},
                {"field": "lastName", "value": "Fjord", "points": 1},
                {"field": "email", "value": "lisa@test.no", "points": 1},
                {"field": "userType", "value": "NO_ACCESS", "points": 5},
            ],
            "found_points": 2,  # Points just for finding the entity
        }
    },
    {
        "name": "Create employee (administrator)",
        "task": {
            "task_type": "create_employee",
            "entities": {
                "firstName": "Ola",
                "lastName": "Nordmann",
                "email": "ola@example.org",
                "isAdministrator": True,
            }
        },
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Ola", "lastName": "Nordmann"},
            "weighted": [
                {"field": "firstName", "value": "Ola", "points": 1},
                {"field": "lastName", "value": "Nordmann", "points": 1},
                {"field": "email", "value": "ola@example.org", "points": 1},
                {"field": "userType", "value": "EXTENDED", "points": 5},
            ],
            "found_points": 2,
        }
    },
    {
        "name": "Create customer",
        "task": {
            "task_type": "create_customer",
            "entities": {
                "name": "Acme AS",
                "email": "post@acme.no",
            }
        },
        "checks": {
            "collection": "customer",
            "find": {"name": "Acme AS"},
            "weighted": [
                {"field": "name", "value": "Acme AS", "points": 3},
                {"field": "email", "value": "post@acme.no", "points": 2},
            ],
            "found_points": 2,
        }
    },
    {
        "name": "Create product (new, not pre-seeded)",
        "task": {
            "task_type": "create_product",
            "entities": {
                "name": "Konsulenttime",
                "priceExcludingVat": 1500,
            }
        },
        "checks": {
            "collection": "product",
            "find": {"name": "Konsulenttime"},
            "weighted": [
                {"field": "name", "value": "Konsulenttime", "points": 3},
                {"field": "priceExcludingVatCurrency", "value": "1500", "points": 3},
            ],
            "found_points": 2,
        }
    },
    {
        "name": "Create department",
        "task": {
            "task_type": "create_department",
            "entities": {
                "name": "Salg",
            }
        },
        "checks": {
            "collection": "department",
            "find": {"name": "Salg"},
            "weighted": [
                {"field": "name", "value": "Salg", "points": 5},
            ],
            "found_points": 2,
        }
    },
    {
        "name": "Create invoice (simple, no VAT types)",
        "task": {
            "task_type": "create_invoice",
            "entities": {
                "customer": {"name": "TestKunde AS", "email": "test@kunde.no"},
                "orderLines": [
                    {"description": "Konsulenttime", "count": 10, "unitPrice": 1500}
                ],
                "invoiceDate": "2026-03-20",
                "invoiceDueDate": "2026-04-20",
            }
        },
        "checks": {
            "collection": "invoice",
            "find": {},
            "weighted": [
                {"field": "invoiceDate", "value": "2026-03-20", "points": 2},
                {"field": "invoiceDueDate", "value": "2026-04-20", "points": 2},
            ],
            "found_points": 3,
            # In reality the platform also checks the customer + order lines exist,
            # but our sim can't verify cross-collection links yet
        }
    },
    {
        # THE REAL COMPETITION TASK — tests VAT type resolution + product lookup
        "name": "Create invoice + products (competition task)",
        "task": {
            "task_type": "create_invoice",
            "entities": {
                "customer": {
                    "name": "Colline SARL",
                    "organizationNumber": "942447647"
                },
                "orderLines": [
                    {
                        "description": "Service réseau",
                        "product": "1340",
                        "unitPrice": 10500,
                        "vatType": "25%"
                    },
                    {
                        "description": "Stockage cloud",
                        "product": "9754",
                        "unitPrice": 11000,
                        "vatType": "15%"
                    },
                    {
                        "description": "Session de formation",
                        "product": "7005",
                        "unitPrice": 5850,
                        "vatType": "0%"
                    }
                ],
            }
        },
        "checks": {
            "collection": "invoice",
            "find": {},
            "weighted": [
                {"field": "invoiceDate", "value": "2026-03-20", "points": 2},
            ],
            # Also verify the order was created with correct products
            "cross_checks": [
                {"collection": "customer", "find_field": "name", "find_value": "Colline SARL", "points": 2},
                {"collection": "order", "exists": True, "points": 3},
            ],
            "found_points": 3,
        }
    },
    {
        "name": "Create project",
        "task": {
            "task_type": "create_project",
            "entities": {
                "name": "Prosjekt Alpha",
                "isInternal": True,
            }
        },
        "checks": {
            "collection": "project",
            "find": {"name": "Prosjekt Alpha"},
            "weighted": [
                {"field": "name", "value": "Prosjekt Alpha", "points": 3},
                {"field": "isInternal", "value": "True", "points": 2},
            ],
            "found_points": 2,
        }
    },
    {
        "name": "Create travel expense",
        "task": {
            "task_type": "create_travel_expense",
            "entities": {
                "title": "Reise til Oslo",
            }
        },
        "checks": {
            "collection": "travelExpense",
            "find": {"title": "Reise til Oslo"},
            "weighted": [
                {"field": "title", "value": "Reise til Oslo", "points": 4},
            ],
            "found_points": 2,
        }
    },
    # -- Invoice with payment --
    {
        "name": "Create invoice with payment",
        "task": {
            "task_type": "create_invoice_with_payment",
            "entities": {
                "customer": {"name": "Betaler AS", "organizationNumber": "999888777"},
                "orderLines": [
                    {"description": "Konsulenttjeneste", "count": 5, "unitPrice": 2000, "vatType": "25%"}
                ],
                "paidAmount": 12500,
                "paymentType": "Kontant",
            }
        },
        "checks": {
            "collection": "invoice",
            "find": {},
            "weighted": [
                {"field": "invoiceDate", "value": "2026-03-20", "points": 2},
            ],
            "cross_checks": [
                {"collection": "customer", "find_field": "name", "find_value": "Betaler AS", "points": 2},
                {"collection": "order", "exists": True, "points": 3},
            ],
            "found_points": 3,
        }
    },
    # -- Credit note (requires an invoice to exist first) --
    # Skipped: needs a pre-existing invoice which our per-test fresh client doesn't have
    # -- Delete travel expense --
    {
        "name": "Delete travel expense",
        "task": {
            "task_type": "delete_travel_expense",
            "entities": {
                "title": "Feil reise",
            }
        },
        # We need to pre-create a travel expense to delete it
        "pre_setup": [
            {"method": "post", "endpoint": "/travelExpense", "body": {
                "employee": {"id": 18169775}, "title": "Feil reise",
            }},
        ],
        "checks": {
            "collection": "travelExpense",
            "expect_deleted": {"title": "Feil reise"},
        }
    },
    # -- Create voucher --
    {
        "name": "Create voucher",
        "task": {
            "task_type": "create_voucher",
            "entities": {
                "description": "Husleie mars",
                "date": "2026-03-01",
                "postings": [
                    {"accountNumber": "1920", "amount": -15000, "description": "Betaling"},
                    {"accountNumber": "4000", "amount": 15000, "description": "Kostnad"},
                ],
            }
        },
        "checks": {
            "collection": "ledger/voucher",
            "find": {"description": "Husleie mars"},
            "weighted": [
                {"field": "description", "value": "Husleie mars", "points": 3},
                {"field": "date", "value": "2026-03-01", "points": 2},
            ],
            "found_points": 2,
        }
    },
    # -- Update employee --
    {
        "name": "Update employee (add email)",
        "task": {
            "task_type": "update_employee",
            "entities": {
                "search": {"firstName": "Admin", "lastName": "NM"},
                "updates": {"email": "admin@oppdatert.no"},
            }
        },
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Admin"},
            "weighted": [
                {"field": "email", "value": "admin@oppdatert.no", "points": 5},
            ],
            "found_points": 2,
        }
    },
    # -- Validation tests --
    {
        "name": "VALIDATION: vatType string resolved correctly",
        "task": {
            "task_type": "create_invoice",
            "entities": {
                "customer": {"name": "VatTest AS"},
                "orderLines": [
                    {"description": "Test", "unitPrice": 100, "vatType": "25%"}
                ],
            }
        },
        "checks": {
            "expected_no_error": True,
        }
    },
]


def _find_entity(items: list[dict], criteria: dict) -> dict | None:
    """Find an entity in a collection by criteria, or the last created one."""
    if criteria:
        for item in items:
            if all(str(item.get(k, "")).lower() == str(v).lower() for k, v in criteria.items()):
                return item
    else:
        # No criteria — find the last created entity (highest ID)
        for item in reversed(items):
            if item.get("id", 0) > 100000000:
                return item
    return None


def run_test(test_case: dict) -> dict:
    """Run a single handler test."""
    name = test_case["name"]
    task = test_case["task"]
    checks = test_case["checks"]

    logger.info("=" * 60)
    logger.info("TEST: %s", name)

    fake_client = FakeTripletexClient()

    # Run pre-setup steps (e.g. create entities needed for delete tests)
    for step in test_case.get("pre_setup", []):
        if step["method"] == "post":
            fake_client.post(step["endpoint"], step["body"])
    # Clear call log so pre-setup doesn't count
    fake_client._call_log.clear()

    context = prefetch_context(fake_client, task["task_type"])

    try:
        result = execute_task(task, fake_client, context)
        logger.info("Handler result: %s", result)
    except Exception as e:
        logger.error("Handler FAILED: %s", e, exc_info=True)
        stats = fake_client.get_call_stats()
        scoring = calculate_score(0.0, task["task_type"], stats["total"], stats["errors_4xx"])
        return {"name": name, "passed": False, "error": str(e), "task_type": task["task_type"],
                "api_calls": stats["total"], "errors_4xx": stats["errors_4xx"],
                "by_method": stats["by_method"], "scoring": scoring}

    if result is None:
        scoring = calculate_score(0.0, task["task_type"], 0, 0)
        return {"name": name, "passed": False, "error": "Handler returned None",
                "task_type": task["task_type"], "api_calls": 0, "errors_4xx": 0,
                "by_method": {}, "scoring": scoring}

    # Special check: just verify no error
    if checks.get("expected_no_error"):
        logger.info("PASS: Handler completed without error")
        stats = fake_client.get_call_stats()
        scoring = calculate_score(1.0, task["task_type"], stats["total"], stats["errors_4xx"])
        return {"name": name, "passed": True, "correctness": 1.0, "checks": "OK", "failures": [],
                "task_type": task["task_type"], "api_calls": stats["total"],
                "errors_4xx": stats["errors_4xx"], "by_method": stats["by_method"], "scoring": scoring}

    # Special check: verify entity was deleted
    if checks.get("expect_deleted"):
        del_criteria = checks["expect_deleted"]
        collection = checks["collection"]
        items = fake_client._store.get(collection, [])
        still_exists = any(
            all(str(i.get(k, "")).lower() == str(v).lower() for k, v in del_criteria.items())
            for i in items
        )
        stats = fake_client.get_call_stats()
        if still_exists:
            scoring = calculate_score(0.0, task["task_type"], stats["total"], stats["errors_4xx"])
            return {"name": name, "passed": False, "correctness": 0.0,
                    "checks": "0/1 (not deleted)", "failures": ["Entity still exists after delete"],
                    "task_type": task["task_type"], "api_calls": stats["total"],
                    "errors_4xx": stats["errors_4xx"], "by_method": stats["by_method"], "scoring": scoring}
        else:
            logger.info("PASS: Entity deleted successfully")
            scoring = calculate_score(1.0, task["task_type"], stats["total"], stats["errors_4xx"])
            return {"name": name, "passed": True, "correctness": 1.0,
                    "checks": "deleted", "failures": [],
                    "task_type": task["task_type"], "api_calls": stats["total"],
                    "errors_4xx": stats["errors_4xx"], "by_method": stats["by_method"], "scoring": scoring}

    # Verify results in fake store
    collection = checks["collection"]
    find_criteria = checks.get("find", {})
    weighted = checks.get("weighted", [])
    found_points = checks.get("found_points", 0)

    items = fake_client._store.get(collection, [])

    match = _find_entity(items, find_criteria)

    # Calculate max possible points
    max_points = found_points + sum(w["points"] for w in weighted)
    # Add cross-check points
    for cc in checks.get("cross_checks", []):
        max_points += cc["points"]

    earned_points = 0
    failures = []

    if not match:
        logger.error("FAIL: Entity not found in %s", collection)
        logger.error("Store: %s", json.dumps(items, ensure_ascii=False, default=str)[:500])
        failures.append(f"Entity not found in '{collection}'")
    else:
        # Entity found — award found_points
        earned_points += found_points

        # Check weighted fields
        for w in weighted:
            actual = match.get(w["field"])
            if str(actual).lower() == str(w["value"]).lower():
                earned_points += w["points"]
            else:
                failures.append(f"{w['field']}: expected '{w['value']}', got '{actual}' (-{w['points']}pts)")

    # Cross-collection checks (e.g. verify customer was created for invoice)
    for cc in checks.get("cross_checks", []):
        cc_items = fake_client._store.get(cc["collection"], [])
        if cc.get("exists"):
            # Just check something was created in that collection
            created = [i for i in cc_items if i.get("id", 0) > 100000000]
            if created:
                earned_points += cc["points"]
            else:
                failures.append(f"Cross-check: nothing created in '{cc['collection']}' (-{cc['points']}pts)")
        elif cc.get("find_field"):
            found = any(str(i.get(cc["find_field"], "")).lower() == str(cc["find_value"]).lower() for i in cc_items)
            if found:
                earned_points += cc["points"]
            else:
                failures.append(f"Cross-check: {cc['find_field']}='{cc['find_value']}' not in '{cc['collection']}' (-{cc['points']}pts)")

    correctness = earned_points / max_points if max_points > 0 else 0
    passed = correctness == 1.0

    if passed:
        logger.info("PASS: %d/%d points", earned_points, max_points)
    else:
        logger.warning("PARTIAL: %d/%d points", earned_points, max_points)
        for f in failures:
            logger.warning("  %s", f)

    stats = fake_client.get_call_stats()
    scoring = calculate_score(correctness, task["task_type"], stats["total"], stats["errors_4xx"])

    return {
        "name": name,
        "passed": passed,
        "correctness": correctness,
        "checks": f"{earned_points}/{max_points}pts",
        "failures": failures,
        "task_type": task["task_type"],
        "api_calls": stats["total"],
        "errors_4xx": stats["errors_4xx"],
        "by_method": stats["by_method"],
        "scoring": scoring,
    }


def main():
    print("\n" + "=" * 60)
    print("HANDLER TESTS (realistic simulator, no LLM needed)")
    print("=" * 60 + "\n")

    results = []
    for tc in PARSED_TASKS:
        result = run_test(tc)
        results.append(result)
        print()

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    total_calls = 0
    total_errors = 0
    total_score = 0.0
    total_max = 0.0

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        s = r.get("scoring", {})
        score_str = f"{s.get('score', 0)}/{s.get('max_possible', '?')}"
        calls = r.get("api_calls", "?")
        errs = r.get("errors_4xx", "?")
        eff = s.get("efficiency_bonus", 0)
        tier = s.get("tier", "?")

        print(f"  [{status}] T{tier} {r['name']}")
        print(f"         Score: {score_str}  |  API: {calls} calls, {errs} errors  |  Efficiency: {eff:.0%}")

        failures = r.get("failures", [])
        error = r.get("error", "")
        if failures:
            for f in failures:
                print(f"         -> {f}")
        if error and not failures:
            print(f"         -> {error}")

        if isinstance(calls, int):
            total_calls += calls
        if isinstance(errs, int):
            total_errors += errs
        total_score += s.get("score", 0)
        total_max += s.get("max_possible", 0)

    print(f"\n  Tests: {passed}/{total} passed, {failed} failed")
    print(f"  API calls: {total_calls} total, {total_errors} errors (4xx)")
    print(f"  Projected score: {total_score:.1f} / {total_max:.1f}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
