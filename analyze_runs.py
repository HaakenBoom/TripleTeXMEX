"""Enhanced run log analyzer — correctness diagnostics, efficiency metrics, score estimation, and optimization hints."""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# Minimum API calls needed per task type (theoretical best — no errors, no retries)
# Based on what the deterministic handlers actually need
MIN_CALLS = {
    "create_employee": 2,       # GET /department (or POST), POST /employee
    "create_customer": 1,       # POST /customer
    "update_customer": 2,       # GET /customer, PUT /customer/{id}
    "create_product": 1,        # POST /product
    "update_employee": 2,       # GET /employee, PUT /employee/{id}
    "update_employee_role": 2,  # GET /employee, PUT /employee/{id}
    "create_project": 3,        # GET /customer, POST /employee (if needed), POST /project
    "create_department": 1,     # POST /department
    "create_invoice": 5,        # GET /vatType, GET /whoAmI, GET+PUT /ledger/account, GET /customer, POST /order, POST /invoice
    "create_invoice_with_payment": 7,  # above + GET /paymentTypeOut, PUT /invoice/:payment
    "create_credit_note": 8,    # create invoice first + PUT /:createCreditNote
    "create_travel_expense": 2, # POST /employee (if needed), POST /travelExpense
    "delete_travel_expense": 2, # GET /travelExpense, DELETE
    "create_voucher": 2,        # GET /ledger/account, POST /ledger/voucher
    "delete_voucher": 2,        # GET /ledger/voucher, DELETE
    "log_timesheet_hours": 5,   # GET /employee, GET /project, GET /activity, POST /timesheet/entry
    "reverse_invoice_payment": 5,  # GET /customer, GET /invoice, GET /paymentType, PUT /:payment
    "create_dimension_voucher": 5,  # POST /dimension, POST /dimensionValue(s), GET /account, POST /voucher
}

# Tier classification based on docs
TASK_TIERS = {
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
    "delete_voucher": 2,
    "log_timesheet_hours": 2,
    "reverse_invoice_payment": 2,
    "create_dimension_voucher": 3,
    "unknown": "?",
}

# Max possible score per tier
MAX_SCORE = {1: 2.0, 2: 4.0, 3: 6.0}

# Keywords that suggest specific task types — used to detect parser misclassification
_TASK_SIGNALS = {
    "reverse_invoice_payment": [
        "reverse", "reverser", "tilbakefør", "revertir", "reverter", "stornieren",
        "payment returned", "betaling returnert", "undo payment", "angre betaling",
    ],
    "create_credit_note": [
        "credit note", "kreditnota", "nota de crédito", "note de crédit", "Gutschrift",
        "kreditere", "creditar",
    ],
    "log_timesheet_hours": [
        "log hours", "timer", "timesheet", "registrer timer", "horas", "heures", "Stunden",
    ],
    "delete_voucher": ["slett bilag", "delete voucher", "eliminar", "supprimer", "löschen"],
    "delete_travel_expense": ["slett reise", "delete travel", "eliminar viaje"],
}


def _estimate_score(tier: int | str, total_calls: int, errors_4xx: int,
                     min_calls: int | str, issues: list[str]) -> dict:
    """Estimate the score based on what we can infer from the run log.

    Returns {estimated_correctness, estimated_score, max_possible, reasoning}.
    Scoring: score = correctness * tier_multiplier * efficiency_bonus
    - correctness: 0-1 (field-by-field)
    - efficiency_bonus: 1.0-2.0 (only if correctness=1.0)
    """
    if not isinstance(tier, int):
        return {"estimated_score": "?", "max_possible": "?", "reasoning": "Unknown tier"}

    max_possible = MAX_SCORE[tier]

    # Check for fatal issues (score = 0)
    fatal = [i for i in issues if i.startswith("FATAL")]
    if fatal:
        return {"estimated_score": 0.0, "max_possible": max_possible,
                "reasoning": f"Fatal: {fatal[0]}"}

    # Check for correctness issues
    correctness_issues = [i for i in issues if i.startswith("CORRECTNESS") or i.startswith("MISCLASS")]
    if correctness_issues:
        # Rough estimate: each correctness issue costs ~20-40% of points
        est_correctness = max(0.0, 1.0 - 0.3 * len(correctness_issues))
        est_score = est_correctness * tier
        return {"estimated_score": round(est_score, 1), "max_possible": max_possible,
                "reasoning": f"~{len(correctness_issues)} correctness issues → est. {est_correctness:.0%}"}

    # If no issues detected, assume correctness ~1.0 and estimate efficiency
    if isinstance(min_calls, int) and min_calls > 0:
        call_ratio = min_calls / max(total_calls, 1)  # 1.0 = perfect, lower = worse
        error_penalty = min(1.0, errors_4xx * 0.1)  # Each error costs ~10%
        efficiency_bonus = 1.0 + max(0.0, call_ratio - error_penalty)  # 1.0-2.0
        efficiency_bonus = min(2.0, efficiency_bonus)
        est_score = tier * efficiency_bonus
    else:
        est_score = float(tier)  # Base score, no efficiency estimate
        efficiency_bonus = 1.0

    return {"estimated_score": round(est_score, 1), "max_possible": max_possible,
            "reasoning": f"Looks correct, efficiency bonus ~{efficiency_bonus:.1f}x"}


