"""Hybrid orchestrator: deterministic handlers for known tasks, LLM agent fallback for unknown."""

import json
import logging
import base64
import time
from pathlib import Path
from anthropic import Anthropic
from agent.tripletex_client import TripletexClient, get_call_log
from agent.parser import parse_task
from agent.handlers import prefetch_context, execute_task

logger = logging.getLogger(__name__)

# Load compact API reference extracted from full OpenAPI spec (15KB)
_API_REFERENCE_PATH = Path(__file__).parent.parent / "api_reference" / "api_reference_compact.txt"
try:
    _API_REFERENCE = _API_REFERENCE_PATH.read_text(encoding="utf-8")
    logger.info("Loaded API reference: %d chars", len(_API_REFERENCE))
except Exception:
    _API_REFERENCE = ""
    logger.warning("Could not load API reference from %s", _API_REFERENCE_PATH)


def solve_task(prompt: str, files: list, base_url: str, session_token: str) -> str:
    """Main entry point: parse → task-aware prefetch → deterministic handler → fallback."""
    start_time = time.time()
    phase_times: dict[str, float] = {}  # phase → seconds

    # Clear any leftover call log from previous run
    get_call_log()

    tripletex = TripletexClient(base_url, session_token)

    # Step 1: Parse the prompt FIRST (1 LLM call, no API calls)
    file_contents = _extract_file_contents(files)
    logger.info("Step 1: Parsing task with LLM...")
    t0 = time.time()
    task = parse_task(prompt, file_contents if file_contents else None)
    phase_times["parse"] = round(time.time() - t0, 2)
    task_type = task.get("task_type", "unknown")
    logger.info("Parsed task_type: %s (%.1fs)", task_type, phase_times["parse"])
    logger.info("Parsed entities: %s", json.dumps(task.get("entities", {}), ensure_ascii=False)[:500])

    # Step 2: Task-aware prefetch (only fetch what this task type needs)
    logger.info("Step 2: Pre-fetching context for '%s'...", task_type)
    t0 = time.time()
    context = prefetch_context(tripletex, task_type)
    phase_times["prefetch"] = round(time.time() - t0, 2)
    logger.info("Context: %d departments, %d employees, %d VAT types, company=%s (%.1fs)",
                len(context.get("departments", [])),
                len(context.get("employees", [])),
                len(context.get("vat_types", [])),
                context.get("company_id"),
                phase_times["prefetch"])

    # Count prefetch API calls (everything so far is prefetch)
    prefetch_call_count = len(get_call_log.__wrapped__() if hasattr(get_call_log, '__wrapped__') else _peek_call_log())

    # Step 3: Try deterministic handler
    path_taken = "unknown"
    deterministic_error = None
    handler_returned_none = False
    if task_type != "unknown":
        logger.info("Step 3: Executing deterministic handler for '%s'...", task_type)
        t0 = time.time()
        try:
            result = execute_task(task, tripletex, context)
            phase_times["handler"] = round(time.time() - t0, 2)
            if result is not None:
                elapsed = time.time() - start_time
                path_taken = "deterministic"
                logger.info("=" * 60)
                logger.info("DETERMINISTIC SUCCESS in %.1fs", elapsed)
                logger.info("  Task type: %s", task_type)
                logger.info("  Result: %s", result[:200])
                logger.info("=" * 60)
                _save_run_data(prompt, task, path_taken, result, elapsed,
                               phase_times=phase_times, files_count=len(files))
                return result
            else:
                handler_returned_none = True
                logger.warning("Handler returned None for task_type '%s', falling back to agent loop", task_type)
        except Exception as e:
            phase_times["handler"] = round(time.time() - t0, 2)
            deterministic_error = str(e)
            logger.error("Deterministic handler failed: %s", e, exc_info=True)

            # Step 3b: Targeted repair — try fixing the specific error and retry
            t0 = time.time()
            repair_result = _try_targeted_repair(deterministic_error, task, tripletex, context)
            phase_times["repair"] = round(time.time() - t0, 2)
            if repair_result:
                elapsed = time.time() - start_time
                path_taken = "deterministic_repaired"
                logger.info("DETERMINISTIC REPAIR SUCCESS in %.1fs: %s", elapsed, repair_result[:200])
                _save_run_data(prompt, task, path_taken, repair_result, elapsed,
                               deterministic_error=deterministic_error,
                               phase_times=phase_times, files_count=len(files))
                return repair_result
            logger.info("Targeted repair failed, falling back to agent loop...")

    # Step 4: Fallback to LLM agent loop for unknown/failed tasks
    logger.info("Step 4: Running LLM agent loop (fallback)...")
    # Ensure full context is available for agent loop
    if not context.get("vat_types"):
        _lazy_fetch_vat_types(tripletex, context)
    if not context.get("company_id"):
        _lazy_fetch_company_id(tripletex, context)
    t0 = time.time()
    result = _run_agent_loop(prompt, files, tripletex, context, deterministic_error)
    phase_times["agent_loop"] = round(time.time() - t0, 2)
    elapsed = time.time() - start_time
    path_taken = "agent_loop"
    logger.info("Agent loop completed in %.1fs", elapsed)
    _save_run_data(prompt, task, path_taken, result, elapsed,
                   deterministic_error=deterministic_error,
                   handler_returned_none=handler_returned_none,
                   phase_times=phase_times, files_count=len(files))
    return result