def _detect_misclassification(prompt: str, parsed_task_type: str) -> str | None:
    """Check if the parser likely picked the wrong task type."""
    prompt_lower = prompt.lower()
    for expected_type, signals in _TASK_SIGNALS.items():
        if expected_type == parsed_task_type:
            continue
        matches = [s for s in signals if s in prompt_lower]
        if len(matches) >= 2 or (len(matches) >= 1 and parsed_task_type in ("unknown", "create_invoice_with_payment", "create_invoice")):
            return f"MISCLASS: Prompt matches '{expected_type}' (signals: {matches[:3]}) but parsed as '{parsed_task_type}'"
    return None


def _check_prompt_field_coverage(prompt: str, entities: dict, task_type: str) -> list[str]:
    """Compare what the prompt mentions vs what the parser extracted.
    Returns list of potentially missed fields."""
    missed = []
    prompt_lower = prompt.lower()

    # Check for email in prompt but not in entities
    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', prompt)
    if email_match:
        found_email = email_match.group()
        all_values = json.dumps(entities)
        if found_email.lower() not in all_values.lower():
            missed.append(f"MISSED_FIELD: Email '{found_email}' in prompt but not in parsed entities")

    # Check for phone numbers
    phone_match = re.search(r'(?:\+\d{1,3}\s?)?\d[\d\s-]{7,}', prompt)
    if phone_match:
        phone = re.sub(r'[\s-]', '', phone_match.group())
        if len(phone) >= 8:
            all_values = json.dumps(entities)
            if phone not in all_values.replace(" ", "").replace("-", ""):
                missed.append(f"MISSED_FIELD: Phone '{phone_match.group().strip()}' in prompt but not in entities")

    # Check for dates (YYYY-MM-DD or DD.MM.YYYY or DD/MM/YYYY)
    date_patterns = re.findall(r'\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}', prompt)
    if date_patterns:
        all_values = json.dumps(entities)
        for dp in date_patterns:
            # Normalize to check
            if dp not in all_values:
                missed.append(f"MISSED_FIELD: Date '{dp}' in prompt but may not be in entities")

    # Check for org numbers (8-9 digit sequences common in Norway)
    org_matches = re.findall(r'\b\d{9}\b', prompt)
    if org_matches:
        all_values = json.dumps(entities)
        for org in org_matches:
            if org not in all_values:
                missed.append(f"MISSED_FIELD: Org number '{org}' in prompt but not in entities")

    # Check for boolean flags mentioned in prompt
    admin_words = ["administrator", "kontoadministrator", "admin", "administrador", "administrateur"]
    if any(w in prompt_lower for w in admin_words):
        if not entities.get("isAdministrator") and not (entities.get("updates", {}) or {}).get("isAdministrator"):
            missed.append("MISSED_FIELD: Admin role mentioned in prompt but isAdministrator not set")

    return missed


def analyze_run(data: dict) -> dict:
    """Extract deep insights from a single run log."""
    api_calls = data.get("api_calls", [])
    errors = [c for c in api_calls if c.get("status", 0) >= 400]
    task_type = data.get("parsed_task", {}).get("task_type", "unknown")
    entities = data.get("parsed_task", {}).get("entities", {})
    prompt = data.get("prompt", "")

    # Categorize API calls
    prefetch_calls = []  # GET calls before first POST/PUT/DELETE
    action_calls = []    # POST/PUT/DELETE calls
    wasted_calls = []    # Calls that resulted in errors
    first_mutation = False

    for c in api_calls:
        method = c.get("method", "")
        status = c.get("status", 0)
        endpoint = c.get("url", "").split("/v2")[-1].split("?")[0]

        if method in ("POST", "PUT", "DELETE"):
            first_mutation = True
        if not first_mutation and method == "GET":
            prefetch_calls.append(endpoint)
        if method in ("POST", "PUT", "DELETE"):
            action_calls.append(f"{method} {endpoint} → {status}")

        if status >= 400:
            msg = _extract_error_msg(c)
            wasted_calls.append(f"{method} {endpoint} → {status}: {msg}")

    # What was created/modified successfully
    created = []
    for c in api_calls:
        if c.get("method") in ("POST", "PUT") and c.get("status") in (200, 201):
            rb = c.get("response_body", {})
            val = rb.get("value", {}) if isinstance(rb, dict) else {}
            endpoint = c.get("url", "").split("/v2")[-1].split("?")[0]
            if c["method"] == "POST":
                created.append({
                    "endpoint": endpoint,
                    "id": val.get("id"),
                    "key_fields": _extract_key_fields(endpoint, val),
                })

    # Invoice details
    invoice_info = _extract_invoice_info(api_calls)

    # Order line details (what we actually sent to the API)
    order_lines_sent = _extract_order_lines_sent(api_calls)

    # Efficiency analysis
    min_calls = MIN_CALLS.get(task_type, "?")
    actual_calls = len(api_calls)
    efficiency_ratio = f"{actual_calls}/{min_calls}" if isinstance(min_calls, int) else f"{actual_calls}/?"

    # Tier info
    tier = TASK_TIERS.get(task_type, "?")
    max_score = MAX_SCORE.get(tier, "?") if isinstance(tier, int) else "?"

    # Detect specific issues
    issues = _detect_issues(data, api_calls, task_type, entities, created)

    # Detect parser misclassification
    misclass = _detect_misclassification(prompt, task_type)
    if misclass:
        issues.insert(0, misclass)

    # Check prompt field coverage
    if isinstance(entities, dict):
        missed = _check_prompt_field_coverage(prompt, entities, task_type)
        issues.extend(missed)

    # Estimate score
    score_est = _estimate_score(tier, actual_calls, len(errors), min_calls, issues)

    return {
        "task_type": task_type,
        "tier": tier,
        "max_score": max_score,
        "prompt": prompt[:300],
        "prompt_fingerprint": data.get("prompt_fingerprint", ""),
        "prompt_language": data.get("prompt_language", ""),
        "path": data.get("path_taken", "?"),
        "result": (data.get("result") or "")[:200],
        "elapsed": data.get("elapsed_seconds", 0),
        "total_calls": actual_calls,
        "errors_4xx": len(errors),
        "efficiency": efficiency_ratio,
        "prefetch_calls": prefetch_calls,
        "action_calls": action_calls,
        "wasted_calls": wasted_calls,
        "created": created,
        "invoice_info": invoice_info,
        "order_lines_sent": order_lines_sent,
        "issues": issues,
        "entities": entities if isinstance(entities, dict) else {},
        "score_estimate": score_est,
        # Raw data reference for root cause analysis
        "_raw": {
            "deterministic_error": data.get("deterministic_error"),
            "handler_returned_none": data.get("handler_returned_none", False),
            "phase_times": data.get("phase_times", {}),
            "timestamp": data.get("timestamp", ""),
        },
    }


def _extract_error_msg(call: dict) -> str:
    rb = call.get("response_body")
    if rb is None:
        return "(no response body)"
    if not isinstance(rb, dict):
        return str(rb)[:100]
    vm = rb.get("validationMessages", [])
    if vm:
        return "; ".join(f"{v.get('field', '?')}: {v.get('message', '')}" for v in vm)[:150]
    msg = rb.get("message")
    if msg is None:
        msg = str(rb)
    return str(msg)[:100]


def _extract_key_fields(endpoint: str, val: dict) -> dict:
    """Extract the most important fields from a created entity for correctness checking."""
    if not isinstance(val, dict):
        return {}
    fields = {}
    if "/employee" in endpoint:
        for k in ("firstName", "lastName", "email", "userType", "phoneNumberMobile", "dateOfBirth"):
            if val.get(k):
                fields[k] = val[k]
    elif "/customer" in endpoint:
        for k in ("name", "organizationNumber", "email", "phoneNumber"):
            if val.get(k):
                fields[k] = val[k]
        if val.get("postalAddress"):
            fields["postalAddress"] = val["postalAddress"]
    elif "/product" in endpoint:
        for k in ("name", "number", "priceExcludingVatCurrency", "vatType"):
            if val.get(k):
                fields[k] = val[k]
    elif "/invoice" in endpoint:
        for k in ("invoiceNumber", "invoiceDate", "amount", "amountExcludingVat", "amountOutstanding", "isCreditNote"):
            if val.get(k) is not None:
                fields[k] = val[k]
    elif "/order" in endpoint:
        for k in ("customerName", "orderDate", "isPrioritizeAmountsIncludingVat"):
            if val.get(k) is not None:
                fields[k] = val[k]
    elif "/project" in endpoint:
        for k in ("name", "number", "startDate", "isInternal"):
            if val.get(k) is not None:
                fields[k] = val[k]
    elif "/department" in endpoint:
        for k in ("name", "departmentNumber"):
            if val.get(k) is not None:
                fields[k] = val[k]
    elif "/travelExpense" in endpoint:
        for k in ("title", "departureDate", "returnDate"):
            if val.get(k) is not None:
                fields[k] = val[k]
    return fields


def _extract_invoice_info(api_calls: list) -> dict | None:
    """Extract detailed invoice info including order lines from the response."""
    for c in api_calls:
        if "/invoice" in c.get("url", "") and c.get("method") == "POST" and c.get("status") in (200, 201):
            val = c.get("response_body", {}).get("value", {})
            info = {
                "id": val.get("id"),
                "invoiceNumber": val.get("invoiceNumber"),
                "invoiceDate": val.get("invoiceDate"),
                "amount": val.get("amount"),
                "amountExclVat": val.get("amountExcludingVat"),
                "amountExclVatCurrency": val.get("amountExcludingVatCurrency"),
                "amountOutstanding": val.get("amountOutstanding"),
                "isCredited": val.get("isCredited"),
                "isCreditNote": val.get("isCreditNote"),
                "orderLineCount": len(val.get("orderLines", [])),
            }
            return info
    return None