def _peek_call_log() -> list:
    """Peek at call log length without clearing it."""
    from agent.tripletex_client import _call_log, _call_log_lock
    with _call_log_lock:
        return list(_call_log)


def _try_targeted_repair(error: str, task: dict, client: TripletexClient, context: dict) -> str | None:
    """Try to fix a specific deterministic handler error and retry.

    Returns the handler result on success, or None to continue to agent loop.
    This avoids the expensive generic agent loop for common, fixable errors.
    """
    error_lower = error.lower()

    # Bank account error → set up bank account and retry
    if "bankkontonummer" in error_lower or "bank account" in error_lower:
        logger.info("Targeted repair: bank account setup needed")
        context["_bank_account_checked"] = False  # Force re-check
        from agent.handlers import _ensure_company_bank_account, execute_task
        # Ensure we have company_id
        if not context.get("company_id"):
            _lazy_fetch_company_id(client, context)
        _ensure_company_bank_account(client, context)
        try:
            result = execute_task(task, client, context)
            if result is not None:
                return result
        except Exception as e2:
            logger.warning("Targeted repair retry failed: %s", e2)
        return None

    # VAT type resolution error → try with hardcoded map
    if "vattype" in error_lower or "vat type" in error_lower:
        logger.info("Targeted repair: VAT type resolution issue")
        # Use hardcoded Norwegian VAT type IDs as fallback
        from agent.handlers import execute_task
        if not context.get("vat_types"):
            context["vat_types"] = _HARDCODED_VAT_TYPES
            try:
                result = execute_task(task, client, context)
                if result is not None:
                    return result
            except Exception as e2:
                logger.warning("Targeted repair (VAT fallback) retry failed: %s", e2)
        return None

    # Employee not found → fetch full employee list and retry
    if "employee not found" in error_lower:
        logger.info("Targeted repair: employee search")
        try:
            emp_resp = client.get("/employee", {"count": 100})
            context["employees"] = emp_resp.get("values", [])
            from agent.handlers import execute_task
            result = execute_task(task, client, context)
            if result is not None:
                return result
        except Exception as e2:
            logger.warning("Targeted repair (employee) retry failed: %s", e2)
        return None

    logger.info("No targeted repair available for error: %s", error[:200])
    return None


# Hardcoded Norwegian VAT type IDs — standard across all Tripletex accounts
_HARDCODED_VAT_TYPES = [
    {"id": 3, "percentage": 25.0, "name": "Utgående mva høy sats 25%"},
    {"id": 31, "percentage": 15.0, "name": "Utgående mva middels sats 15%"},
    {"id": 32, "percentage": 12.0, "name": "Utgående mva lav sats 12%"},
    {"id": 6, "percentage": 0.0, "name": "Fritatt for mva 0%"},
]


def _lazy_fetch_vat_types(client: TripletexClient, context: dict):
    """Fetch VAT types if not already in context."""
    try:
        vat_resp = client.get("/ledger/vatType", {"count": 1000})
        context["vat_types"] = vat_resp.get("values", [])
    except Exception:
        pass