def _extract_order_lines_sent(api_calls: list) -> list | None:
    """Extract what order lines we actually sent to the API."""
    for c in api_calls:
        if "/order" in c.get("url", "") and c.get("method") == "POST" and c.get("status") in (200, 201):
            body = c.get("request_body", {})
            if isinstance(body, dict) and "orderLines" in body:
                lines = []
                for ol in body["orderLines"]:
                    line = {}
                    if ol.get("description"):
                        line["description"] = ol["description"]
                    if ol.get("product"):
                        line["product_id"] = ol["product"].get("id")
                    if ol.get("count") is not None:
                        line["count"] = ol["count"]
                    if ol.get("unitPriceExcludingVatCurrency") is not None:
                        line["priceExclVat"] = ol["unitPriceExcludingVatCurrency"]
                    if ol.get("unitPriceIncludingVatCurrency") is not None:
                        line["priceInclVat"] = ol["unitPriceIncludingVatCurrency"]
                    if ol.get("vatType"):
                        line["vatType_id"] = ol["vatType"].get("id")
                    lines.append(line)
                return lines
    return None


def _detect_issues(data: dict, api_calls: list, task_type: str, entities: dict, created: list) -> list[str]:
    """Detect specific correctness and efficiency issues."""
    issues = []

    # 1. Country format error (wasted call)
    for c in api_calls:
        if c.get("status") == 422:
            msg = _extract_error_msg(c)
            if "country" in msg.lower():
                issues.append("EFFICIENCY: Country format error → wasted API call (string instead of {id: N})")
            if "startDate" in msg:
                issues.append("EFFICIENCY: Missing startDate → wasted API call")
            if "quantity" in msg.lower():
                issues.append("BUG: Used 'quantity' instead of 'count' in order lines")
            if "vatType" in msg.lower() or "vattype" in msg.lower():
                issues.append("BUG: vatType format error (likely string instead of {id: N})")

    # 2. All 403s — dead session
    statuses = [c.get("status", 0) for c in api_calls]
    if statuses and all(s == 403 for s in statuses):
        issues.append("FATAL: All API calls returned 403 — invalid session token")

    # 3. Max iterations reached
    if data.get("result", "").startswith("Max iterations"):
        issues.append("FATAL: Agent loop hit max iterations without completing")

    # 4. Rate limit crash
    if data.get("error") and "rate_limit" in str(data.get("error", "")).lower():
        issues.append("FATAL: Crashed on 429 rate limit")

    # 5. Invoice amount mismatch — check if order line prices match prompt
    if task_type == "create_invoice" and isinstance(entities, dict):
        prompt_lines = entities.get("orderLines", [])
        for c in api_calls:
            if "/order" in c.get("url", "") and c.get("method") == "POST" and c.get("status") in (200, 201):
                sent_lines = c.get("request_body", {}).get("orderLines", [])
                if len(sent_lines) != len(prompt_lines):
                    issues.append(f"CORRECTNESS: Sent {len(sent_lines)} order lines, prompt had {len(prompt_lines)}")

    # 6. Missing sendToCustomer when prompt implies sending
    if task_type == "create_invoice" and isinstance(entities, dict):
        prompt = data.get("prompt", "").lower()
        send_words = ["send", "envie", "enviar", "envoyer", "senden", "sende"]
        if any(w in prompt for w in send_words) and not entities.get("sendToCustomer"):
            issues.append("CORRECTNESS: Prompt says 'send' but sendToCustomer not set to true by parser")

    # 7. Prefetch waste — calls not needed for this task type
    prefetch_endpoints = set()
    first_mutation = False
    for c in api_calls:
        if c.get("method") in ("POST", "PUT", "DELETE"):
            first_mutation = True
        if not first_mutation and c.get("method") == "GET":
            ep = c.get("url", "").split("/v2")[-1].split("?")[0]
            prefetch_endpoints.add(ep)

    if task_type == "create_customer" and len(prefetch_endpoints) > 0:
        issues.append(f"EFFICIENCY: create_customer needs 0 prefetch calls, did {len(prefetch_endpoints)}: {prefetch_endpoints}")
    if task_type == "create_employee" and "/ledger/vatType" in prefetch_endpoints:
        issues.append("EFFICIENCY: create_employee doesn't need VAT types")

    # 8. Payment not registered
    if task_type == "create_invoice_with_payment":
        invoice = _extract_invoice_info(api_calls)
        if invoice and invoice.get("amount") and invoice.get("amountOutstanding"):
            if invoice["amount"] == invoice["amountOutstanding"]:
                issues.append("CORRECTNESS: Invoice created but payment NOT registered (amountOutstanding == amount)")

    # 9. Duplicate entity creation
    endpoint_counts = defaultdict(int)
    for c in api_calls:
        if c.get("method") == "POST" and c.get("status") in (200, 201):
            ep = c.get("url", "").split("/v2")[-1].split("?")[0]
            endpoint_counts[ep] += 1
    for ep, count in endpoint_counts.items():
        if count > 1 and ep in ("/customer", "/employee"):
            issues.append(f"EFFICIENCY: Created {count}x {ep} — possible duplicate")

    return issues