def _lazy_fetch_company_id(client: TripletexClient, context: dict):
    """Fetch company ID if not already in context."""
    try:
        who = client.get("/token/session/>whoAmI")
        context["company_id"] = who.get("value", {}).get("companyId")
    except Exception:
        pass


def _save_run_data(prompt: str, task: dict, path: str, result: str, elapsed: float,
                   deterministic_error: str | None = None,
                   handler_returned_none: bool = False,
                   phase_times: dict | None = None,
                   files_count: int = 0):
    """Save full run data (parsed task + all API calls) for analysis.

    Schema designed to support: failure diagnosis, efficiency analysis,
    regression detection, and task-level score tracking.
    """
    from pathlib import Path
    from datetime import datetime
    import hashlib

    # Use absolute path relative to this file, not working directory
    log_dir = Path(__file__).parent.parent / "run_logs"
    log_dir.mkdir(exist_ok=True)

    api_calls = get_call_log()
    errors_4xx = [c for c in api_calls if c.get("status", 0) >= 400]

    # Classify each API call by phase and purpose
    call_phases = _classify_api_calls(api_calls)

    # Extract what was successfully created/modified
    mutations = _extract_mutations(api_calls)

    # Detect the language of the prompt (first 2-3 words heuristic)
    prompt_lang = _detect_language(prompt)

    # Generate a stable prompt fingerprint — same task variant produces same hash
    # This lets us track the same task across runs even without a task_id from the platform
    prompt_fingerprint = hashlib.sha256(prompt.strip().encode()).hexdigest()[:12]

    # Count calls by status bucket
    status_counts = {}
    for c in api_calls:
        s = c.get("status", 0)
        bucket = f"{s // 100}xx"
        status_counts[bucket] = status_counts.get(bucket, 0) + 1

    run_data = {
        # -- Identity --
        "timestamp": datetime.now().isoformat(),
        "prompt_fingerprint": prompt_fingerprint,
        "prompt": prompt,
        "prompt_language": prompt_lang,
        "files_count": files_count,

        # -- Parse result --
        "parsed_task": task,

        # -- Execution --
        "path_taken": path,
        "result": result,
        "deterministic_error": deterministic_error,
        "handler_returned_none": handler_returned_none,

        # -- Timing --
        "elapsed_seconds": round(elapsed, 2),
        "phase_times": phase_times or {},

        # -- API call summary --
        "total_api_calls": len(api_calls),
        "errors_4xx": len(errors_4xx),
        "status_counts": status_counts,
        "call_phases": call_phases,
        "mutations": mutations,

        # -- Full API call log (for deep debugging) --
        "api_calls": api_calls,
    }

    filename = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(log_dir / filename, "w", encoding="utf-8") as f:
            json.dump(run_data, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Run data saved: %s (%d calls, %d errors, %.1fs)",
                    filename, len(api_calls), len(errors_4xx), elapsed)
    except Exception as e:
        logger.error("Failed to save run data to file: %s", e)
        logger.error("RUN DATA DUMP: %s", json.dumps(run_data, ensure_ascii=False, default=str)[:5000])


def _classify_api_calls(api_calls: list) -> dict:
    """Classify API calls into phases: prefetch, action, retry, wasted."""
    counts = {"prefetch": 0, "action": 0, "wasted": 0, "lookup": 0}
    first_mutation = False
    for c in api_calls:
        method = c.get("method", "")
        status = c.get("status", 0)
        if method in ("POST", "PUT", "DELETE"):
            first_mutation = True
        if status >= 400:
            counts["wasted"] += 1
        elif not first_mutation and method == "GET":
            counts["prefetch"] += 1
        elif method == "GET":
            counts["lookup"] += 1
        else:
            counts["action"] += 1
    return counts


def _extract_mutations(api_calls: list) -> list:
    """Extract successful create/update/delete operations."""
    mutations = []
    for c in api_calls:
        method = c.get("method", "")
        status = c.get("status", 0)
        if method in ("POST", "PUT", "DELETE") and status in (200, 201):
            endpoint = c.get("url", "").split("/v2")[-1].split("?")[0]
            val = (c.get("response_body") or {}).get("value", {})
            entry = {"method": method, "endpoint": endpoint, "status": status}
            if isinstance(val, dict) and val.get("id"):
                entry["entity_id"] = val["id"]
            mutations.append(entry)
    return mutations


def _detect_language(prompt: str) -> str:
    """Detect prompt language using keyword heuristics."""
    p = prompt.lower()
    if any(w in p for w in ["opprett", "ansatt", "avdeling", "faktura", "bilag", "slett"]):
        return "nb"  # Norwegian bokmål
    if any(w in p for w in ["créer", "employé", "facture", "département"]):
        return "fr"
    if any(w in p for w in ["crear", "empleado", "factura", "departamento"]):
        return "es"
    if any(w in p for w in ["erstellen", "mitarbeiter", "rechnung", "abteilung"]):
        return "de"
    if any(w in p for w in ["criar", "empregado", "fatura", "departamento"]):
        return "pt"
    if any(w in p for w in ["opprett", "tilsett", "avdeling"]) and "nynorsk" not in p:
        # Check for nynorsk-specific words
        if any(w in p for w in ["tilsett", "avdeling", "kunde"]):
            return "nn"
    if any(w in p for w in ["create", "employee", "invoice", "department", "customer"]):
        return "en"
    return "unknown"


# ---------------------------------------------------------------------------
# File extraction
# ---------------------------------------------------------------------------

def _extract_file_contents(files: list) -> list[str]:
    """Extract text content from attached files for the parser."""
    contents = []
    for f in files:
        mime_type = f.get("mime_type", "")
        filename = f.get("filename", "unknown")
        file_data = f.get("content_base64", "")

        if mime_type == "application/pdf":
            text = _extract_pdf_text(file_data)
            if text.strip():
                contents.append(f"[File: {filename}]\n{text}")
        elif not mime_type.startswith("image/"):
            try:
                text = base64.b64decode(file_data).decode("utf-8")
                contents.append(f"[File: {filename}]\n{text}")
            except Exception:
                pass
    return contents


def _extract_pdf_text(base64_data: str) -> str:
    """Extract text from a base64-encoded PDF."""
    try:
        from PyPDF2 import PdfReader
        import io
        pdf_bytes = base64.b64decode(base64_data)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# LLM Agent Loop (fallback for unknown tasks)
# ---------------------------------------------------------------------------