def print_run(f_name: str, r: dict, verbose: bool = False):
    """Print a single run analysis."""
    tier_str = f"T{r['tier']}" if isinstance(r['tier'], int) else "T?"
    max_str = f"max={r['max_score']}" if isinstance(r['max_score'], float) else ""
    se = r.get("score_estimate", {})
    score_str = f"~{se['estimated_score']}" if se.get("estimated_score") != "?" else "?"

    print(f"{'─' * 70}")
    print(f"  {f_name}")
    print(f"   Task: {r['task_type']} | {tier_str} {max_str} | Est: {score_str}/{se.get('max_possible', '?')} | Path: {r['path']} | Time: {r['elapsed']}s")
    print(f"   Calls: {r['total_calls']} (min: {r['efficiency']}) | Errors: {r['errors_4xx']} | {se.get('reasoning', '')}")
    print(f"   Prompt: {r['prompt'][:120]}...")

    if r["issues"]:
        print(f"   Issues:")
        for issue in r["issues"]:
            prefix = issue.split(":")[0]
            icon = {"FATAL": "!!", "CORRECTNESS": "!!", "MISCLASS": "!!", "EFFICIENCY": "~", "MISSED_FIELD": "?", "BUG": "!!"}.get(prefix, " ")
            print(f"     [{icon}] {issue}")

    if r["created"]:
        print(f"   Created:")
        for c in r["created"]:
            fields = ", ".join(f"{k}={v}" for k, v in list(c.get("key_fields", {}).items())[:5])
            print(f"     {c['endpoint']} id={c['id']} | {fields}")

    if r["invoice_info"]:
        inv = r["invoice_info"]
        print(f"   Invoice #{inv.get('invoiceNumber')}: amount={inv.get('amount')} exclVat={inv.get('amountExclVat')} outstanding={inv.get('amountOutstanding')} lines={inv.get('orderLineCount')}")

    if r["order_lines_sent"]:
        print(f"   Order lines sent:")
        for i, ol in enumerate(r["order_lines_sent"]):
            print(f"     [{i}] {ol}")

    if r["wasted_calls"] and verbose:
        print(f"   Wasted calls:")
        for w in r["wasted_calls"]:
            print(f"     {w}")

    if verbose:
        print(f"   Prefetch: {r['prefetch_calls']}")
        print(f"   Actions: {r['action_calls']}")

    print()


def classify_root_cause(r: dict) -> str:
    """Classify the root cause of a failure into one actionable category.

    Categories (in diagnosis order):
    - PARSE: Parser picked wrong task_type or missed fields
    - PREFETCH: Needed context wasn't fetched (wrong prefetch set)
    - HANDLER_BUG: Deterministic handler threw an exception
    - HANDLER_MISSING: No handler for this task type (fell to agent loop)
    - API_FORMAT: Sent wrong field format (vatType string, quantity vs count)
    - API_ENDPOINT: Used wrong endpoint or method
    - API_PREREQ: Missing prerequisite (bank account, payment type)
    - AGENT_LOOP: Agent loop flailing (multiple errors, max iterations)
    - EFFICIENCY: Task completed but used too many calls
    - OK: No issues detected
    """
    issues = r.get("issues", [])
    path = r.get("path", "")

    # Fatal first
    for i in issues:
        if "403" in i:
            return "AUTH"
        if "Max iterations" in i:
            return "AGENT_LOOP"

    # Parse errors
    for i in issues:
        if i.startswith("MISCLASS"):
            return "PARSE"
    missed = [i for i in issues if i.startswith("MISSED_FIELD")]
    if missed and any(i.startswith("CORRECTNESS") or i.startswith("FATAL") for i in issues):
        return "PARSE"

    # Handler issues
    if path == "agent_loop":
        # Check WHY we went to agent loop
        det_err = r.get("_raw", {}).get("deterministic_error")
        handler_none = r.get("_raw", {}).get("handler_returned_none")
        task_type = r.get("task_type", "unknown")

        if task_type == "unknown":
            return "PARSE"
        if handler_none:
            return "HANDLER_MISSING"
        if det_err:
            err_lower = str(det_err).lower()
            if "bankkontonummer" in err_lower or "bank account" in err_lower:
                return "API_PREREQ"
            if "vattype" in err_lower or "vat type" in err_lower:
                return "API_FORMAT"
            return "HANDLER_BUG"
        return "HANDLER_MISSING"

    # API format bugs
    for i in issues:
        if i.startswith("BUG"):
            return "API_FORMAT"

    # Correctness issues
    for i in issues:
        if i.startswith("CORRECTNESS"):
            return "CORRECTNESS"

    # Efficiency issues
    efficiency_issues = [i for i in issues if i.startswith("EFFICIENCY")]
    if efficiency_issues:
        return "EFFICIENCY"

    if not issues:
        return "OK"

    return "OTHER"


def compute_impact_score(r: dict) -> float:
    """Compute how many points we're leaving on the table for this run.

    impact = max_possible_score - estimated_score
    Higher impact = more points to gain by fixing this task.
    """
    se = r.get("score_estimate", {})
    est = se.get("estimated_score", 0)
    max_p = se.get("max_possible", 0)
    if not isinstance(est, (int, float)) or not isinstance(max_p, (int, float)):
        return 0.0
    return max_p - est