FALLBACK_SYSTEM_PROMPT = """You are an accounting agent for Tripletex (Norwegian accounting system). Execute API calls to complete the task.

CRITICAL: NEVER give up. NEVER say "I can't do this". Always attempt every part of the task. Even if one part fails, complete the other parts for partial credit. Partial credit is better than zero.

CRITICAL: When the task describes EXISTING entities (invoices, payments, customers, orders), you MUST FIND them first — NEVER create new ones. If GET /invoice returns 0 results, try different date ranges (2024-01-01 to 2025-12-31) or broader search params. The task environment uses dates in 2024-2025.

KEY RULES:
1. vatType MUST be {"id": N}, NEVER a string. OUTGOING (sales/invoices): 25%→id=3, 15%→id=31, 12%→id=32, 0%→id=5. INCOMING (expenses/purchases): 25%→id=1, 15%→id=11, 12%→id=12, 0%/none→id=0. For voucher postings on expense accounts (4xxx-7xxx), use INCOMING IDs. For balance accounts (1xxx-2xxx), use id=0.
2. Order lines use "count" (NOT "quantity"), "unitPriceExcludingVatCurrency" for price
3. Reference fields are ALWAYS {"id": N} — customer, product, department, employee, vatType
4. Products may be PRE-SEEDED — GET /product?number=X to find them before creating
5. sendToCustomer=false as query param on POST /invoice
6. "kontoadministrator"/"administrator"/"admin" in any language → userType="EXTENDED"
7. Do NOT create duplicate entities — check context for already-created IDs
8. Dates: ISO "YYYY-MM-DD" format
9. Bank account error on invoice → GET /ledger/account?number=1920, PUT with bankAccountNumber="12345678903"
10. POST/PUT return {"value": {...}}, GET list returns {"values": [...]}
15. GET /company WITHOUT an ID returns 405 — use GET /token/session/>whoAmI (case-sensitive!) to get companyId first, then GET /company/{companyId}

CORRECT ORDER LINE EXAMPLE (copy this format exactly):
{"product": {"id": 84376857}, "count": 1, "unitPriceExcludingVatCurrency": 10500, "vatType": {"id": 3}}

WRONG (will cause 422):
{"product": {"id": 84376857}, "quantity": 1, "unitPriceExcludingVatCurrency": 10500, "vatType": "25%"}
                            ^^^^^^^^ wrong field                                        ^^^^^ must be {"id": 3}
11. VOUCHER POSTINGS: Use "amountGross" and "amountGrossCurrency" (NOT "amount"!). The "amount" field is read-only/system-generated and will cause 422 errors. Example posting: {"account": {"id": N}, "amountGross": 13900, "amountGrossCurrency": 13900, "date": "YYYY-MM-DD"}
12. ACCOUNTING DIMENSIONS: POST /ledger/accountingDimensionName to create dimension, POST /ledger/accountingDimensionValue to create values. When posting vouchers with dimensions, use "freeAccountingDimension1": {"id": valueId} (or dimension2/3 for 2nd/3rd dimensions) on each posting that needs it. The dimensionIndex from the created dimension tells you which field to use (1→freeAccountingDimension1, 2→freeAccountingDimension2).
13. PAYMENT REVERSAL: To cancel/reverse a payment on an invoice, use PUT /invoice/{id}/:payment with NEGATIVE paidAmount (e.g., paidAmount=-55312.50). To create a credit note: PUT /invoice/{id}/:createCreditNote with query params date, comment. To reverse a voucher: PUT /ledger/voucher/{id}/:reverse.
14. FINDING INVOICES: GET /invoice REQUIRES invoiceDateFrom & invoiceDateTo. Always try broad ranges: "2024-01-01" to "2025-12-31". Search by customerId after finding the customer. If 0 results, try even broader ranges.

COMMON ENDPOINTS (but Tripletex has more — try reasonable paths if needed):
/employee, /customer, /product, /invoice, /order, /travelExpense, /project, /department,
/ledger/account, /ledger/voucher, /ledger/vatType, /ledger/accountingDimensionName, /ledger/accountingDimensionValue,
/invoice/paymentType (for incoming payments from customers), /ledger/paymentTypeOut (for outgoing payments to suppliers), /token/session/>whoAmI

SALARY/PAYROLL ENDPOINTS:
/salary/transaction — POST to create payroll run (body: date, year, month). GET to list.
/salary/type — GET to find salary type IDs (Fastlønn, Bonus, etc.)
/salary/payslip — GET payslips for a salary transaction (params: wageTransactionId, employeeId)
/salary/specification — POST to add lines to a payslip (body: payslip.id, salaryType.id, rate/amount, count)
/salary/settings — GET/PUT salary settings for company

EMPLOYEE EMPLOYMENT ENDPOINTS:
/employee/employment — GET/POST to manage employments (body: employee.id, startDate, isMainEmployer, taxDeductionCode)
/employee/employment/details — GET/POST/PUT employment details (body: employment.id, date, annualSalary, employmentType, remunerationType)
/employee/employment/{id} — GET/PUT specific employment

INVOICE ENDPOINTS:
POST /invoice accepts query params: sendToCustomer, paymentTypeId, paidAmount (register payment at creation!)
PUT /invoice/{id}/:payment to register payment after creation (params: paymentDate, paymentTypeId, paidAmount) — use NEGATIVE paidAmount to reverse!
PUT /invoice/{id}/:createCreditNote to create a credit note (params: date, comment)
PUT /ledger/voucher/{id}/:reverse to reverse a voucher
GET /invoice REQUIRES invoiceDateFrom and invoiceDateTo params — use broad range like 2024-01-01 to 2025-12-31

BANK RECONCILIATION:
/bank/statement — GET bank statements
/bank/reconciliation — bank reconciliation endpoints

If the task requires endpoints not listed above, TRY them — the Tripletex API is large. Common patterns:
- /ledger/voucher for journal entries (postings use amountGross NOT amount!)
- /project for projects
- /timesheet/entry for logging hours, /activity/>forTimeSheet?projectId=N for activities
- /ledger/accountingDimensionName + /ledger/accountingDimensionValue for custom dimensions
- Use Tripletex v2 API conventions: plural nouns, camelCase fields

Be MINIMAL — every extra call or error hurts scoring. Respond with a short summary when done."""

# Append the full API reference to the system prompt at runtime
def _get_system_prompt() -> str:
    """Build system prompt with full API reference."""
    if _API_REFERENCE:
        return FALLBACK_SYSTEM_PROMPT + "\n\n" + _API_REFERENCE
    return FALLBACK_SYSTEM_PROMPT

FALLBACK_TOOLS = [
    {
        "name": "tripletex_get",
        "description": "Make a GET request to a Tripletex API endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint path"},
                "params": {"type": "object", "description": "Query parameters", "additionalProperties": True}
            },
            "required": ["endpoint"]
        }
    },
    {
        "name": "tripletex_post",
        "description": "Make a POST request. Supports optional query params.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint path"},
                "body": {"type": "object", "description": "JSON body", "additionalProperties": True},
                "params": {"type": "object", "description": "Optional query parameters", "additionalProperties": True}
            },
            "required": ["endpoint", "body"]
        }
    },
    {
        "name": "tripletex_put",
        "description": "Make a PUT request. Supports optional body and query params.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint path"},
                "body": {"type": "object", "description": "JSON body (optional for action endpoints)", "additionalProperties": True},
                "params": {"type": "object", "description": "Optional query parameters", "additionalProperties": True}
            },
            "required": ["endpoint"]
        }
    },
    {
        "name": "tripletex_delete",
        "description": "Make a DELETE request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint path with entity ID"}
            },
            "required": ["endpoint"]
        }
    }
]


def _run_agent_loop(prompt: str, files: list, tripletex: TripletexClient, context: dict,
                    deterministic_error: str | None = None) -> str:
    """Fallback LLM agent loop for tasks the deterministic handlers can't handle."""
    client = Anthropic()

    # Build user message with context (including what the deterministic handler already did)
    user_content = _build_agent_user_content(prompt, files, context, deterministic_error)
    messages = [{"role": "user", "content": user_content}]

    metrics = {
        "total_iterations": 0,
        "api_calls": {"GET": 0, "POST": 0, "PUT": 0, "DELETE": 0},
        "errors_4xx": [],
    }

    # Track entities created during agent loop to prevent duplicates
    created_entities: dict[str, list[dict]] = {}

    max_iterations = 15
    for i in range(max_iterations):
        metrics["total_iterations"] = i + 1
        logger.info("Agent loop iteration %d", i + 1)

        # Retry on rate limit with exponential backoff
        response = None
        for retry in range(4):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    system=_get_system_prompt(),
                    tools=FALLBACK_TOOLS,
                    messages=messages,
                )
                break
            except Exception as e:
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    wait = 2 ** (retry + 1)
                    logger.warning("Rate limited, waiting %ds before retry %d...", wait, retry + 1)
                    time.sleep(wait)
                else:
                    raise
        if response is None:
            logger.error("All retries exhausted for LLM call")
            return "Rate limited — could not complete task"

        if response.stop_reason == "end_turn":
            final_text = ""
            for block in response.content:
                if block.type == "text":
                    final_text += block.text
            logger.info("Agent finished: %s", final_text[:200])
            _log_agent_summary(metrics)
            return final_text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logger.info("TOOL CALL: %s | input=%s", block.name,
                           json.dumps(block.input, ensure_ascii=False)[:300])

                result = _execute_agent_tool(tripletex, block.name, block.input)

                method = block.name.replace("tripletex_", "").upper()
                if method in metrics["api_calls"]:
                    metrics["api_calls"][method] += 1

                result_str = json.dumps(result, ensure_ascii=False)
                if isinstance(result, dict) and (result.get("status") in (400, 401, 404, 422) or result.get("error")):
                    metrics["errors_4xx"].append({
                        "iteration": i + 1,
                        "tool": block.name,
                        "input": block.input,
                        "error": result_str[:500],
                    })
                    logger.warning("API ERROR: %s", result_str[:500])

                # Track created entities from successful POSTs to prevent duplicates
                reminder = ""
                if block.name == "tripletex_post" and isinstance(result, dict):
                    value = result.get("value", {})
                    if value.get("id"):
                        endpoint = block.input.get("endpoint", "")
                        entity_type = endpoint.strip("/").split("/")[-1]
                        entity_info = {"id": value["id"], "name": value.get("name", value.get("displayName", ""))}
                        created_entities.setdefault(entity_type, []).append(entity_info)
                        logger.info("Tracked created %s: id=%d", entity_type, value["id"])
                        reminder = f"\n\n[SYSTEM: Created {entity_type} id={value['id']}. Do NOT create duplicates. Already created: {json.dumps(created_entities, ensure_ascii=False)}]"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str + reminder
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    logger.warning("Agent hit max iterations")
    _log_agent_summary(metrics)
    return "Max iterations reached"