def print_summary(all_results: list[dict]):
    """Print aggregate summary with optimization recommendations."""
    print(f"\n{'═' * 70}")
    print(f"SUMMARY ({len(all_results)} runs)")
    print(f"{'═' * 70}\n")

    # Group by task type
    by_type = defaultdict(list)
    for r in all_results:
        by_type[r["task_type"]].append(r)

    # Best score per task type (mimics leaderboard logic)
    print("Per-task breakdown (best estimated score kept):")
    print(f"{'Task Type':<30} {'Tier':>4} {'Runs':>4} {'Best Est':>8} {'Max':>5} {'Errors':>6} {'Avg Calls':>9} {'Min':>5} {'Issues':>6}")
    print("─" * 80)

    total_issues = []
    total_best = 0.0
    total_max = 0.0
    task_coverage = set()
    for task_type in sorted(by_type.keys()):
        runs = by_type[task_type]
        tier = TASK_TIERS.get(task_type, "?")
        avg_calls = sum(r["total_calls"] for r in runs) / len(runs)
        total_errors = sum(r["errors_4xx"] for r in runs)
        min_c = MIN_CALLS.get(task_type, "?")
        issue_count = sum(len(r["issues"]) for r in runs)

        # Best estimated score for this task type
        best_est = 0.0
        for r in runs:
            se = r.get("score_estimate", {})
            est = se.get("estimated_score", 0)
            if isinstance(est, (int, float)):
                best_est = max(best_est, est)
        max_s = MAX_SCORE.get(tier, 0) if isinstance(tier, int) else 0

        task_coverage.add(task_type)
        total_best += best_est
        total_max += max_s if isinstance(max_s, (int, float)) else 0

        print(f"{task_type:<30} T{tier:>3} {len(runs):>4} {best_est:>8.1f} {max_s:>5} {total_errors:>6} {avg_calls:>9.1f} {str(min_c):>5} {issue_count:>6}")
        for r in runs:
            total_issues.extend(r["issues"])

    print(f"{'─' * 80}")
    print(f"{'TOTAL':<30} {'':>4} {len(all_results):>4} {total_best:>8.1f} {total_max:>5.0f}")

    # Task coverage vs known 30 tasks
    all_known = set(TASK_TIERS.keys()) - {"unknown"}
    untested = all_known - task_coverage
    if untested:
        missing_by_tier = defaultdict(list)
        for t in sorted(untested):
            tier = TASK_TIERS.get(t, "?")
            missing_by_tier[tier].append(t)
        print(f"\nUntested task types ({len(untested)}/{len(all_known)}):")
        for tier in sorted(missing_by_tier.keys()):
            tasks = missing_by_tier[tier]
            max_each = MAX_SCORE.get(tier, "?")
            print(f"  T{tier} (max {max_each} each): {', '.join(tasks)}")

    # Issue frequency — group by category
    if total_issues:
        print(f"\nIssue frequency:")
        by_category = defaultdict(list)
        for issue in total_issues:
            cat = issue.split(":")[0]
            by_category[cat].append(issue)
        for cat in ["FATAL", "MISCLASS", "CORRECTNESS", "BUG", "MISSED_FIELD", "EFFICIENCY"]:
            items = by_category.get(cat, [])
            if items:
                print(f"  {cat} ({len(items)}x):")
                issue_counts = defaultdict(int)
                for i in items:
                    issue_counts[i] += 1
                for issue, count in sorted(issue_counts.items(), key=lambda x: -x[1])[:5]:
                    print(f"    [{count}x] {issue}")

    # ── ROOT CAUSE ANALYSIS ──
    print(f"\n{'─' * 70}")
    print("ROOT CAUSE BREAKDOWN (why did we lose points?)")
    print("─" * 70)
    cause_counts = defaultdict(list)
    for r in all_results:
        cause = classify_root_cause(r)
        cause_counts[cause].append(r)

    cause_labels = {
        "PARSE": "Parser error (wrong task type or missed fields)",
        "PREFETCH": "Prefetch gap (needed context not fetched)",
        "HANDLER_BUG": "Handler exception (deterministic handler crashed)",
        "HANDLER_MISSING": "No handler (fell to agent loop)",
        "API_FORMAT": "API format error (wrong field format)",
        "API_PREREQ": "Missing prerequisite (bank account, etc)",
        "API_ENDPOINT": "Wrong endpoint or method",
        "AGENT_LOOP": "Agent loop failure (max iterations, flailing)",
        "CORRECTNESS": "Partial correctness (some fields wrong)",
        "EFFICIENCY": "Correct but inefficient (too many calls)",
        "AUTH": "Authentication failure (403)",
        "OK": "No issues detected",
        "OTHER": "Unclassified",
    }

    # Sort by total impact (points left on table)
    for cause in sorted(cause_counts.keys(),
                        key=lambda c: sum(compute_impact_score(r) for r in cause_counts[c]),
                        reverse=True):
        runs = cause_counts[cause]
        impact = sum(compute_impact_score(r) for r in runs)
        label = cause_labels.get(cause, cause)
        tasks = sorted(set(r["task_type"] for r in runs))
        print(f"  {cause:<18} {len(runs):>3} runs  {impact:>5.1f} pts at stake  {label}")
        if cause != "OK":
            for t in tasks[:5]:
                print(f"    - {t}")

    # ── PRIORITIZED FIX LIST ──
    print(f"\n{'─' * 70}")
    print("PRIORITIZED FIX LIST (highest impact first)")
    print("─" * 70)

    # Build fix candidates: (impact, task_type, root_cause, description)
    fixes = []
    for r in all_results:
        cause = classify_root_cause(r)
        if cause == "OK":
            continue
        impact = compute_impact_score(r)
        tier = r.get("tier", 0)
        if not isinstance(tier, int):
            tier = 0
        fixes.append((impact, tier, r["task_type"], cause, r.get("issues", [])))

    # Deduplicate: keep highest-impact instance per (task_type, cause)
    seen = set()
    unique_fixes = []
    for impact, tier, task_type, cause, issues in sorted(fixes, key=lambda x: -x[0]):
        key = (task_type, cause)
        if key not in seen:
            seen.add(key)
            unique_fixes.append((impact, tier, task_type, cause, issues))

    for i, (impact, tier, task_type, cause, issues) in enumerate(unique_fixes[:10], 1):
        print(f"  {i}. [{cause}] {task_type} (T{tier}) — {impact:.1f} pts recoverable")
        for iss in issues[:2]:
            print(f"     {iss}")

    # ── PHASE TIMING ──
    phase_data = defaultdict(list)
    for r in all_results:
        raw = r.get("_raw", {})
        pt = raw.get("phase_times", {})
        for phase, t in pt.items():
            phase_data[phase].append(t)

    if phase_data:
        print(f"\n{'─' * 70}")
        print("PHASE TIMING (seconds)")
        print("─" * 70)
        for phase in ["parse", "prefetch", "handler", "repair", "agent_loop"]:
            times = phase_data.get(phase, [])
            if times:
                avg = sum(times) / len(times)
                mx = max(times)
                print(f"  {phase:<15} avg={avg:.1f}s  max={mx:.1f}s  n={len(times)}")

    # Efficiency recommendations
    print(f"\nEfficiency optimization opportunities:")
    any_eff = False
    for task_type, runs in sorted(by_type.items()):
        min_c = MIN_CALLS.get(task_type)
        if not isinstance(min_c, int):
            continue
        avg_calls = sum(r["total_calls"] for r in runs) / len(runs)
        if avg_calls > min_c * 1.5:
            overhead = avg_calls - min_c
            print(f"  {task_type}: averaging {avg_calls:.0f} calls vs {min_c} minimum (+{overhead:.0f} overhead)")
            any_eff = True
    if not any_eff:
        print("  None — call counts look reasonable")

    # Path distribution
    det = sum(1 for r in all_results if r["path"] == "deterministic")
    rep = sum(1 for r in all_results if r["path"] == "deterministic_repaired")
    agent = sum(1 for r in all_results if r["path"] == "agent_loop")
    other = len(all_results) - det - rep - agent
    print(f"\nPath distribution: deterministic={det}, repaired={rep}, agent_loop={agent}, other={other}")
    if agent > 0:
        print(f"  Agent loop tasks (slower + less efficient — consider adding deterministic handlers):")
        for r in all_results:
            if r["path"] == "agent_loop":
                print(f"    - {r['task_type']}: {r['prompt'][:80]}...")

    # Score optimization advice
    print(f"\nScore optimization priorities:")
    # 1. Fix correctness issues (biggest impact)
    correctness_tasks = set()
    for r in all_results:
        for i in r["issues"]:
            if i.startswith("CORRECTNESS") or i.startswith("MISCLASS") or i.startswith("FATAL"):
                correctness_tasks.add(r["task_type"])
    if correctness_tasks:
        print(f"  1. FIX CORRECTNESS: {', '.join(sorted(correctness_tasks))} — no efficiency bonus until perfect")

    # 2. Higher tier tasks = more points per fix
    high_tier_imperfect = set()
    for r in all_results:
        tier = r.get("tier")
        if isinstance(tier, int) and tier >= 2 and r.get("issues"):
            high_tier_imperfect.add(f"{r['task_type']} (T{tier})")
    if high_tier_imperfect:
        print(f"  2. PRIORITIZE HIGH-TIER: {', '.join(sorted(high_tier_imperfect))} — T2=4pt max, T3=6pt max")

    # 3. Reduce API calls for perfect tasks
    efficient_targets = []
    for task_type, runs in by_type.items():
        min_c = MIN_CALLS.get(task_type)
        if not isinstance(min_c, int):
            continue
        for r in runs:
            if not r["issues"] and r["total_calls"] > min_c + 2:
                efficient_targets.append(f"{task_type} ({r['total_calls']} calls → {min_c} min)")
                break
    if efficient_targets:
        print(f"  3. REDUCE CALLS (for efficiency bonus on perfect tasks): {'; '.join(efficient_targets[:5])}")


def print_trend_analysis(current_results: list[dict], previous_results: list[dict]):
    """Compare current batch against previous runs to detect trends."""
    if not previous_results:
        return

    print(f"\n{'═' * 70}")
    print(f"TREND ANALYSIS (latest {len(current_results)} vs previous {len(previous_results)} runs)")
    print(f"{'═' * 70}\n")

    def _stats(results):
        ok = sum(1 for r in results if classify_root_cause(r) == "OK")
        total_score = sum(
            r.get("score_estimate", {}).get("estimated_score", 0)
            for r in results
            if isinstance(r.get("score_estimate", {}).get("estimated_score", 0), (int, float))
        )
        total_errors = sum(r["errors_4xx"] for r in results)
        avg_calls = sum(r["total_calls"] for r in results) / max(len(results), 1)
        det = sum(1 for r in results if r["path"] == "deterministic")
        return {"ok": ok, "total": len(results), "score": total_score,
                "errors": total_errors, "avg_calls": avg_calls, "deterministic": det}

    curr = _stats(current_results)
    prev = _stats(previous_results)

    def _arrow(curr_val, prev_val, higher_is_better=True):
        if curr_val > prev_val:
            return "+" if higher_is_better else "!"
        elif curr_val < prev_val:
            return "-" if higher_is_better else "+"
        return "="

    success_rate_curr = curr["ok"] / max(curr["total"], 1) * 100
    success_rate_prev = prev["ok"] / max(prev["total"], 1) * 100
    det_rate_curr = curr["deterministic"] / max(curr["total"], 1) * 100
    det_rate_prev = prev["deterministic"] / max(prev["total"], 1) * 100

    print(f"  {'Metric':<25} {'Current':>10} {'Previous':>10} {'Delta':>10}")
    print(f"  {'─' * 60}")
    print(f"  {'Success rate':<25} {success_rate_curr:>9.0f}% {success_rate_prev:>9.0f}% {success_rate_curr - success_rate_prev:>+9.0f}% {_arrow(success_rate_curr, success_rate_prev)}")
    print(f"  {'Total est. score':<25} {curr['score']:>10.1f} {prev['score']:>10.1f} {curr['score'] - prev['score']:>+10.1f} {_arrow(curr['score'], prev['score'])}")
    print(f"  {'Total 4xx errors':<25} {curr['errors']:>10} {prev['errors']:>10} {curr['errors'] - prev['errors']:>+10} {_arrow(curr['errors'], prev['errors'], higher_is_better=False)}")
    print(f"  {'Avg API calls/run':<25} {curr['avg_calls']:>10.1f} {prev['avg_calls']:>10.1f} {curr['avg_calls'] - prev['avg_calls']:>+10.1f} {_arrow(curr['avg_calls'], prev['avg_calls'], higher_is_better=False)}")
    print(f"  {'Deterministic path %':<25} {det_rate_curr:>9.0f}% {det_rate_prev:>9.0f}% {det_rate_curr - det_rate_prev:>+9.0f}% {_arrow(det_rate_curr, det_rate_prev)}")

    # Per-task regression detection
    curr_by_type = defaultdict(list)
    prev_by_type = defaultdict(list)
    for r in current_results:
        curr_by_type[r["task_type"]].append(r)
    for r in previous_results:
        prev_by_type[r["task_type"]].append(r)

    regressions = []
    improvements = []
    for task_type in set(list(curr_by_type.keys()) + list(prev_by_type.keys())):
        curr_runs = curr_by_type.get(task_type, [])
        prev_runs = prev_by_type.get(task_type, [])
        if not curr_runs or not prev_runs:
            continue

        def _best_score(runs):
            scores = [r.get("score_estimate", {}).get("estimated_score", 0) for r in runs]
            return max((s for s in scores if isinstance(s, (int, float))), default=0)

        curr_best = _best_score(curr_runs)
        prev_best = _best_score(prev_runs)
        delta = curr_best - prev_best
        if delta < -0.5:
            regressions.append((task_type, prev_best, curr_best, delta))
        elif delta > 0.5:
            improvements.append((task_type, prev_best, curr_best, delta))

    if regressions:
        print(f"\n  REGRESSIONS (score dropped):")
        for task, prev_s, curr_s, delta in sorted(regressions, key=lambda x: x[3]):
            print(f"    {task}: {prev_s:.1f} -> {curr_s:.1f} ({delta:+.1f})")

    if improvements:
        print(f"\n  IMPROVEMENTS (score increased):")
        for task, prev_s, curr_s, delta in sorted(improvements, key=lambda x: -x[3]):
            print(f"    {task}: {prev_s:.1f} -> {curr_s:.1f} ({delta:+.1f})")

    if not regressions and not improvements:
        print(f"\n  No significant per-task score changes detected.")