def _build_agent_user_content(prompt: str, files: list, context: dict,
                              deterministic_error: str | None = None) -> list:
    """Build user message for the agent loop with context."""
    content = []

    # Task first
    content.append({"type": "text", "text": f"## Task\n{prompt}"})

    # Files
    for f in files:
        mime_type = f.get("mime_type", "")
        filename = f.get("filename", "unknown")
        file_data = f.get("content_base64", "")

        if mime_type.startswith("image/"):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": file_data}
            })
        elif mime_type == "application/pdf":
            text = _extract_pdf_text(file_data)
            if text.strip():
                content.append({"type": "text", "text": f"--- File: {filename} ---\n{text}\n--- End ---"})
            else:
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": file_data}
                })
        else:
            try:
                text = base64.b64decode(file_data).decode("utf-8")
                content.append({"type": "text", "text": f"--- File: {filename} ---\n{text}\n--- End ---"})
            except Exception:
                pass

    # Context last (recency bias)
    ctx_parts = []
    if context.get("departments"):
        dept_lines = [f"  - id={d.get('id')}, name=\"{d.get('name', '')}\"" for d in context["departments"]]
        default = context.get("default_department_id", "?")
        ctx_parts.append(f"Departments:\n" + "\n".join(dept_lines) + f"\n**DEFAULT DEPT: {{\"id\": {default}}}**")
    if context.get("employees"):
        emp_lines = [f"  - id={e.get('id')}, name=\"{e.get('firstName', '')} {e.get('lastName', '')}\"" for e in context["employees"]]
        ctx_parts.append("Employees:\n" + "\n".join(emp_lines))
    if context.get("vat_types"):
        vat_lines = [f"  - id={v.get('id')}, percentage={v.get('percentage')}%, name=\"{v.get('name', '')}\"" for v in context["vat_types"][:20]]
        ctx_parts.append("VAT Types (use {\"id\": N} in vatType fields):\n" + "\n".join(vat_lines))
    if context.get("company_id"):
        ctx_parts.append(f"Company ID: {context['company_id']}")

    # Include entities already created by deterministic handler (avoid duplicates!)
    created = context.get("_created_entities", {})
    if created:
        created_lines = [f"  - {k}: {v}" for k, v in created.items()]
        ctx_parts.append("**ALREADY CREATED (do NOT re-create):**\n" + "\n".join(created_lines))

    # Include error from deterministic handler so agent knows what failed
    if deterministic_error:
        ctx_parts.append(f"**Previous attempt failed with:** {deterministic_error}\nFix this specific issue and continue from where it left off. Do NOT re-create entities listed above.")

    if ctx_parts:
        content.append({"type": "text", "text": "## Pre-fetched Context (do NOT re-fetch)\n" + "\n\n".join(ctx_parts)})

    return content


def _fix_voucher_postings(body: dict) -> dict:
    """Auto-fix common LLM mistakes in voucher posting bodies.

    - Convert 'amount' → 'amountGross'/'amountGrossCurrency' (amount is read-only)
    """
    if not body or "postings" not in body:
        return body
    for posting in body.get("postings", []):
        if "amount" in posting and "amountGross" not in posting:
            posting["amountGross"] = posting.pop("amount")
        if "amountGross" in posting and "amountGrossCurrency" not in posting:
            posting["amountGrossCurrency"] = posting["amountGross"]
    return body


# Hardcoded VAT string → ID map for auto-fixing LLM mistakes in agent loop
_VAT_STRING_TO_ID = {
    "25%": 3, "25": 3, "15%": 31, "15": 31,
    "12%": 32, "12": 32, "0%": 6, "0": 6,
}


def _auto_fix_order_body(body: dict) -> dict:
    """Auto-fix common LLM mistakes in order bodies before sending to API.

    Fixes:
    - vatType as string (e.g. "25%") → {"id": 3}
    - "quantity" field → "count" (Tripletex uses "count")
    - vatType as bare integer → {"id": N}
    """
    for ol in body.get("orderLines", []):
        # Fix vatType string → {"id": N}
        vt = ol.get("vatType")
        if isinstance(vt, str):
            clean = vt.strip().replace("%", "").strip()
            vat_id = _VAT_STRING_TO_ID.get(clean + "%") or _VAT_STRING_TO_ID.get(clean)
            if vat_id:
                logger.info("Auto-fix: vatType '%s' → {'id': %d}", vt, vat_id)
                ol["vatType"] = {"id": vat_id}
        elif isinstance(vt, (int, float)):
            logger.info("Auto-fix: vatType %s → {'id': %d}", vt, int(vt))
            ol["vatType"] = {"id": int(vt)}

        # Fix "quantity" → "count"
        if "quantity" in ol and "count" not in ol:
            logger.info("Auto-fix: 'quantity' → 'count'")
            ol["count"] = ol.pop("quantity")

        # Ensure count defaults to 1 if missing
        if "count" not in ol:
            ol["count"] = 1
    return body


def _auto_fix_product_body(body: dict) -> dict:
    """Auto-fix common LLM mistakes in product bodies."""
    vt = body.get("vatType")
    if isinstance(vt, str):
        clean = vt.strip().replace("%", "").strip()
        vat_id = _VAT_STRING_TO_ID.get(clean + "%") or _VAT_STRING_TO_ID.get(clean)
        if vat_id:
            logger.info("Auto-fix product: vatType '%s' → {'id': %d}", vt, vat_id)
            body["vatType"] = {"id": vat_id}
    elif isinstance(vt, (int, float)):
        body["vatType"] = {"id": int(vt)}
    return body


def _execute_agent_tool(tripletex: TripletexClient, tool_name: str, tool_input: dict) -> dict:
    """Execute a tool call from the agent loop."""
    try:
        if tool_name == "tripletex_get":
            return tripletex.get(tool_input["endpoint"], tool_input.get("params"))
        elif tool_name == "tripletex_post":
            body = tool_input.get("body", {})
            endpoint = tool_input["endpoint"]
            # Auto-fix voucher postings (amount → amountGross)
            if "/ledger/voucher" in endpoint and "postings" in body:
                body = _fix_voucher_postings(body)
            # Auto-fix order lines (vatType as string, quantity → count)
            if "orderLines" in body:
                body = _auto_fix_order_body(body)
            # Auto-fix product vatType
            if "/product" in endpoint and "vatType" in body:
                body = _auto_fix_product_body(body)
            return tripletex.post(endpoint, body, tool_input.get("params"))
        elif tool_name == "tripletex_put":
            return tripletex.put(tool_input["endpoint"], tool_input.get("body"), tool_input.get("params"))
        elif tool_name == "tripletex_delete":
            return tripletex.delete(tool_input["endpoint"])
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        logger.error("Tool execution error: %s", e)
        return {"error": str(e)}


def _log_agent_summary(metrics: dict):
    """Log agent loop summary."""
    logger.info("=" * 60)
    logger.info("AGENT LOOP SUMMARY")
    logger.info("  Iterations: %d", metrics["total_iterations"])
    logger.info("  API calls: %s", metrics["api_calls"])
    logger.info("  4xx errors: %d", len(metrics["errors_4xx"]))
    if metrics["errors_4xx"]:
        for err in metrics["errors_4xx"]:
            logger.info("    iter %d: %s -> %s", err["iteration"], err["tool"], err["error"][:200])
    logger.info("=" * 60)