def main():
    log_dir = Path("run_logs")
    if not log_dir.exists():
        print("No run_logs directory found")
        return

    run_files = sorted(log_dir.glob("run_*.json"))
    if not run_files:
        print("No run log files found")
        return

    # Parse args
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    show_all = "-a" in sys.argv or "--all" in sys.argv
    filter_str = None
    limit = 10  # Default to latest 10 runs

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("-n", "--limit") and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if not args[i].startswith("-"):
            filter_str = args[i]
        i += 1

    if filter_str:
        run_files = [f for f in run_files if filter_str in f.name]

    # Split into current and previous batches for trend analysis
    all_run_files = run_files
    previous_files = []
    if not show_all and len(run_files) > limit:
        previous_files = run_files[:-limit]
        run_files = run_files[-limit:]
        print(f"Showing latest {len(run_files)} of {len(all_run_files)} runs (use -a/--all for all, -n N to change limit)\n")

    all_results = []
    for f in run_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"ERROR reading {f.name}: {e}")
            continue

        r = analyze_run(data)
        all_results.append(r)
        print_run(f.name, r, verbose)

    # Load previous results for trend comparison
    previous_results = []
    for f in previous_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            previous_results.append(analyze_run(data))
        except Exception:
            continue

    if all_results:
        print_summary(all_results)
        if previous_results:
            print_trend_analysis(all_results, previous_results)


if __name__ == "__main__":
    main()
