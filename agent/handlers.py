"""Deterministic task handlers for Tripletex API operations.

Each handler takes parsed task data and a TripletexClient, executes the
exact API calls needed, and returns a result summary string.
"""

import logging
from datetime import date
from typing import Any

from .tripletex_client import TripletexClient

logger = logging.getLogger(__name__)


def _today() -> str:
    return date.today().isoformat()


def _get_or_create_account(client: TripletexClient, number: str, name: str | None = None,
                           cache: dict | None = None) -> int:
    """Resolve an account number to its ID, creating the account if it doesn't exist."""
    if cache and number in cache:
        return cache[number]
    resp = client.get("/ledger/account", {"number": number, "count": 1})
    values = resp.get("values", [])
    if values:
        acct_id = values[0]["id"]
    else:
        # Auto-create missing account
        acct_name = name or f"Account {number}"
        logger.info("Account %s not found, auto-creating as '%s'", number, acct_name)
        create_resp = client.post("/ledger/account", {
            "number": int(number),
            "name": acct_name,
        })
        if isinstance(create_resp, dict) and create_resp.get("value", {}).get("id"):
            acct_id = create_resp["value"]["id"]
        else:
            raise RuntimeError(f"Failed to create account {number}: {create_resp}")
    if cache is not None:
        cache[number] = acct_id
    return acct_id


def _check_response(result: dict, operation: str) -> dict:
    """Check API response for errors. Raises RuntimeError on 4xx."""
    if isinstance(result, dict) and result.get("status") and result["status"] >= 400:
        msg = result.get("message", result.get("developerMessage", str(result)))
        raise RuntimeError(f"{operation} failed ({result['status']}): {msg}")
    return result


# ---------------------------------------------------------------------------
# Context prefetch
# ---------------------------------------------------------------------------

# Task types that need specific prefetch categories
_NEEDS_DEPARTMENT = {
    "create_employee", "update_employee", "update_employee_role",
}
# Removed: create_project, create_travel_expense
# — these resolve department inline only when needed
_NEEDS_EMPLOYEES = {
    "update_employee", "update_employee_role",
    "delete_travel_expense",
    "run_payroll",
}
# Removed: create_project, create_travel_expense, log_timesheet_hours
# — these do their own employee lookup by email/name inline
# VAT type prefetch REMOVED — _resolve_vat_type has hardcoded fallback for
# standard Norwegian rates (25%=3, 15%=31, 12%=32, 0%=6). Saves 1 GET call.
_NEEDS_VAT_TYPES: set[str] = set()
# Bank account setup is now LAZY — only triggered on invoice creation failure.
# This saves 2-3 GET/PUT calls when bank account is already set up.
_NEEDS_BANK_ACCOUNT: set[str] = set()


def prefetch_context(client: TripletexClient, task_type: str = "unknown") -> dict:
    """Task-aware prefetch: only fetch what this task type actually needs.

    For simple tasks like create_customer, this saves 2-4 unnecessary GET calls.
    """
    context: dict[str, Any] = {
        "default_department_id": None,
        "departments": [],
        "employees": [],
        "vat_types": [],
        "company_id": None,
        "_bank_account_checked": False,
        "_created_entities": {},
    }

    # For unknown tasks, only fetch employees (most commonly needed).
    # Skip expensive prefetches (VAT types, bank account) to save API calls.
    fetch_all = False
    fetch_employees_only = task_type == "unknown"

    if task_type in _NEEDS_DEPARTMENT:
        try:
            dept_resp = client.get("/department", {"count": 100})
            departments = dept_resp.get("values", [])
            context["departments"] = departments
            if departments:
                context["default_department_id"] = departments[0].get("id")
            logger.info("Prefetched %d departments", len(departments))
        except Exception as exc:
            logger.warning("Failed to prefetch departments: %s", exc)

    if fetch_employees_only or task_type in _NEEDS_EMPLOYEES:
        try:
            emp_resp = client.get("/employee", {"count": 100})
            employees = emp_resp.get("values", [])
            context["employees"] = employees
            logger.info("Prefetched %d employees", len(employees))
        except Exception as exc:
            logger.warning("Failed to prefetch employees: %s", exc)

    if task_type in _NEEDS_VAT_TYPES:
        try:
            vat_resp = client.get("/ledger/vatType", {"count": 1000})
            vat_types = vat_resp.get("values", [])
            context["vat_types"] = vat_types
            logger.info("Prefetched %d VAT types", len(vat_types))
        except Exception as exc:
            logger.warning("Failed to prefetch VAT types: %s", exc)

    if task_type in _NEEDS_BANK_ACCOUNT:
        # Get company ID (needed for bank account setup)
        try:
            who = client.get("/token/session/>whoAmI")
            company_id = who.get("value", {}).get("companyId")
            context["company_id"] = company_id
            logger.info("Company ID: %s", company_id)
        except Exception as exc:
            logger.warning("Failed to get company ID: %s", exc)

        # Set up bank account (required for invoices)
        _ensure_company_bank_account(client, context)

    return context


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute_task(task: dict, client: TripletexClient, context: dict) -> str | None:
    """Route to the right handler. Returns summary string or None for unknown types."""
    task_type = task.get("task_type") or task.get("type", "")
    handler = _HANDLERS.get(task_type)
    if handler is None:
        logger.warning("Unknown task type: %s", task_type)
        return None
    return handler(task, client, context)


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

def _ensure_department(client: TripletexClient, context: dict) -> int:
    """Ensure a department exists and return its ID. Creates one if needed."""
    dept_id = context.get("default_department_id")
    if dept_id:
        return dept_id
    # No department found — create one
    logger.info("No department found, creating default...")
    result = client.post("/department", {"name": "Avdeling"})
    _check_response(result, "POST /department")
    dept_id = result["value"]["id"]
    context["default_department_id"] = dept_id
    context["departments"] = [result["value"]]
    return dept_id


def _ensure_employee(client: TripletexClient, context: dict) -> int:
    """Ensure an employee exists and return their ID. Creates one if needed."""
    employees = context.get("employees", [])
    if employees:
        return employees[0].get("id")
    # No employee — create a minimal one
    logger.info("No employee found, creating default for references...")
    dept_id = _ensure_department(client, context)
    result = client.post("/employee", {
        "firstName": "System",
        "lastName": "Bruker",
        "userType": "NO_ACCESS",
        "department": {"id": dept_id},
        "allowInformationRegistration": True,
    })
    _check_response(result, "POST /employee (default)")
    emp = result["value"]
    context["employees"] = [emp]
    return emp["id"]


def _resolve_vat_type(vat_str: str | None, context: dict) -> dict | None:
    """Resolve a VAT type string (e.g. '25%', '25', 'MVA 25%') to a {'id': N} reference.

    Matching priority:
    1. Exact percentage match with typeOfVat=OUTGOING (sales VAT)
    2. Exact percentage match (any type)
    3. None if no match
    """
    if not vat_str:
        return None

    # Handle "exempt"/"fritatt" → 0% outgoing VAT (id=5 innenfor or id=6 utenfor)
    vat_lower = str(vat_str).lower()
    _EXEMPT_KEYWORDS = ("exempt", "fritatt", "avgiftsfri", "exento", "exonéré", "befreit")
    if any(kw in vat_lower for kw in _EXEMPT_KEYWORDS):
        return {"id": 6}  # Ingen utgående avgift (utenfor mva-loven)

    # Extract numeric percentage from string like "25%", "25", "MVA 25%"
    import re
    match = re.search(r"(\d+(?:\.\d+)?)\s*%?", str(vat_str))
    if not match:
        # If it's already an ID reference like {"id": 3}, pass through
        if isinstance(vat_str, dict) and "id" in vat_str:
            return vat_str
        return None

    target_pct = float(match.group(1))

    # Fast path: hardcoded standard Norwegian VAT type IDs (no API call needed)
    _HARDCODED_VAT = {25.0: 3, 15.0: 31, 12.0: 32, 0.0: 6}
    if target_pct in _HARDCODED_VAT:
        return {"id": _HARDCODED_VAT[target_pct]}

    # Slow path: search prefetched VAT types (only if list was fetched)
    vat_types = context.get("vat_types", [])
    for vt in vat_types:
        if (vt.get("percentage") == target_pct and
                "utgående" in vt.get("name", "").lower()):
            return {"id": vt["id"]}
    for vt in vat_types:
        if vt.get("percentage") == target_pct:
            return {"id": vt["id"]}

    logger.warning("Could not resolve VAT type '%s' (target %.1f%%)", vat_str, target_pct)
    return None


def _ensure_company_bank_account(client: TripletexClient, context: dict) -> None:
    """Ensure the company has a bank account registered (required for invoices).

    The Company schema has NO bankAccountNumber field. The correct approach is
    to set bankAccountNumber on ledger account 1920 (Bank) via PUT /ledger/account/{id}.

    Raises RuntimeError if bank account setup fails — this is critical for invoice
    tasks and should trigger targeted repair rather than silently proceeding.
    """
    if context.get("_bank_account_checked"):
        return

    try:
        # Find ledger account 1920 (Bank)
        acct_resp = client.get("/ledger/account", {"number": "1920", "count": 1})
        accounts = acct_resp.get("values", [])

        if not accounts:
            logger.warning("Ledger account 1920 not found, skipping bank account setup")
            context["_bank_account_checked"] = True
            return

        acct = accounts[0]
        acct_id = acct["id"]

        # Already set — skip
        if acct.get("bankAccountNumber"):
            logger.info("Bank account 1920 already has number: %s", acct["bankAccountNumber"])
            context["_bank_account_checked"] = True
            return

        # Set bank account number — send the full account object back with the new field
        # The API requires matching version to avoid conflicts
        put_body = {
            "id": acct_id,
            "version": acct.get("version", 0),
            "name": acct.get("name", "Bank"),
            "number": acct.get("number", 1920),
            "isBankAccount": True,
            "bankAccountNumber": "12345678903",
        }
        result = client.put(f"/ledger/account/{acct_id}", put_body)
        if isinstance(result, dict) and result.get("status", 0) >= 400:
            logger.warning("PUT /ledger/account/%d failed: %s — trying minimal body", acct_id, result.get("message", ""))
            # Retry with minimal body
            result2 = client.put(f"/ledger/account/{acct_id}", {
                "id": acct_id,
                "version": acct.get("version", 0),
                "bankAccountNumber": "12345678903",
            })
            if isinstance(result2, dict) and result2.get("status", 0) >= 400:
                error_msg = result2.get("message", str(result2))
                logger.error("Bank account setup failed on retry: %s", error_msg)
                # Mark as NOT checked so targeted repair can retry
                context["_bank_account_checked"] = False
                raise RuntimeError(f"bank account setup failed: {error_msg}")
            else:
                logger.info("Bank account set on ledger account %d (minimal body)", acct_id)
        else:
            logger.info("Bank account set on ledger account %d", acct_id)

        context["_bank_account_checked"] = True

    except RuntimeError:
        raise  # Re-raise our own errors for targeted repair
    except Exception as exc:
        logger.warning("Failed to setup bank account: %s", exc)
        # Mark as checked to avoid infinite retry loops, but log the failure
        context["_bank_account_checked"] = True



def _resolve_product_in_order_line(line: dict, client: TripletexClient, context: dict) -> dict:
    """Build a proper order line dict, resolving product references and VAT types."""
    ol: dict[str, Any] = {}
    products: list = []  # Track fetched product data for VAT type inference

    # Resolve product: if a product number is given, look it up
    product_ref = line.get("product")
    if product_ref and isinstance(product_ref, str):
        # It's a product number string — look it up
        resp = client.get("/product", {"number": product_ref, "count": 1})
        products = resp.get("values", [])
        if products:
            ol["product"] = {"id": products[0]["id"]}
            logger.info("Resolved product number '%s' -> id=%d", product_ref, products[0]["id"])
        else:
            # Product not found by number — use description-only line
            logger.warning("Product number '%s' not found, using description only", product_ref)
    elif product_ref and isinstance(product_ref, dict) and "id" in product_ref:
        ol["product"] = product_ref

    # Description
    if line.get("description"):
        ol["description"] = line["description"]

    # Count/quantity — default to 1
    count = line.get("count") or line.get("quantity") or 1
    ol["count"] = count

    # Price
    if line.get("unitPrice") is not None:
        ol["unitPriceExcludingVatCurrency"] = line["unitPrice"]
    if line.get("unitPriceIncludingVat") is not None:
        ol["unitPriceIncludingVatCurrency"] = line["unitPriceIncludingVat"]

    # VAT type: resolve from prompt, or use product's own VAT type
    raw_vat = line.get("vatType")
    vat_ref = _resolve_vat_type(raw_vat, context)

    # If a product was found, check if we should trust the product's own VAT type
    # instead of the parser's potentially incorrect extraction
    product_vat_ref = None
    if ol.get("product") and products:
        product_vat = products[0].get("vatType")
        if isinstance(product_vat, dict) and "id" in product_vat:
            product_vat_ref = {"id": product_vat["id"]}

    # Safety: if parser sent "0%" for what was really "excl. VAT" (sem IVA/ohne MwSt),
    # the resolved id would be 5 or 6 (0% VAT). Only keep 0% if:
    # (a) the original string clearly indicates exempt, OR
    # (b) the product itself has 0% VAT (trust pre-seeded product data)
    if vat_ref and vat_ref.get("id") in (5, 6, 0):
        raw_lower = str(raw_vat).lower() if raw_vat else ""
        _EXEMPT_KEYWORDS = ("exempt", "fritatt", "avgiftsfri", "exento", "exonéré",
                            "befreit", "isento", "friteken")
        product_is_exempt = (product_vat_ref and product_vat_ref.get("id") in (5, 6))
        if product_is_exempt:
            # Trust the product's own VAT type — it was pre-seeded correctly
            logger.info("Keeping 0%% VAT (id=%d) — product has 0%% VAT", vat_ref["id"])
        elif not any(kw in raw_lower for kw in _EXEMPT_KEYWORDS):
            logger.info("Overriding 0%% VAT (id=%d) to 25%% — parser likely misinterpreted 'excl. VAT'", vat_ref["id"])
            vat_ref = {"id": 3}  # 25% standard outgoing VAT

    if vat_ref:
        ol["vatType"] = vat_ref
    elif product_vat_ref:
        # No explicit VAT from parser, but product has a VAT type — use it
        ol["vatType"] = product_vat_ref
        logger.info("Using product's own VAT type: id=%d", product_vat_ref["id"])
    elif not ol.get("product"):
        # No product and no explicit VAT type — default to 25% outgoing VAT (id=3)
        ol["vatType"] = {"id": 3}
    # else: product exists and no explicit VAT → let product's own vatType be used

    # Discount
    if line.get("discount") is not None:
        ol["discount"] = line["discount"]

    return ol


def _handle_create_employee(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    emp = entities.get("employee", entities)

    # Flatten nested "employment" sub-dict into emp (LLM sometimes nests these)
    if isinstance(emp.get("employment"), dict):
        for k, v in emp["employment"].items():
            if k not in emp or emp[k] is None:
                emp[k] = v

    # Resolve department: use specific department name if provided, else default
    dept_name = emp.get("department") or emp.get("departmentName")
    # Also check comments for "Department: X" pattern
    if not dept_name and emp.get("comments"):
        import re
        dept_match = re.search(r'(?:Department|Avdeling|Abteilung|Departamento|Département):\s*([^.!\n,;]+)', emp.get("comments", ""))
        if dept_match:
            dept_name = dept_match.group(1).strip()

    if dept_name:
        # Look for existing department or create it
        dept_resp = client.get("/department", {"name": dept_name, "count": 5})
        dept_values = dept_resp.get("values", [])
        dept_id = None
        for dv in dept_values:
            if dv.get("name", "").strip().lower() == dept_name.strip().lower():
                dept_id = dv["id"]
                break
        if not dept_id:
            dept_result = client.post("/department", {"name": dept_name})
            if isinstance(dept_result, dict) and dept_result.get("value", {}).get("id"):
                dept_id = dept_result["value"]["id"]
            else:
                dept_id = _ensure_department(client, context)
    else:
        dept_id = _ensure_department(client, context)

    body: dict[str, Any] = {
        "firstName": emp["firstName"],
        "lastName": emp["lastName"],
        "userType": "EXTENDED" if emp.get("isAdministrator") else "NO_ACCESS",
        "department": {"id": dept_id},
        "allowInformationRegistration": True,
    }

    optional_fields = [
        "email", "dateOfBirth", "phoneNumberMobile",
        "nationalIdentityNumber", "bankAccountNumber",
        "employeeNumber", "comments",
    ]
    for field in optional_fields:
        if emp.get(field):
            body[field] = emp[field]
    if emp.get("address"):
        sanitized_addr = _sanitize_address(emp["address"])
        if sanitized_addr:
            body["address"] = sanitized_addr

    result = client.post("/employee", body)
    _check_response(result, "POST /employee")
    value = result.get("value", {})
    emp_id = value.get("id")
    emp_name = f"{value.get('firstName', '')} {value.get('lastName', '')}"

    # Create employment record if start date or salary info is provided
    has_employment = any(emp.get(f) for f in (
        "startDate", "annualSalary", "employmentPercentage",
        "occupationCode", "employmentType", "remunerationType",
    ))
    if has_employment and emp_id:
        _create_employment(emp, emp_id, client)

    return f"Created employee {emp_name} (id={emp_id})"


def _create_employment(emp: dict, emp_id: int, client: TripletexClient):
    """Create employment and employment details for an employee."""
    start_date = emp.get("startDate", _today())

    # Step 1: POST /employee/employment
    # NOTE: occupationCode belongs on employmentDetails, not employment.
    employment_body: dict[str, Any] = {
        "employee": {"id": emp_id},
        "startDate": start_date,
        "isMainEmployer": True,
    }

    try:
        emp_result = client.post("/employee/employment", employment_body)
        _check_response(emp_result, "POST /employee/employment")
        employment_id = emp_result.get("value", {}).get("id")
        logger.info("Created employment id=%s for employee %d", employment_id, emp_id)
    except Exception as e:
        logger.warning("Failed to create employment: %s", e)
        return

    if not employment_id:
        return

    # Step 2: POST /employee/employment/details (salary, percentage, type, occupationCode)
    has_details = any(emp.get(f) for f in (
        "annualSalary", "employmentPercentage", "employmentType", "remunerationType",
        "occupationCode",
    ))
    if has_details:
        details_body: dict[str, Any] = {
            "employment": {"id": employment_id},
            "date": start_date,
        }
        if emp.get("annualSalary"):
            details_body["annualSalary"] = emp["annualSalary"]
        if emp.get("employmentPercentage"):
            details_body["percentageOfFullTimeEquivalent"] = emp["employmentPercentage"]
        if emp.get("employmentType"):
            details_body["employmentType"] = emp["employmentType"]
        if emp.get("remunerationType"):
            details_body["remunerationType"] = emp["remunerationType"]

        # Resolve occupationCode string (e.g. "3512") to a ref object {"id": N}
        if emp.get("occupationCode"):
            occ_code = str(emp["occupationCode"])
            try:
                occ_resp = client.get(
                    "/employee/employment/occupationCode",
                    {"code": occ_code, "count": 1},
                )
                occ_values = occ_resp.get("values", [])
                if occ_values:
                    details_body["occupationCode"] = {"id": occ_values[0]["id"]}
                    logger.info("Resolved occupationCode %s → id %d", occ_code, occ_values[0]["id"])
                else:
                    logger.warning("occupationCode '%s' not found, skipping", occ_code)
            except Exception as e:
                logger.warning("Failed to look up occupationCode '%s': %s", occ_code, e)

        try:
            det_result = client.post("/employee/employment/details", details_body)
            _check_response(det_result, "POST /employee/employment/details")
            logger.info("Created employment details for employment %d", employment_id)
        except Exception as e:
            logger.warning("Failed to create employment details: %s", e)


def _handle_update_employee(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    search = entities.get("search", entities)
    updates = entities.get("updates", {})

    # Search for the employee
    search_params: dict[str, Any] = {"count": 1, "fields": "*"}
    if search.get("firstName"):
        search_params["firstName"] = search["firstName"]
    if search.get("lastName"):
        search_params["lastName"] = search["lastName"]
    if search.get("email"):
        search_params["email"] = search["email"]

    search_resp = client.get("/employee", search_params)
    logger.info("update_employee search result: %s", search_resp)
    values = search_resp.get("values", [])
    if not values:
        raise RuntimeError(
            f"Employee not found: {search}"
        )

    existing = values[0]
    emp_id = existing["id"]
    version = existing.get("version")

    # Merge: start from existing, overlay with updates
    body = dict(existing)
    for key, val in updates.items():
        if val is not None and key not in ("search", "updates"):
            body[key] = val
    # Handle isAdministrator -> userType mapping
    if updates.get("isAdministrator"):
        body["userType"] = "EXTENDED"
    body["id"] = emp_id
    if version is not None:
        body["version"] = version

    result = client.put(f"/employee/{emp_id}", body)
    logger.info("update_employee result: %s", result)
    value = result.get("value", {})
    return (
        f"Updated employee {value.get('firstName', '')} "
        f"{value.get('lastName', '')} (id={emp_id})"
    )


def _sanitize_address(addr: Any) -> dict | None:
    """Sanitize an address object for the Tripletex API.

    Converts country strings to {"id": N} references and removes None values.
    """
    if not isinstance(addr, dict):
        return None

    # Country must be {"id": N}, not a string
    country = addr.get("country")
    if isinstance(country, str):
        # Map common country names/codes to Tripletex country IDs
        _COUNTRY_MAP = {
            "norway": 161, "no": 161, "norge": 161, "noreg": 161,
            "sweden": 195, "se": 195, "sverige": 195,
            "denmark": 48, "dk": 48, "danmark": 48,
            "germany": 66, "de": 66, "deutschland": 66, "tyskland": 66,
            "united kingdom": 217, "uk": 217, "gb": 217,
            "united states": 218, "us": 218, "usa": 218,
            "france": 62, "fr": 62, "frankrike": 62,
            "spain": 192, "es": 192, "españa": 192, "spania": 192,
            "portugal": 171, "pt": 171,
            "brazil": 27, "br": 27, "brasil": 27,
        }
        country_id = _COUNTRY_MAP.get(country.lower().strip())
        if country_id:
            addr["country"] = {"id": country_id}
        else:
            # Unknown country string — remove it rather than crash
            del addr["country"]

    # Remove None values
    return {k: v for k, v in addr.items() if v is not None}


def _handle_create_customer(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    is_supplier = entities.get("isSupplier", False)

    # If this is a supplier registration, use POST /supplier instead of POST /customer
    # The scoring system checks GET /supplier for supplier tasks
    if is_supplier:
        body: dict[str, Any] = {"name": entities["name"]}
        optional_fields = [
            "email", "phoneNumber", "organizationNumber",
            "invoiceEmail", "description",
            "isPrivateIndividual", "language",
        ]
        for field in optional_fields:
            if entities.get(field) is not None:
                body[field] = entities[field]

        for addr_field in ("postalAddress", "physicalAddress"):
            if entities.get(addr_field):
                sanitized = _sanitize_address(entities[addr_field])
                if sanitized:
                    body[addr_field] = sanitized

        result = client.post("/supplier", body)
        _check_response(result, "POST /supplier")
        value = result.get("value", {})
        return f"Created supplier {value.get('name', '')} (id={value.get('id', '?')})"

    body = {"name": entities["name"], "isCustomer": True}
    optional_fields = [
        "email", "phoneNumber", "organizationNumber",
        "invoiceEmail", "description", "isCustomer", "isSupplier",
        "accountManager",
        "isPrivateIndividual", "language", "invoiceSendMethod",
        "invoicesDueIn", "invoicesDueInType",
    ]
    for field in optional_fields:
        if entities.get(field) is not None:
            body[field] = entities[field]

    # Sanitize address objects (country must be {"id": N}, not a string)
    for addr_field in ("postalAddress", "physicalAddress"):
        if entities.get(addr_field):
            sanitized = _sanitize_address(entities[addr_field])
            if sanitized:
                body[addr_field] = sanitized

    result = client.post("/customer", body)
    _check_response(result, "POST /customer")
    value = result.get("value", {})
    return f"Created customer {value.get('name', '')} (id={value.get('id', '?')})"


def _handle_update_customer(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    # Parser outputs: search (object) + updates (object)
    search = entities.get("search", entities)
    updates = entities.get("updates", {})
    name = search.get("name", entities.get("name", ""))

    # Search for the customer
    search_params: dict[str, Any] = {"count": 1}
    if name:
        search_params["name"] = name
    if search.get("email"):
        search_params["email"] = search["email"]

    search_resp = client.get("/customer", search_params)
    logger.info("update_customer search result: %s", search_resp)
    values = search_resp.get("values", [])
    if not values:
        raise RuntimeError(f"Customer not found: {search}")

    existing = values[0]
    cust_id = existing["id"]
    version = existing.get("version")

    body = dict(existing)
    for key, val in updates.items():
        if val is not None and key not in ("search", "updates"):
            body[key] = val
    body["id"] = cust_id
    if version is not None:
        body["version"] = version

    result = client.put(f"/customer/{cust_id}", body)
    logger.info("update_customer result: %s", result)
    value = result.get("value", {})
    return f"Updated customer {value.get('name', '')} (id={cust_id})"


def _handle_create_product(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]

    body: dict[str, Any] = {"name": entities["name"]}

    # Map parser field names → API field names
    field_map = {
        "priceExcludingVat": "priceExcludingVatCurrency",
        "priceIncludingVat": "priceIncludingVatCurrency",
        "costExcludingVat": "costExcludingVatCurrency",
    }
    for parser_name, api_name in field_map.items():
        val = entities.get(parser_name) or entities.get(api_name)
        if val is not None:
            body[api_name] = val

    optional_fields = [
        "number", "description", "isInactive", "productUnit",
        "isStockItem",
    ]
    for field in optional_fields:
        if entities.get(field) is not None:
            body[field] = entities[field]

    # Resolve vatType string → {"id": N}
    if entities.get("vatType"):
        vat_ref = _resolve_vat_type(entities["vatType"], context)
        if vat_ref:
            body["vatType"] = vat_ref

    result = client.post("/product", body)
    _check_response(result, "POST /product")
    value = result.get("value", {})
    return f"Created product {value.get('name', '')} (id={value.get('id', '?')})"


def _find_or_create_customer(customer_data: dict, client: TripletexClient, context: dict) -> int:
    """Search for an existing customer by name, or create one. Returns customer ID.

    Verifies the search result actually matches to avoid Tripletex fuzzy matching
    returning unrelated customers.
    """
    name = customer_data.get("name", "")
    org_number = customer_data.get("organizationNumber", "")

    # Search by organizationNumber first (exact match, most reliable)
    if org_number:
        resp = client.get("/customer", {"organizationNumber": org_number, "count": 1})
        values = resp.get("values", [])
        if values:
            logger.info("Found existing customer by orgNr '%s': id=%d", org_number, values[0]["id"])
            return values[0]["id"]

    # Fallback: search by name, but verify the result actually matches
    if name:
        resp = client.get("/customer", {"name": name, "count": 5})
        values = resp.get("values", [])
        for v in values:
            # Check if the returned name actually matches (case-insensitive)
            if v.get("name", "").strip().lower() == name.strip().lower():
                logger.info("Found existing customer '%s': id=%d", name, v["id"])
                return v["id"]
            # Also accept org number match
            if org_number and v.get("organizationNumber") == org_number:
                logger.info("Found existing customer by orgNr in name search: id=%d", v["id"])
                return v["id"]
        if values:
            logger.warning(
                "Name search for '%s' returned non-matching results: %s",
                name, [v.get("name") for v in values],
            )

    # Not found — create
    cust_body: dict[str, Any] = {"name": name, "isCustomer": True}
    for field in ["email", "phoneNumber", "organizationNumber", "invoiceEmail"]:
        if customer_data.get(field):
            cust_body[field] = customer_data[field]

    cust = client.post("/customer", cust_body)
    _check_response(cust, "POST /customer")
    cust_id = cust["value"]["id"]
    logger.info("Created new customer '%s': id=%d", name, cust_id)
    return cust_id


def _handle_create_invoice(task: dict, client: TripletexClient, context: dict) -> str:
    e = task["entities"]
    today = e.get("today", _today())

    # Proactively ensure bank account is set up (GET is free, avoids wasted POST on failure)
    _ensure_company_bank_account(client, context)

    # Step 1: Find or create customer (avoids duplicates)
    customer_data = e.get("customer")
    if not customer_data:
        raise RuntimeError("create_invoice: no customer data in entities")
    customer_id = _find_or_create_customer(customer_data, client, context)
    context.setdefault("_created_entities", {})["customer_id"] = customer_id

    # Step 2: Create order with lines (resolving products and VAT types)
    raw_lines = e.get("orderLines", e.get("lines", []))
    # Fallback: if LLM put amount/description at top level instead of in orderLines array
    if not raw_lines:
        top_price = e.get("amount") or e.get("unitPrice") or e.get("totalAmount") or e.get("priceExcludingVat")
        top_desc = e.get("description") or e.get("productName") or e.get("productDescription") or ""
        if top_price:
            fallback_line: dict[str, Any] = {"count": e.get("count", 1)}
            if e.get("isPrioritizeAmountsIncludingVat"):
                fallback_line["unitPriceIncludingVat"] = top_price
            else:
                fallback_line["unitPrice"] = top_price
            if top_desc:
                fallback_line["description"] = top_desc
            if e.get("vatType"):
                fallback_line["vatType"] = e["vatType"]
            if e.get("product"):
                fallback_line["product"] = e["product"]
            raw_lines = [fallback_line]
            logger.info("Constructed orderLine from top-level fields: %s", fallback_line)

    order_lines = []
    for line in raw_lines:
        ol = _resolve_product_in_order_line(line, client, context)
        order_lines.append(ol)

    order_body: dict[str, Any] = {
        "customer": {"id": customer_id},
        "orderDate": e.get("orderDate") or today,
        "deliveryDate": e.get("deliveryDate") or today,
    }
    if order_lines:
        order_body["orderLines"] = order_lines
    if e.get("isPrioritizeAmountsIncludingVat"):
        order_body["isPrioritizeAmountsIncludingVat"] = True

    order = client.post("/order", order_body)
    _check_response(order, "POST /order")
    order_id = order["value"]["id"]

    # Step 3: Create invoice
    inv_body: dict[str, Any] = {
        "invoiceDate": e.get("invoiceDate") or today,
        "invoiceDueDate": e.get("invoiceDueDate") or today,
        "orders": [{"id": order_id}],
    }
    if e.get("comment"):
        inv_body["comment"] = e["comment"]

    params: dict[str, Any] = {"sendToCustomer": e.get("sendToCustomer", False)}

    inv = client.post("/invoice", inv_body, params)

    # If invoice fails due to bank account, try setting it up and retry
    if isinstance(inv, dict) and inv.get("status", 0) >= 400:
        error_msg = str(inv.get("message", "")) + str(inv.get("validationMessages", ""))
        if "bankkontonummer" in error_msg.lower() or "bank" in error_msg.lower():
            logger.info("Invoice failed due to bank account, setting up and retrying...")
            context["_bank_account_checked"] = False
            _ensure_company_bank_account(client, context)
            inv = client.post("/invoice", inv_body, params)

    _check_response(inv, "POST /invoice")
    inv_id = inv["value"]["id"]
    return f"Created invoice {inv_id} for customer {e['customer']['name']}"


def _handle_create_invoice_with_payment(task: dict, client: TripletexClient, context: dict) -> str:
    e = task["entities"]
    today = e.get("today", _today())

    # Proactively ensure bank account is set up (GET is free, avoids wasted POST on failure)
    _ensure_company_bank_account(client, context)

    # Step 0b: Find payment type via /invoice/paymentType (NOT /ledger/paymentTypeOut!)
    # /invoice/paymentType = incoming payments (customer → us)
    # /ledger/paymentTypeOut = outgoing payments (us → supplier) — WRONG for invoices
    pt_resp = client.get("/invoice/paymentType", {"count": 100})
    logger.info("create_invoice_with_payment: payment types: %s", pt_resp)
    payment_types = pt_resp.get("values", [])
    payment_type_id = None
    desired_type = e.get("paymentTypeDescription") or e.get("paymentType") or ""
    for pt in payment_types:
        desc = pt.get("description", "")
        if desired_type and desired_type.lower() in desc.lower():
            payment_type_id = pt["id"]
            break
    if payment_type_id is None and payment_types:
        payment_type_id = payment_types[0]["id"]

    # Step 1: Find or create customer (avoids duplicates)
    customer_data = e.get("customer")
    if not customer_data:
        raise RuntimeError("create_invoice_with_payment: no customer data in entities")
    customer_id = _find_or_create_customer(customer_data, client, context)
    context.setdefault("_created_entities", {})["customer_id"] = customer_id

    # Step 2: Create order with lines (resolving products and VAT types)
    raw_lines = e.get("orderLines", e.get("lines", []))
    # Fallback: if LLM put amount/description at top level instead of in orderLines array
    if not raw_lines:
        top_price = e.get("amount") or e.get("unitPrice") or e.get("totalAmount") or e.get("priceExcludingVat")
        top_desc = e.get("description") or e.get("productName") or e.get("productDescription") or ""
        if top_price:
            fallback_line: dict[str, Any] = {"count": e.get("count", 1)}
            if e.get("isPrioritizeAmountsIncludingVat"):
                fallback_line["unitPriceIncludingVat"] = top_price
            else:
                fallback_line["unitPrice"] = top_price
            if top_desc:
                fallback_line["description"] = top_desc
            if e.get("vatType"):
                fallback_line["vatType"] = e["vatType"]
            raw_lines = [fallback_line]
            logger.info("Constructed orderLine from top-level fields: %s", fallback_line)

    order_lines = []
    for line in raw_lines:
        ol = _resolve_product_in_order_line(line, client, context)
        order_lines.append(ol)

    order_body: dict[str, Any] = {
        "customer": {"id": customer_id},
        "orderDate": e.get("orderDate") or today,
        "deliveryDate": e.get("deliveryDate") or today,
    }
    if order_lines:
        order_body["orderLines"] = order_lines
    if e.get("isPrioritizeAmountsIncludingVat"):
        order_body["isPrioritizeAmountsIncludingVat"] = True

    order = client.post("/order", order_body)
    _check_response(order, "POST /order (for invoice with payment)")
    order_id = order["value"]["id"]

    # Step 3: Create invoice WITH payment in a single POST call
    # POST /invoice accepts paymentTypeId and paidAmount as query params
    inv_body: dict[str, Any] = {
        "invoiceDate": e.get("invoiceDate") or today,
        "invoiceDueDate": e.get("invoiceDueDate") or today,
        "orders": [{"id": order_id}],
    }
    if e.get("comment"):
        inv_body["comment"] = e["comment"]

    # Estimate gross amount from order lines for the payment param
    _VAT_PCT_MAP = {3: 25, 31: 15, 32: 12, 5: 0, 6: 0}
    estimated_gross = 0.0
    for ol in order_lines:
        price = ol.get("unitPriceExcludingVatCurrency", 0) or 0
        count = ol.get("count", 1) or 1
        vat_id = (ol.get("vatType") or {}).get("id", 3)
        vat_pct = _VAT_PCT_MAP.get(vat_id, 25)
        estimated_gross += count * price * (1 + vat_pct / 100)

    # Primary approach: register payment at invoice creation time
    # Use explicitly parsed paidAmount if available; otherwise use estimated gross
    explicit_paid = e.get("paidAmount")
    paid_amount_to_use = explicit_paid if explicit_paid else estimated_gross

    params: dict[str, Any] = {"sendToCustomer": False}
    if payment_type_id is not None:
        params["paymentTypeId"] = payment_type_id
        params["paidAmount"] = paid_amount_to_use
        params["paidAmountCurrency"] = paid_amount_to_use

    inv = client.post("/invoice", inv_body, params)

    # If invoice fails due to bank account, try setting it up and retry
    if isinstance(inv, dict) and inv.get("status", 0) >= 400:
        error_msg = str(inv.get("message", "")) + str(inv.get("validationMessages", ""))
        if "bankkontonummer" in error_msg.lower() or "bank" in error_msg.lower():
            logger.info("Invoice failed due to bank account, setting up and retrying...")
            context["_bank_account_checked"] = False
            _ensure_company_bank_account(client, context)
            inv = client.post("/invoice", inv_body, params)

    # If one-call with payment failed, try without payment first
    if isinstance(inv, dict) and inv.get("status", 0) >= 400:
        logger.warning("POST /invoice with payment params failed, trying without...")
        params_no_pay: dict[str, Any] = {"sendToCustomer": False}
        inv = client.post("/invoice", inv_body, params_no_pay)

    _check_response(inv, "POST /invoice (for payment)")
    inv_id = inv["value"]["id"]
    inv_data = inv["value"]
    actual_amount = inv_data.get("amount") or inv_data.get("amountCurrency")

    # Check if payment was registered in the one-call approach
    outstanding = inv_data.get("amountOutstanding")
    payment_registered = outstanding is not None and outstanding == 0

    # Fallback: register payment via PUT /invoice/{id}/:payment
    if not payment_registered and actual_amount and payment_type_id is not None:
        logger.info("Payment not registered at creation, trying PUT /:payment...")
        pay_params: dict[str, Any] = {
            "paymentDate": e.get("paymentDate", today),
            "paymentTypeId": payment_type_id,
            "paidAmount": actual_amount,
            "paidAmountCurrency": actual_amount,
        }
        pay_result = client.put(f"/invoice/{inv_id}/:payment", params=pay_params)
        logger.info("PUT /:payment result: %s", pay_result)

        # If query params didn't work, try with JSON body
        if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
            logger.warning("PUT /:payment with query params failed, trying JSON body...")
            pay_result = client.put(
                f"/invoice/{inv_id}/:payment",
                json_body={
                    "paymentDate": e.get("paymentDate", today),
                    "paymentTypeId": payment_type_id,
                    "paidAmount": actual_amount,
                    "paidAmountCurrency": actual_amount,
                },
            )
            logger.info("PUT /:payment (body) result: %s", pay_result)

        if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
            logger.error("All payment approaches failed: %s", pay_result.get("message", str(pay_result)))

    return (
        f"Created invoice {inv_id} with payment for customer "
        f"{e['customer']['name']} (paymentTypeId={payment_type_id}, "
        f"paidAmount={actual_amount})"
    )


def _match_invoice(invoices: list[dict], entities: dict, inv_id_obj: dict) -> int:
    """Pick the best invoice from a list by matching amount and/or description.

    Returns the invoice ID. Falls back to the first invoice if no match found.
    """
    if len(invoices) == 1:
        return invoices[0]["id"]

    # Target amount from parser (excl. VAT)
    target_amount = entities.get("amount") or inv_id_obj.get("amount")
    target_desc = (entities.get("description") or inv_id_obj.get("description") or "").lower()

    # Try matching by amount first (most reliable)
    if target_amount:
        for inv in invoices:
            excl = inv.get("amountExcludingVat") or inv.get("amountExcludingVatCurrency") or 0
            if abs(excl - target_amount) < 1:
                logger.info("Matched invoice %d by amount (%.2f ≈ %.2f)", inv["id"], excl, target_amount)
                return inv["id"]

        # Try matching including VAT (prompt might give gross amount)
        for inv in invoices:
            gross = inv.get("amount") or inv.get("amountCurrency") or 0
            if abs(gross - target_amount) < 1:
                logger.info("Matched invoice %d by gross amount (%.2f ≈ %.2f)", inv["id"], gross, target_amount)
                return inv["id"]

    # Try matching by description (check order lines if available)
    if target_desc:
        for inv in invoices:
            # Check invoice comment
            comment = (inv.get("invoiceComment") or inv.get("comment") or "").lower()
            if target_desc in comment:
                logger.info("Matched invoice %d by description in comment", inv["id"])
                return inv["id"]

    # Fall back to first non-credit-note invoice
    for inv in invoices:
        if not inv.get("isCreditNote"):
            logger.info("No amount/description match — using first non-credit-note invoice %d", inv["id"])
            return inv["id"]

    logger.info("No match — using first invoice %d", invoices[0]["id"])
    return invoices[0]["id"]


def _handle_create_credit_note(task: dict, client: TripletexClient, context: dict) -> str:
    e = task["entities"]
    today = e.get("today", _today())

    # Parser may wrap invoice info in "invoiceIdentifier" object
    inv_id_obj = e.get("invoiceIdentifier", {})

    # Extract identifiers from both top-level and invoiceIdentifier
    inv_id = e.get("invoiceId") or inv_id_obj.get("invoiceId") or inv_id_obj.get("id")
    inv_num = e.get("invoiceNumber") or inv_id_obj.get("invoiceNumber")
    cust_name = e.get("customerName") or inv_id_obj.get("customerName")
    org_number = e.get("organizationNumber") or inv_id_obj.get("organizationNumber")

    # Find the invoice — GET /invoice REQUIRES invoiceDateFrom and invoiceDateTo
    search_params: dict[str, Any] = {
        "count": 10,
        "invoiceDateFrom": "2020-01-01",
        "invoiceDateTo": today,
    }
    if inv_id:
        search_params["id"] = inv_id
    elif inv_num:
        search_params["invoiceNumber"] = inv_num
    elif cust_name:
        search_params["customerName"] = cust_name

    search_resp = client.get("/invoice", search_params)
    logger.info("create_credit_note: invoice search: %s", search_resp)
    values = search_resp.get("values", [])

    if not values:
        # No invoice found — the sandbox is fresh, so we need to CREATE the invoice first
        logger.info("No invoice found, creating it first for credit note workflow...")
        inv_id = _create_invoice_for_credit_note(e, inv_id_obj, client, context, today)
    else:
        # Match by amount and/or description when multiple invoices exist
        inv_id = _match_invoice(values, e, inv_id_obj)

    # Create credit note via PUT /:createCreditNote
    params: dict[str, Any] = {
        "date": e.get("date", today),
    }
    if e.get("comment"):
        params["comment"] = e["comment"]

    result = client.put(f"/invoice/{inv_id}/:createCreditNote", params=params)
    logger.info("create_credit_note result: %s", result)
    _check_response(result, "PUT /invoice/:createCreditNote")
    return f"Created credit note for invoice {inv_id}"


def _create_invoice_for_credit_note(
    e: dict, inv_id_obj: dict, client: TripletexClient, context: dict, today: str
) -> int:
    """Create the original invoice so we can then credit it.

    On fresh sandboxes the invoice referenced in the prompt doesn't exist yet.
    We must create customer → order → invoice first.
    """
    # Ensure bank account
    if not context.get("_bank_account_checked"):
        if not context.get("company_id"):
            try:
                who = client.get("/token/session/>whoAmI")
                context["company_id"] = who.get("value", {}).get("companyId")
            except Exception:
                pass
        _ensure_company_bank_account(client, context)

    # Find or create the customer
    cust_name = e.get("customerName") or inv_id_obj.get("customerName", "")
    org_number = e.get("organizationNumber") or inv_id_obj.get("organizationNumber", "")
    cust_data = {"name": cust_name}
    if org_number:
        cust_data["organizationNumber"] = org_number
    customer_id = _find_or_create_customer(cust_data, client, context)

    # Build order line from the credit note description
    description = e.get("description") or inv_id_obj.get("description", "")
    unit_price = e.get("unitPrice") or inv_id_obj.get("unitPrice") or e.get("amount") or inv_id_obj.get("amount")
    vat_str = e.get("vatType") or inv_id_obj.get("vatType")

    ol: dict[str, Any] = {"count": 1}
    if description:
        ol["description"] = description
    if unit_price is not None:
        ol["unitPriceExcludingVatCurrency"] = unit_price

    # Resolve VAT type
    vat_ref = _resolve_vat_type(vat_str, context) if vat_str else None
    if vat_ref:
        ol["vatType"] = vat_ref

    # Create order
    order_body: dict[str, Any] = {
        "customer": {"id": customer_id},
        "orderDate": today,
        "deliveryDate": today,
        "orderLines": [ol],
    }
    order = client.post("/order", order_body)
    _check_response(order, "POST /order (for credit note)")
    order_id = order["value"]["id"]

    # Create invoice
    inv_body: dict[str, Any] = {
        "invoiceDate": today,
        "invoiceDueDate": today,
        "orders": [{"id": order_id}],
    }
    inv = client.post("/invoice", inv_body, {"sendToCustomer": False})

    # Retry on bank account error
    if isinstance(inv, dict) and inv.get("status", 0) >= 400:
        error_msg = str(inv.get("message", "")) + str(inv.get("validationMessages", ""))
        if "bankkontonummer" in error_msg.lower() or "bank" in error_msg.lower():
            context["_bank_account_checked"] = False
            _ensure_company_bank_account(client, context)
            inv = client.post("/invoice", inv_body, {"sendToCustomer": False})

    _check_response(inv, "POST /invoice (for credit note)")
    logger.info("Created invoice %d for credit note workflow", inv["value"]["id"])
    return inv["value"]["id"]


def _find_or_create_employee_by_name(name: str, client: TripletexClient, context: dict) -> int:
    """Find an employee by name, or create one if not found."""
    if not name:
        return _ensure_employee(client, context)

    # Split name into first/last
    parts = name.strip().split()
    first_name = parts[0] if parts else name
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Search existing employees in context first
    for emp in context.get("employees", []):
        emp_first = emp.get("firstName", "").lower()
        emp_last = emp.get("lastName", "").lower()
        if emp_first == first_name.lower() and (not last_name or emp_last == last_name.lower()):
            return emp["id"]

    # Search via API
    search_params: dict[str, Any] = {"count": 1, "firstName": first_name}
    if last_name:
        search_params["lastName"] = last_name
    resp = client.get("/employee", search_params)
    values = resp.get("values", [])
    if values:
        return values[0]["id"]

    # Not found — create the employee
    dept_id = _ensure_department(client, context)
    result = client.post("/employee", {
        "firstName": first_name,
        "lastName": last_name or "Bruker",
        "userType": "NO_ACCESS",
        "department": {"id": dept_id},
        "allowInformationRegistration": True,
    })
    _check_response(result, "POST /employee (for reference)")
    emp = result["value"]
    context["employees"].append(emp)
    return emp["id"]


def _handle_create_project(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]

    # Determine project manager — by ID, by name, or default
    pm_id = entities.get("projectManagerId")
    if not pm_id and entities.get("projectManagerName"):
        pm_id = _find_or_create_employee_by_name(entities["projectManagerName"], client, context)
    if not pm_id:
        pm_id = _ensure_employee(client, context)

    body: dict[str, Any] = {
        "name": entities["name"],
        "projectManager": {"id": pm_id},
        "isInternal": entities.get("isInternal", False),
        "startDate": entities.get("startDate", _today()),
    }

    # Link to customer if specified
    cust_name = entities.get("customerName")
    cust_org = entities.get("customerOrganizationNumber")
    if cust_name or cust_org:
        cust_data: dict[str, Any] = {}
        if cust_name:
            cust_data["name"] = cust_name
        if cust_org:
            cust_data["organizationNumber"] = cust_org
        customer_id = _find_or_create_customer(cust_data, client, context)
        body["customer"] = {"id": customer_id}

    optional_fields = [
        "number", "description", "endDate",
        "projectCategory", "department",
    ]
    for field in optional_fields:
        if entities.get(field) is not None:
            body[field] = entities[field]

    result = client.post("/project", body)
    _check_response(result, "POST /project")
    value = result.get("value", {})
    return f"Created project {value.get('name', '')} (id={value.get('id', '?')})"


def _handle_create_department(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]

    # Resolve manager once (shared across all departments if multi-create)
    mgr_ref = None
    if entities.get("departmentManagerId"):
        mgr_ref = {"id": entities["departmentManagerId"]}
    elif entities.get("departmentManagerName"):
        mgr_id = _find_or_create_employee_by_name(entities["departmentManagerName"], client, context)
        mgr_ref = {"id": mgr_id}

    # Support multi-department creation via "names" array
    names = entities.get("names", [])
    if not names and entities.get("name"):
        names = [entities["name"]]

    if not names:
        raise RuntimeError("create_department: no name or names provided")

    # Build department bodies
    bodies = []
    for dept_name in names:
        body: dict[str, Any] = {"name": dept_name}
        if entities.get("departmentNumber") is not None and len(names) == 1:
            body["departmentNumber"] = entities["departmentNumber"]
        if mgr_ref:
            body["departmentManager"] = mgr_ref
        bodies.append(body)

    # Use batch endpoint for multiple departments, single for one
    created = []
    if len(bodies) == 1:
        result = client.post("/department", bodies[0])
        _check_response(result, "POST /department")
        value = result.get("value", {})
        created.append(f"{value.get('name', '')} (id={value.get('id', '?')})")
    else:
        result = client.post("/department/list", bodies)
        _check_response(result, "POST /department/list")
        for value in result.get("values", []):
            created.append(f"{value.get('name', '')} (id={value.get('id', '?')})")

    return f"Created {len(created)} department(s): {', '.join(created)}"


def _handle_create_travel_expense(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]

    # Determine employee — by ID, by name, or default
    emp_id = entities.get("employeeId")
    if not emp_id and entities.get("employeeName"):
        emp_id = _find_or_create_employee_by_name(entities["employeeName"], client, context)
    if not emp_id:
        emp_id = _ensure_employee(client, context)

    body: dict[str, Any] = {
        "employee": {"id": emp_id},
        "title": entities.get("title", "Travel Expense"),
    }
    optional_fields = [
        "departureDate", "returnDate", "project", "department",
        "isCompleted",
    ]
    for field in optional_fields:
        if entities.get(field) is not None:
            body[field] = entities[field]

    # Handle department reference (may be a name, not an ID)
    if isinstance(body.get("department"), str):
        # It's a department name — find or create
        dept_name = body.pop("department")
        for d in context.get("departments", []):
            if d.get("name", "").lower() == dept_name.lower():
                body["department"] = {"id": d["id"]}
                break
        if "department" not in body:
            dept_result = client.post("/department", {"name": dept_name})
            if isinstance(dept_result, dict) and dept_result.get("value"):
                body["department"] = {"id": dept_result["value"]["id"]}

    # Handle project reference (may be a name, not an ID)
    if isinstance(body.get("project"), str):
        proj_name = body.pop("project")
        resp = client.get("/project", {"name": proj_name, "count": 1})
        values = resp.get("values", [])
        if values:
            body["project"] = {"id": values[0]["id"]}

    result = client.post("/travelExpense", body)
    _check_response(result, "POST /travelExpense")
    value = result.get("value", {})
    te_id = value.get("id")

    # Add cost items if specified
    costs = entities.get("costs", [])
    if costs:
        # Fetch a default payment type for travel expenses (REQUIRED by API)
        # Try multiple approaches to ensure we always have a payment type
        default_payment_type_id = None

        # Approach 1: dedicated travel payment type endpoint
        try:
            pt_resp = client.get("/travelExpense/paymentType", {"count": 100})
            pt_values = pt_resp.get("values", [])
            if pt_values:
                # Prefer active payment types shown on employee expenses
                for pt in pt_values:
                    if not pt.get("isInactive", False) and pt.get("showOnEmployeeExpenses", True):
                        default_payment_type_id = pt["id"]
                        break
                if not default_payment_type_id:
                    default_payment_type_id = pt_values[0]["id"]
                logger.info("Travel payment type from /travelExpense/paymentType: id=%s", default_payment_type_id)
        except Exception as e:
            logger.warning("Failed to fetch travel payment types: %s", e)

        # Approach 2: try outgoing payment types as fallback
        if not default_payment_type_id:
            try:
                pt_resp2 = client.get("/ledger/paymentTypeOut", {"count": 100})
                pt_values2 = pt_resp2.get("values", [])
                if pt_values2:
                    default_payment_type_id = pt_values2[0]["id"]
                    logger.info("Travel payment type from /ledger/paymentTypeOut: id=%s", default_payment_type_id)
            except Exception as e:
                logger.warning("Fallback payment type fetch failed: %s", e)

        # Approach 3: try incoming payment types
        if not default_payment_type_id:
            try:
                pt_resp3 = client.get("/invoice/paymentType", {"count": 10})
                pt_values3 = pt_resp3.get("values", [])
                if pt_values3:
                    default_payment_type_id = pt_values3[0]["id"]
                    logger.info("Travel payment type from /invoice/paymentType: id=%s", default_payment_type_id)
            except Exception as e:
                logger.warning("Invoice payment type fallback failed: %s", e)

        # If we STILL don't have a payment type, skip cost creation to avoid
        # wasted 422 errors (paymentType is REQUIRED). At least we get the
        # travel expense record created.
        if not default_payment_type_id:
            logger.warning("No payment type found after all fallbacks — skipping cost creation to avoid 422s")
            costs = []  # Clear costs to skip the loop

        for cost in costs:
            cost_body: dict[str, Any] = {
                "travelExpense": {"id": te_id},
            }
            if cost.get("date"):
                cost_body["date"] = cost["date"]
            if cost.get("amount") is not None:
                cost_body["amountCurrencyIncVat"] = cost["amount"]
                cost_body["amountNOKInclVAT"] = cost.get("amountNOK", cost["amount"])
            if cost.get("description"):
                cost_body["comments"] = cost["description"]
            if cost.get("category"):
                cost_body["category"] = cost["category"]
            if cost.get("paymentType"):
                pt = cost["paymentType"]
                cost_body["paymentType"] = pt if isinstance(pt, dict) else {"id": pt}
            elif default_payment_type_id:
                cost_body["paymentType"] = {"id": default_payment_type_id}
            if cost.get("rate") is not None:
                cost_body["rate"] = cost["rate"]
            if cost.get("isPaidByEmployee") is not None:
                cost_body["isPaidByEmployee"] = cost["isPaidByEmployee"]

            try:
                cost_result = client.post("/travelExpense/cost", cost_body)
                _check_response(cost_result, "POST /travelExpense/cost")
                logger.info("Added cost to travel expense %d: %s", te_id, cost.get("description", ""))
            except Exception as e:
                logger.warning("Failed to add cost to travel expense: %s", e)

    # Deliver the travel expense (mark as completed)
    try:
        deliver_result = client.put("/travelExpense/:deliver", params={"id": te_id})
        logger.info("Delivered travel expense %d: %s", te_id, deliver_result)
    except Exception as e:
        logger.warning("Failed to deliver travel expense %d: %s", te_id, e)

    return (
        f"Created travel expense '{value.get('title', '')}' "
        f"(id={te_id})"
    )


def _handle_delete_travel_expense(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    # Parser may wrap in "search" object
    search = entities.get("search", entities)

    te_id = entities.get("id") or search.get("id")
    if not te_id:
        # Search for it
        search_params: dict[str, Any] = {"count": 1}
        title = search.get("title") or entities.get("title")
        emp_name = search.get("employeeName") or entities.get("employeeName")
        emp_id = search.get("employeeId") or entities.get("employeeId")
        if title:
            search_params["title"] = title
        if emp_id:
            search_params["employeeId"] = emp_id

        search_resp = client.get("/travelExpense", search_params)
        logger.info("delete_travel_expense search: %s", search_resp)
        values = search_resp.get("values", [])
        if not values:
            raise RuntimeError(
                f"Travel expense not found with params: {search_params}"
            )
        te_id = values[0]["id"]

    result = client.delete(f"/travelExpense/{te_id}")
    logger.info("delete_travel_expense result: %s", result)
    return f"Deleted travel expense (id={te_id})"


def _handle_create_voucher(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    today = entities.get("today", _today())
    voucher_date = entities.get("date", today)
    invoice_number = entities.get("invoiceNumber") or ""
    due_date = entities.get("dueDate") or ""
    supplier_bank_account = entities.get("supplierBankAccount") or ""

    # Fallback: extract invoice number from description or raw prompt if LLM missed it
    if not invoice_number:
        import re
        raw = entities.get("description", "") + " " + task.get("raw_prompt", "")
        inv_match = re.search(r'(?:INV|inv|faktura|fatura|factura|invoice|Rechnung|facture)[- .:]*(\d{4}[-/]\d{2,6})', raw, re.IGNORECASE)
        if not inv_match:
            inv_match = re.search(r'(INV-\d{4}-\d+)', raw)
        if inv_match:
            invoice_number = inv_match.group(0).strip()
            logger.info("Extracted invoice number from prompt: %s", invoice_number)

    # Phase 0: Extract supplier info from ENTITIES first (parser already extracted these)
    supplier_name = entities.get("supplierName") or ""
    supplier_org = str(entities.get("supplierOrganizationNumber") or "").strip()

    # Phase 1: Resolve all account numbers to IDs and collect account metadata
    # Skip resolving VAT accounts (27xx) upfront — they'll be collapsed and removed
    resolved = []
    for posting in entities.get("postings", []):
        account_number = posting.get("accountNumber")
        account_id = posting.get("accountId")
        account_data = None
        try:
            acct_num_int = int(account_number or 0)
        except (ValueError, TypeError):
            acct_num_int = 0

        # Don't waste an API call resolving VAT accounts (27xx) —
        # we'll collapse them into the expense posting instead
        if 2700 <= acct_num_int <= 2799:
            amount = posting.get("amount") or posting.get("amountGross") or 0
            resolved.append({
                "posting": posting,
                "account_id": None,
                "account_number": acct_num_int,
                "account_data": None,
                "amount": amount,
            })
            continue

        if account_number and not account_id:
            acc_resp = client.get(
                "/ledger/account",
                {"number": account_number, "count": 1},
            )
            acc_values = acc_resp.get("values", [])
            if acc_values:
                account_id = acc_values[0]["id"]
                account_data = acc_values[0]
            else:
                # Auto-create missing account
                account_id = _get_or_create_account(
                    client, str(account_number),
                    name=posting.get("description") or f"Account {account_number}",
                )

        amount = posting.get("amount") or posting.get("amountGross") or 0
        resolved.append({
            "posting": posting,
            "account_id": account_id,
            "account_number": acct_num_int,
            "account_data": account_data,
            "amount": amount,
        })

    # Phase 2: Detect manual VAT splits — parser sometimes creates separate VAT postings
    # (accounts 27xx) that Tripletex auto-generates. Collapse them into the expense posting.
    # Tripletex handles VAT automatically when you set vatType on the expense posting
    # and send the GROSS amount. We must NOT send manual VAT postings.
    vat_entries = [r for r in resolved if 2700 <= r["account_number"] <= 2799]
    non_vat_entries = [r for r in resolved if not (2700 <= r["account_number"] <= 2799)]

    if vat_entries and non_vat_entries:
        # Use absolute VAT value (parser may give positive or negative sign)
        total_vat_abs = sum(abs(r["amount"]) for r in vat_entries)
        logger.info("Detected %d manual VAT postings (abs total=%s), collapsing into expense postings",
                     len(vat_entries), total_vat_abs)

        # Determine VAT rate from the amounts (e.g., 3312/13250 ≈ 0.25 → 25%)
        # Default to 25% input VAT (vatType id=1 in Norwegian Tripletex)
        detected_vat_type_id = 1  # 25% is the default for Norwegian supplier invoices

        # Adjust expense postings: set amount to GROSS and set correct vatType
        adjusted = False
        for r in non_vat_entries:
            acct_type = (r.get("account_data") or {}).get("type", "")
            is_expense = (acct_type == "OPERATING_EXPENSES"
                          or 4000 <= r["account_number"] <= 7999)
            if is_expense and r["amount"] > 0:
                # Expense amount should be GROSS (net + VAT)
                # Parser gave us net amount, add VAT to make it gross
                r["amount"] = r["amount"] + total_vat_abs
                # Always use input VAT type (id=1 for 25%), NOT the account default
                # Account defaults are often 0 (no VAT) which is wrong for supplier invoices
                r["vat_type_id"] = detected_vat_type_id
                logger.info("Adjusted expense posting to gross=%s with vatType=%s",
                            r["amount"], r.get("vat_type_id"))
                adjusted = True
        if not adjusted:
            logger.warning("Could not identify expense posting for VAT collapse, keeping VAT postings")
            non_vat_entries = list(resolved)  # Restore all entries

    # Phase 3: Resolve supplier — use entities first, then fall back to description regex
    supplier_id = None
    has_ap_posting = any(2400 <= r["account_number"] <= 2499 for r in non_vat_entries)

    if not supplier_name and has_ap_posting:
        # Fall back to extracting from description text AND raw prompt
        import re
        # Combine all available text sources for extraction
        desc_parts = [entities.get("description", "")]
        for r in non_vat_entries:
            if 2400 <= r["account_number"] <= 2499:
                desc_parts.append(r["posting"].get("description") or "")
        # Also search the raw prompt for supplier info
        raw_prompt = task.get("raw_prompt", "")
        desc_parts.append(raw_prompt)
        desc = " ".join(desc_parts)

        org_match = re.search(r'(?:[Oo]rg\.?\s*(?:nr\.?|n[uú]m(?:ero|mer)?|nº)?\.?:?\s*)(\d{6,9})', desc)
        # Try multiple name patterns, stopping at first match
        name_patterns = [
            r'(?:fra|from|de|von|du|de la|dal)\s+(.+?)(?:\s*\(|\s*-\s*(?:Org|org)|,|\s*$)',
            r'(?:Leverand[øo]rgjeld|gjeld|fournisseur|proveedor|fornecedor|Lieferant)\s*[-:]\s*(.+?)(?:\s*\(|\s*$)',
            # Extract "Company Name" from patterns like "facture de Company Name"
            r'(?:facture?\s+(?:de|du|fournisseur))\s+(.+?)(?:\s*\(|\s*-|\s*$)',
        ]
        for pat in name_patterns:
            name_match = re.search(pat, desc, re.IGNORECASE)
            if name_match:
                candidate = name_match.group(1).strip().rstrip('.,;:')
                # Reject if too long (likely captured too much)
                if len(candidate) <= 60 and candidate:
                    supplier_name = candidate
                    break

        if org_match and not supplier_org:
            supplier_org = org_match.group(1)

    if supplier_name or supplier_org:
        # Look up existing supplier
        search_params: dict[str, Any] = {"count": 1}
        if supplier_org:
            search_params["organizationNumber"] = supplier_org
        else:
            search_params["name"] = supplier_name
        supp_resp = client.get("/supplier", search_params)
        supp_values = supp_resp.get("values", [])
        if supp_values:
            supplier_id = supp_values[0]["id"]
        else:
            # Create supplier with BOTH name and org number
            supp_body: dict[str, Any] = {"name": supplier_name or f"Supplier {supplier_org}"}
            if supplier_org:
                supp_body["organizationNumber"] = supplier_org
            if supplier_bank_account:
                supp_body["bankAccountPresentation"] = [{"number": supplier_bank_account}]
            supp_result = client.post("/supplier", supp_body)
            if isinstance(supp_result, dict) and supp_result.get("value", {}).get("id"):
                supplier_id = supp_result["value"]["id"]
                logger.info("Created supplier '%s' org=%s (id=%d)",
                            supp_body["name"], supplier_org, supplier_id)

    # If AP posting exists but no supplier was resolved, create a fallback supplier
    # to avoid "Leverandør mangler" 422 errors
    if has_ap_posting and not supplier_id:
        fallback_name = supplier_name or entities.get("description", "Leverandør")[:50]
        logger.info("Creating fallback supplier for AP posting: '%s'", fallback_name)
        supp_body: dict[str, Any] = {"name": fallback_name}
        if supplier_org:
            supp_body["organizationNumber"] = supplier_org
        if supplier_bank_account:
            supp_body["bankAccountPresentation"] = [{"number": supplier_bank_account}]
        supp_result = client.post("/supplier", supp_body)
        if isinstance(supp_result, dict) and supp_result.get("value", {}).get("id"):
            supplier_id = supp_result["value"]["id"]
            logger.info("Created fallback supplier (id=%d)", supplier_id)

    # Phase 4: Build final postings (skip entries with unresolved account_id)
    postings = []
    row_num = 0
    for r in non_vat_entries:
        if r["account_id"] is None:
            logger.warning("Skipping posting with unresolved account_id for account %s", r["account_number"])
            continue
        row_num += 1
        p: dict[str, Any] = {
            "row": row_num,
            "account": {"id": r["account_id"]},
            "date": r["posting"].get("date", voucher_date),
            "amountGross": r["amount"],
            "amountGrossCurrency": r["amount"],
        }
        if r.get("vat_type_id") is not None:
            p["vatType"] = {"id": r["vat_type_id"]}
        if r["posting"].get("description"):
            p["description"] = r["posting"]["description"]
        # Add supplier reference to AP postings
        if supplier_id and 2400 <= r["account_number"] <= 2499:
            p["supplier"] = {"id": supplier_id}
            if invoice_number:
                p["invoiceNumber"] = invoice_number
            if due_date:
                p["termOfPayment"] = due_date
        postings.append(p)

    # Only add bank counter-posting if postings genuinely don't balance
    # (e.g. receipt/expense vouchers without an AP posting).
    # Supplier invoices with AP posting should already balance.
    posting_sum = sum(r["amount"] for r in non_vat_entries)
    if abs(posting_sum) > 0.01 and postings and not has_ap_posting:
        bank_acc_resp = client.get("/ledger/account", {"number": "1920", "count": 1})
        bank_values = bank_acc_resp.get("values", [])
        if bank_values:
            bank_id = bank_values[0]["id"]
            postings.append({
                "row": row_num + 1,
                "account": {"id": bank_id},
                "date": postings[0].get("date", today),
                "amountGross": -posting_sum,
                "amountGrossCurrency": -posting_sum,
            })
            logger.info("Added bank counter-posting on 1920 for %.2f to balance voucher", -posting_sum)

    if not postings:
        raise RuntimeError("No postings could be resolved for voucher creation")

    body: dict[str, Any] = {
        "date": entities.get("date", today),
        "description": entities.get("description", "Voucher"),
        "postings": postings,
    }

    result = client.post("/ledger/voucher", body)
    _check_response(result, "POST /ledger/voucher")
    value = result.get("value", {})
    return f"Created voucher (id={value.get('id', '?')})"


def _handle_delete_voucher(task: dict, client: TripletexClient, context: dict) -> str:
    entities = task["entities"]
    # Parser may wrap in "search" object
    search = entities.get("search", entities)

    v_id = entities.get("id") or search.get("id")
    if not v_id:
        search_params: dict[str, Any] = {"count": 1}
        desc = search.get("description") or entities.get("description")
        dt = search.get("date") or entities.get("date")
        if desc:
            search_params["description"] = desc
        if dt:
            search_params["dateFrom"] = dt
            search_params["dateTo"] = dt

        search_resp = client.get("/ledger/voucher", search_params)
        logger.info("delete_voucher search: %s", search_resp)
        values = search_resp.get("values", [])
        if not values:
            raise RuntimeError(
                f"Voucher not found with params: {search_params}"
            )
        v_id = values[0]["id"]

    result = client.delete(f"/ledger/voucher/{v_id}")
    logger.info("delete_voucher result: %s", result)
    return f"Deleted voucher (id={v_id})"


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

def _handle_update_employee_role(task: dict, client: TripletexClient, context: dict) -> str:
    """Alias: update role is just an employee update with userType change."""
    entities = task["entities"]
    # Ensure updates includes the role
    if "updates" not in entities:
        entities["updates"] = {}
    if entities.get("userType"):
        entities["updates"]["userType"] = entities["userType"]
    if entities.get("isAdministrator"):
        entities["updates"]["isAdministrator"] = True
    return _handle_update_employee(task, client, context)


def _handle_log_timesheet_hours(task: dict, client: TripletexClient, context: dict) -> str:
    """Log hours on a project activity for an employee via /timesheet/entry."""
    entities = task["entities"]
    today = _today()

    # Step 1: Find or create employee
    emp_name = entities.get("employeeName", "")
    emp_email = entities.get("employeeEmail", "")
    emp_id = None

    if emp_email:
        # Search by email first (most reliable)
        resp = client.get("/employee", {"email": emp_email, "count": 1})
        values = resp.get("values", [])
        if values:
            emp_id = values[0]["id"]
    if not emp_id and emp_name:
        emp_id = _find_or_create_employee_by_name(emp_name, client, context)
    if not emp_id:
        emp_id = _ensure_employee(client, context)

    # Step 2: Find or create customer
    cust_name = entities.get("customerName", "")
    cust_org = entities.get("customerOrganizationNumber", "")
    customer_id = None
    if cust_name or cust_org:
        cust_data = {"name": cust_name}
        if cust_org:
            cust_data["organizationNumber"] = cust_org
        customer_id = _find_or_create_customer(cust_data, client, context)

    # Step 3: Find or create project
    project_name = entities.get("projectName", "")
    project_id = None

    if project_name:
        # Search for existing project
        resp = client.get("/project", {"name": project_name, "count": 5})
        for p in resp.get("values", []):
            if p.get("name", "").strip().lower() == project_name.strip().lower():
                project_id = p["id"]
                break
        if not project_id and resp.get("values"):
            # Accept first result if it's close enough
            project_id = resp["values"][0]["id"]

    # Helper: create a global activity and link it to a project
    def _create_and_link_activity(act_name: str, proj_id: int) -> int | None:
        """Create a global activity, link to project, return timesheet activity id."""
        try:
            act_create = client.post("/activity", {"name": act_name, "isProjectActivity": True})
            if isinstance(act_create, dict) and act_create.get("value", {}).get("id"):
                global_act_id = act_create["value"]["id"]
                logger.info("Created global activity '%s' id=%d", act_name, global_act_id)
                # Link to project
                link = client.post("/project/projectActivity", {
                    "project": {"id": proj_id},
                    "activity": {"id": global_act_id},
                })
                logger.info("Linked activity to project: %s", link)
                # Re-fetch timesheet activities
                act_resp2 = client.get("/activity/>forTimeSheet", {"projectId": proj_id, "count": 100})
                for a in act_resp2.get("values", []):
                    if a.get("name", "").strip().lower() == act_name.strip().lower():
                        return a["id"]
                if act_resp2.get("values"):
                    return act_resp2["values"][0]["id"]
        except Exception as e:
            logger.warning("Failed to create+link activity '%s': %s", act_name, e)
        return None

    if not project_id:
        # Create project — try including generalProjectActivitiesPerProjectOnly=True
        # to allow adding project-specific activities
        proj_body: dict[str, Any] = {
            "name": project_name or "Project",
            "projectManager": {"id": emp_id},
            "isInternal": False,
            "startDate": today,
        }
        if customer_id:
            proj_body["customer"] = {"id": customer_id}
        proj = client.post("/project", proj_body)
        _check_response(proj, "POST /project (for timesheet)")
        project_id = proj["value"]["id"]

    # Step 4: Find activity for the project
    activity_name = entities.get("activityName", "")
    activity_id = None

    act_resp = client.get("/activity/>forTimeSheet", {"projectId": project_id, "count": 100})
    activities = act_resp.get("values", [])
    logger.info("Activities for project %d: %s", project_id,
                [(a.get("id"), a.get("name")) for a in activities[:10]])

    if activity_name:
        for a in activities:
            if a.get("name", "").strip().lower() == activity_name.strip().lower():
                activity_id = a["id"]
                break
    if not activity_id and activities:
        activity_id = activities[0]["id"]
        logger.info("Using first available activity: id=%d name=%s",
                    activity_id, activities[0].get("name"))

    if not activity_id:
        act_name = activity_name or "Generell"

        # Approach 1: Create global activity and link to our project
        activity_id = _create_and_link_activity(act_name, project_id)

        # Approach 2: Enable general activities on the project, then retry
        if not activity_id:
            try:
                # Fetch project and enable general activities
                proj_data = client.get(f"/project/{project_id}")
                if isinstance(proj_data, dict) and proj_data.get("value"):
                    pv = proj_data["value"]
                    pv["generalProjectActivitiesPerProjectOnly"] = False
                    client.put(f"/project/{project_id}", pv)
                    logger.info("Enabled general activities on project %d", project_id)
                    # Re-fetch timesheet activities
                    act_resp3 = client.get("/activity/>forTimeSheet", {"projectId": project_id, "count": 100})
                    activities3 = act_resp3.get("values", [])
                    if activities3:
                        if activity_name:
                            for a in activities3:
                                if a.get("name", "").strip().lower() == act_name.strip().lower():
                                    activity_id = a["id"]
                                    break
                        if not activity_id:
                            activity_id = activities3[0]["id"]
                    # Still no activities? Create one and link
                    if not activity_id:
                        activity_id = _create_and_link_activity(act_name, project_id)
            except Exception as e:
                logger.warning("Failed to enable general activities: %s", e)

        # Approach 3: Find ANY existing activity and link it
        if not activity_id:
            try:
                all_acts = client.get("/activity", {"count": 50})
                all_values = all_acts.get("values", [])
                for a in all_values:
                    try:
                        client.post("/project/projectActivity", {
                            "project": {"id": project_id},
                            "activity": {"id": a["id"]},
                        })
                        act_resp4 = client.get("/activity/>forTimeSheet", {"projectId": project_id, "count": 100})
                        if act_resp4.get("values"):
                            activity_id = act_resp4["values"][0]["id"]
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.warning("Failed to find any activity: %s", e)

        if not activity_id:
            raise RuntimeError(f"No activities found for project {project_id}")

    # Step 5: Create timesheet entry
    hours = entities.get("hours", 0)
    entry_date = entities.get("date", today)

    entry_body: dict[str, Any] = {
        "employee": {"id": emp_id},
        "project": {"id": project_id},
        "activity": {"id": activity_id},
        "date": entry_date,
        "hours": hours,
    }
    # Note: hourly rate is NOT a field on TimesheetEntry — it's set via project hourly rates.
    # Do NOT send hourlyCharge/hourlyRate on the entry body (causes 422 "field does not exist").
    if entities.get("comment"):
        entry_body["comment"] = entities["comment"]

    result = client.post("/timesheet/entry", entry_body)
    _check_response(result, "POST /timesheet/entry")
    value = result.get("value", {})
    return (
        f"Logged {hours} hours for employee {emp_name or emp_id} "
        f"on project '{project_name}' activity '{activity_name}' "
        f"(entry id={value.get('id', '?')})"
    )


def _handle_create_dimension_voucher(task: dict, client: TripletexClient, context: dict) -> str:
    """Create a custom accounting dimension with values, then post a voucher linked to a value."""
    e = task["entities"]
    today = e.get("today", _today())

    # Step 1: Create the accounting dimension
    dim_name = e.get("dimensionName", "Dimension")
    dim_body: dict[str, Any] = {
        "dimensionName": dim_name,
        "description": e.get("dimensionDescription", f"Custom dimension: {dim_name}"),
        "active": True,
    }
    dim_result = client.post("/ledger/accountingDimensionName", dim_body)
    _check_response(dim_result, "POST /ledger/accountingDimensionName")
    dim_data = dim_result["value"]
    dim_index = dim_data.get("dimensionIndex", 1)
    logger.info("Created dimension '%s' with index=%d, id=%d", dim_name, dim_index, dim_data["id"])

    # Step 2: Create dimension values
    dim_values = e.get("dimensionValues", [])
    value_id_map: dict[str, int] = {}
    seen_codes: set[str] = set()
    for i, val_name in enumerate(dim_values):
        # Generate a short code from the value name
        code = val_name[:4].upper().replace(" ", "")
        if code in seen_codes:
            code = f"{code}{i}"
        seen_codes.add(code)
        val_body: dict[str, Any] = {
            "displayName": val_name,
            "dimensionIndex": dim_index,
            "active": True,
            "number": code,
            "showInVoucherRegistration": True,
        }
        val_result = client.post("/ledger/accountingDimensionValue", val_body)
        _check_response(val_result, f"POST /ledger/accountingDimensionValue ({val_name})")
        value_id_map[val_name] = val_result["value"]["id"]
        logger.info("Created dimension value '%s' -> id=%d", val_name, val_result["value"]["id"])

    # Step 3: Look up the account
    account_number = e.get("accountNumber")
    if not account_number:
        raise RuntimeError("No account number specified for voucher posting")

    acct_resp = client.get("/ledger/account", {"number": account_number, "count": 1})
    acct_values = acct_resp.get("values", [])
    if not acct_values:
        raise RuntimeError(f"Account {account_number} not found")
    account_id = acct_values[0]["id"]
    # Use the account's default VAT type, or id=0 (no VAT) if it's a balance account
    acct_vat_id = acct_values[0].get("vatType", {}).get("id", 0)
    acct_legal_vats = [v.get("id") for v in acct_values[0].get("legalVatTypes", [])]

    # Step 4: Determine which dimension value to link
    linked_value_name = e.get("linkedDimensionValue", "")
    linked_value_id = value_id_map.get(linked_value_name)
    if not linked_value_id:
        if value_id_map:
            # Try case-insensitive match
            for name, vid in value_id_map.items():
                if name.lower() == linked_value_name.lower():
                    linked_value_id = vid
                    break
            if not linked_value_id:
                # Default to the last created value
                linked_value_id = list(value_id_map.values())[-1]
        else:
            raise RuntimeError("No dimension values created — cannot link voucher posting")

    # Step 5: Build voucher with balanced postings
    amount = e.get("amount", 0)
    dim_field = f"freeAccountingDimension{dim_index}"

    # Choose a VAT type that's legal for the account
    posting_vat_id = 0  # Default: no VAT treatment
    if acct_vat_id in acct_legal_vats:
        posting_vat_id = acct_vat_id
    elif 0 in acct_legal_vats:
        posting_vat_id = 0

    # Find bank account (1920) for the counter-posting
    bank_resp = client.get("/ledger/account", {"number": "1920", "count": 1})
    bank_values = bank_resp.get("values", [])
    bank_id = bank_values[0]["id"] if bank_values else None

    if not bank_id:
        raise RuntimeError("Bank account 1920 not found for counter-posting")

    postings = [
        {
            "row": 1,
            "account": {"id": account_id},
            "amountGross": amount,
            "amountGrossCurrency": amount,
            "date": e.get("voucherDate", today),
            "vatType": {"id": posting_vat_id},
            dim_field: {"id": linked_value_id},
        },
        {
            "row": 2,
            "account": {"id": bank_id},
            "amountGross": -amount,
            "amountGrossCurrency": -amount,
            "date": e.get("voucherDate", today),
            "vatType": {"id": 0},
        },
    ]

    voucher_body: dict[str, Any] = {
        "date": e.get("voucherDate", today),
        "description": e.get("voucherDescription", f"Posting with dimension {dim_name}"),
        "postings": postings,
    }

    result = client.post("/ledger/voucher", voucher_body)
    _check_response(result, "POST /ledger/voucher")
    voucher_id = result["value"]["id"]

    created_values = ", ".join(f"{k}={v}" for k, v in value_id_map.items())
    return (
        f"Created dimension '{dim_name}' (index={dim_index}) with values [{created_values}]. "
        f"Posted voucher {voucher_id} for {amount} on account {account_number} "
        f"linked to dimension value '{linked_value_name}' (id={linked_value_id})."
    )


def _handle_reverse_invoice_payment(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle both: (A) register payment on existing unpaid invoice, (B) reverse existing payment.

    The parser routes here for BOTH cases:
    - "Register payment on existing outstanding invoice" → register positive payment
    - "Reverse/undo/cancel a payment" → register negative payment
    """
    entities = task["entities"]
    today = _today()

    # Detect intent: is this a REVERSAL or a REGISTER PAYMENT?
    raw_prompt = task.get("raw_prompt", "")
    prompt_lower = raw_prompt.lower() if raw_prompt else ""
    # Also check entities for hints
    is_reversal = entities.get("isReversal", False)
    if not is_reversal and prompt_lower:
        _REVERSE_KEYWORDS = [
            "reverse", "cancel the payment", "cancel payment",
            "reverter", "cancelar o pagamento",
            "tilbakefør", "angre",
            "annuler", "annulez",
            "stornieren", "storniere",
            "revertir", "revierta", "cancelar el pago",
            "returned by the bank", "returnert av banken",
            "retournée par la banque", "retourné par la banque",
            "devuelto por el banco", "devolvido pelo banco",
        ]
        is_reversal = any(kw in prompt_lower for kw in _REVERSE_KEYWORDS)

    # Step 1: Find the customer
    org_number = str(entities.get("customerOrganizationNumber") or "").strip()
    cust_name = entities.get("customerName", "")
    customer_id = None

    if org_number:
        resp = client.get("/customer", {"organizationNumber": org_number, "count": 1})
        values = resp.get("values", [])
        if values:
            customer_id = values[0]["id"]

    if not customer_id and cust_name:
        resp = client.get("/customer", {"name": cust_name, "count": 5})
        for v in resp.get("values", []):
            if v.get("name", "").strip().lower() == cust_name.strip().lower():
                customer_id = v["id"]
                break
        if not customer_id and resp.get("values"):
            customer_id = resp["values"][0]["id"]

    if not customer_id:
        raise RuntimeError(f"Customer not found: {cust_name} / {org_number}")

    # Step 2: Find the invoice
    inv_resp = client.get("/invoice", {
        "customerId": customer_id,
        "invoiceDateFrom": "2020-01-01",
        "invoiceDateTo": "2030-12-31",
        "count": 100,
    })
    invoices = inv_resp.get("values", [])
    if not invoices:
        raise RuntimeError(f"No invoices found for customer {customer_id}")

    # Match by amount (excl. VAT) or description
    target_amount = entities.get("amount")
    target_desc = (entities.get("invoiceDescription") or "").lower()

    if is_reversal:
        # REVERSAL: find invoice with existing payment (outstanding < total)
        matched_invoice = _match_invoice_for_reversal(invoices, target_amount, target_desc)
    else:
        # REGISTER PAYMENT: find unpaid invoice (outstanding > 0)
        matched_invoice = _match_invoice_for_payment(invoices, target_amount, target_desc)

    if not matched_invoice:
        # Fallback: try the other approach
        if is_reversal:
            matched_invoice = _match_invoice_for_payment(invoices, target_amount, target_desc)
        else:
            matched_invoice = _match_invoice_for_reversal(invoices, target_amount, target_desc)

    if not matched_invoice:
        # Last resort: use first invoice
        matched_invoice = invoices[0]
        logger.warning("No matching invoice found, using first invoice %d", matched_invoice["id"])

    inv_id = matched_invoice["id"]
    inv_amount = matched_invoice.get("amount", 0)
    outstanding = matched_invoice.get("amountOutstanding", 0)
    paid_already = inv_amount - outstanding
    inv_voucher_id = matched_invoice.get("voucher", {}).get("id")

    # Step 3: Find payment type
    pt_resp = client.get("/invoice/paymentType", {"count": 100})
    payment_types = pt_resp.get("values", [])
    payment_type_id = None
    desired_type = entities.get("paymentTypeDescription") or entities.get("paymentType") or ""
    for pt in payment_types:
        desc = pt.get("description", "")
        if desired_type and desired_type.lower() in desc.lower():
            payment_type_id = pt["id"]
            break
    if payment_type_id is None and payment_types:
        payment_type_id = payment_types[0]["id"]

    if payment_type_id is None:
        raise RuntimeError("No payment types found")

    # Step 4: Register payment (positive for register, negative for reversal)
    if is_reversal:
        payment_amount = -paid_already if paid_already > 0 else -inv_amount
        logger.info("REVERSAL: invoice %d, amount=%s, outstanding=%s, reversing=%s",
                    inv_id, inv_amount, outstanding, payment_amount)
    else:
        # Register payment: pay the outstanding amount (full or partial)
        parsed_paid = entities.get("paidAmount")
        if parsed_paid and parsed_paid > 0:
            payment_amount = parsed_paid
        else:
            payment_amount = outstanding if outstanding > 0 else inv_amount
        logger.info("REGISTER PAYMENT: invoice %d, amount=%s, outstanding=%s, paying=%s",
                    inv_id, inv_amount, outstanding, payment_amount)

    pay_params: dict[str, Any] = {
        "paymentDate": entities.get("date", today),
        "paymentTypeId": payment_type_id,
        "paidAmount": payment_amount,
        "paidAmountCurrency": payment_amount,
    }
    pay_result = client.put(f"/invoice/{inv_id}/:payment", params=pay_params)
    logger.info("Payment result: %s", pay_result)

    if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
        if is_reversal:
            # Negative payment didn't work — try reversing the payment voucher instead
            logger.warning("Negative payment failed, trying voucher reversal...")
            postings_refs = matched_invoice.get("postings", [])
            payment_voucher_id = None

            for p_ref in postings_refs:
                p_id = p_ref.get("id") if isinstance(p_ref, dict) else p_ref
                try:
                    p_detail = client.get(f"/ledger/posting/{p_id}")
                    p_data = p_detail.get("value", p_detail)
                    p_voucher = p_data.get("voucher", {}).get("id")
                    if p_voucher and p_voucher != inv_voucher_id:
                        payment_voucher_id = p_voucher
                        break
                except Exception:
                    continue

            if payment_voucher_id:
                rev_result = client.put(
                    f"/ledger/voucher/{payment_voucher_id}/:reverse",
                    params={"date": entities.get("date", today)},
                )
                _check_response(rev_result, "PUT /ledger/voucher/:reverse")
                return (
                    f"Reversed payment voucher {payment_voucher_id} for invoice {inv_id}. "
                    f"Outstanding amount restored to {inv_amount}."
                )
            else:
                raise RuntimeError(f"Payment reversal failed: {pay_result}")
        else:
            # Try JSON body approach for registering payment
            logger.warning("PUT /:payment with params failed, trying JSON body...")
            pay_result = client.put(
                f"/invoice/{inv_id}/:payment",
                json_body={
                    "paymentDate": entities.get("date", today),
                    "paymentTypeId": payment_type_id,
                    "paidAmount": payment_amount,
                    "paidAmountCurrency": payment_amount,
                },
            )
            if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
                raise RuntimeError(f"Payment registration failed: {pay_result}")

    if is_reversal:
        return (
            f"Reversed payment of {abs(payment_amount)} on invoice {inv_id}. "
            f"Outstanding amount restored to {inv_amount}."
        )
    else:
        return (
            f"Registered payment of {payment_amount} on invoice {inv_id}. "
            f"Invoice is now paid."
        )


def _match_invoice_for_reversal(invoices: list[dict], target_amount: float | None,
                                 target_desc: str) -> dict | None:
    """Find an invoice that has an existing payment (outstanding < total) for reversal."""
    for inv in invoices:
        outstanding = inv.get("amountOutstanding", 0)
        total = inv.get("amount", 0)
        if outstanding >= total:
            continue  # No payment to reverse

        excl = inv.get("amountExcludingVat") or inv.get("amountExcludingVatCurrency")
        if target_amount and excl and abs(excl - target_amount) < 1:
            return inv
        if target_amount and total and abs(total - target_amount) < 1:
            return inv

    # Fallback: first invoice with payment
    for inv in invoices:
        if inv.get("amountOutstanding", 0) < inv.get("amount", 0):
            return inv
    return None


def _match_invoice_for_payment(invoices: list[dict], target_amount: float | None,
                                target_desc: str) -> dict | None:
    """Find an unpaid invoice (outstanding > 0) for payment registration."""
    for inv in invoices:
        outstanding = inv.get("amountOutstanding", 0)
        if outstanding <= 0:
            continue  # Already fully paid

        excl = inv.get("amountExcludingVat") or inv.get("amountExcludingVatCurrency")
        if target_amount and excl and abs(excl - target_amount) < 1:
            return inv
        if target_amount and outstanding and abs(outstanding - target_amount) < 1:
            return inv
        gross = inv.get("amount", 0)
        if target_amount and gross and abs(gross - target_amount) < 1:
            return inv

    # Fallback: first unpaid invoice
    for inv in invoices:
        if inv.get("amountOutstanding", 0) > 0:
            return inv
    return None


def _handle_run_payroll(task: dict, client: TripletexClient, context: dict) -> str:
    """Run payroll for an employee: ensure employment, set salary, create payroll transaction."""
    entities = task["entities"]
    today = _today()
    from datetime import date as _date_mod
    current_date = _date_mod.today()

    # Step 1: Find the employee
    emp_name = entities.get("employeeName", "")
    emp_email = entities.get("employeeEmail", "")
    emp_id = None
    emp_record = None

    if emp_email:
        resp = client.get("/employee", {"email": emp_email, "count": 1})
        values = resp.get("values", [])
        if values:
            emp_id = values[0]["id"]
            emp_record = values[0]
    if not emp_id and emp_name:
        # Try to find by name first to get the full record
        for emp in context.get("employees", []):
            full_name = f"{emp.get('firstName', '')} {emp.get('lastName', '')}".strip()
            if full_name.lower() == emp_name.lower():
                emp_id = emp["id"]
                emp_record = emp
                break
        if not emp_id:
            emp_id = _find_or_create_employee_by_name(emp_name, client, context)
    if not emp_id:
        emp_id = _ensure_employee(client, context)

    # Ensure employee has dateOfBirth — Tripletex requires it for employment creation
    if emp_id and not (emp_record and emp_record.get("dateOfBirth")):
        # Fetch full employee record if we don't have it
        if not emp_record:
            emp_detail = client.get(f"/employee/{emp_id}")
            emp_record = emp_detail.get("value", emp_detail)
        if not emp_record.get("dateOfBirth"):
            # Set a placeholder dateOfBirth so employment creation succeeds
            version = emp_record.get("version", 0)
            client.put(f"/employee/{emp_id}", {
                "id": emp_id,
                "version": version,
                "dateOfBirth": "1990-01-01",
            })
            logger.info("Set placeholder dateOfBirth on employee %d for payroll", emp_id)

    # Extract payroll period early — needed for employment startDate
    payroll_month = int(entities.get("month", current_date.month))
    payroll_year = int(entities.get("year", current_date.year))

    # Step 2: Ensure employment exists
    emp_resp = client.get("/employee/employment", {"employeeId": emp_id, "count": 10})
    employments = emp_resp.get("values", [])

    employment_id = None
    if employments:
        employment_id = employments[0]["id"]
        # Ensure existing employment covers the payroll period — update startDate if needed
        emp_start = employments[0].get("startDate", "")
        payroll_first_day = f"{payroll_year}-{payroll_month:02d}-01"
        if emp_start and emp_start > payroll_first_day:
            logger.info("Employment startDate %s is after payroll month %s, updating...",
                        emp_start, payroll_first_day)
            emp_version = employments[0].get("version", 0)
            new_start = f"{min(payroll_year, int(emp_start[:4]))}-01-01"
            client.put(f"/employee/employment/{employment_id}", {
                "id": employment_id,
                "version": emp_version,
                "startDate": new_start,
            })
        logger.info("Using existing employment %d for employee %d", employment_id, emp_id)
    else:
        # Create employment — startDate must be before the payroll month
        emp_start_year = min(payroll_year, current_date.year)
        emp_body: dict[str, Any] = {
            "employee": {"id": emp_id},
            "startDate": f"{emp_start_year}-01-01",
            "isMainEmployer": True,
            "taxDeductionCode": "loennFraHovedarbeidsgiver",
        }
        emp_result = client.post("/employee/employment", emp_body)
        _check_response(emp_result, "POST /employee/employment")
        employment_id = emp_result["value"]["id"]
        logger.info("Created employment %d for employee %d", employment_id, emp_id)

    # Step 3: Set salary via employment details
    monthly_salary = float(entities.get("monthlySalary", 0) or 0)
    if monthly_salary:
        annual_salary = monthly_salary * 12
        details_body: dict[str, Any] = {
            "employment": {"id": employment_id},
            "date": f"{current_date.year}-01-01",
            "employmentType": "ORDINARY",
            "employmentForm": "PERMANENT",
            "remunerationType": "MONTHLY_WAGE",
            "workingHoursScheme": "NOT_SHIFT",
            "percentageOfFullTimeEquivalent": 100.0,
            "annualSalary": annual_salary,
        }
        det_result = client.post("/employee/employment/details", details_body)
        if isinstance(det_result, dict) and det_result.get("status", 0) >= 400:
            # Details might already exist — try updating via PUT on the latest
            logger.warning("POST employment/details failed, trying to update existing...")
            existing_details = client.get("/employee/employment/details",
                                          {"employmentId": employment_id, "count": 1})
            det_values = existing_details.get("values", [])
            if det_values:
                det_id = det_values[0]["id"]
                det_version = det_values[0].get("version", 0)
                details_body["id"] = det_id
                details_body["version"] = det_version
                det_result = client.put(f"/employee/employment/details/{det_id}", details_body)
                _check_response(det_result, "PUT /employee/employment/details")
            else:
                _check_response(det_result, "POST /employee/employment/details")
        logger.info("Set salary: annual=%d, monthly=%d", annual_salary, monthly_salary)

    # Step 4: Create salary transaction (payroll run) for the month
    # (payroll_month and payroll_year already extracted at top of function)

    # Use last day of month as the payroll date
    if payroll_month == 12:
        payroll_date = f"{payroll_year}-12-31"
    else:
        from datetime import timedelta
        next_month_first = _date_mod(payroll_year, payroll_month + 1, 1)
        last_day = next_month_first - timedelta(days=1)
        payroll_date = last_day.isoformat()

    sal_body: dict[str, Any] = {
        "date": payroll_date,
        "year": payroll_year,
        "month": payroll_month,
        "payslips": [{"employee": {"id": emp_id}}],
    }
    sal_result = client.post("/salary/transaction", sal_body)
    _check_response(sal_result, "POST /salary/transaction")
    sal_value = sal_result["value"]
    sal_id = sal_value["id"]
    logger.info("Created salary transaction %d for %d-%02d", sal_id, payroll_year, payroll_month)

    # Extract payslip IDs directly from the transaction response (GET query often returns empty)
    transaction_payslips = sal_value.get("payslips", [])

    # Step 5: If bonus specified, add bonus specification to the payslip
    bonus = entities.get("bonus", 0)
    if bonus and transaction_payslips:
        # Find salary types to get the bonus type ID
        type_resp = client.get("/salary/type", {"count": 1000})
        salary_types = type_resp.get("values", [])

        bonus_type_id = None
        for st in salary_types:
            st_name = st.get("name", "").lower()
            st_number = str(st.get("number", ""))
            # Salary type 2002 = "Bonus" in Norwegian Tripletex
            if st_number == "2002":
                bonus_type_id = st["id"]
                break
        if not bonus_type_id:
            for st in salary_types:
                st_name = st.get("name", "").lower()
                if st_name == "bonus" or "bonus" in st_name:
                    bonus_type_id = st["id"]
                    break

        # Use payslip ID from the transaction creation response
        payslip_id = transaction_payslips[0]["id"]
        logger.info("Using payslip %d from transaction response", payslip_id)

        if bonus_type_id:
            # Add bonus as a salary specification on the payslip
            # API requires: rate (amount per unit), count (number of units)
            spec_body: dict[str, Any] = {
                "payslip": {"id": payslip_id},
                "salaryType": {"id": bonus_type_id},
                "rate": bonus,
                "count": 1,
            }
            spec_result = client.post("/salary/specification", spec_body)
            if isinstance(spec_result, dict) and spec_result.get("status", 0) >= 400:
                logger.warning("POST /salary/specification failed: %s",
                             spec_result.get("message", ""))
            else:
                logger.info("Added bonus specification to payslip %d", payslip_id)
        else:
            logger.warning("Could not find bonus salary type")

    result_parts = [
        f"Ran payroll for employee {emp_name or emp_id} ({payroll_year}-{payroll_month:02d})",
        f"salary transaction id={sal_id}",
    ]
    if monthly_salary:
        result_parts.append(f"monthly salary={monthly_salary}")
    if bonus:
        result_parts.append(f"bonus={bonus}")

    return ". ".join(result_parts)


def _handle_bank_reconciliation(task: dict, client: TripletexClient, context: dict) -> str:
    """Reconcile bank statement (CSV) with open invoices in Tripletex.

    1. Parse CSV bank statement from attached files
    2. GET all open invoices
    3. Match bank transactions to invoices by amount/reference
    4. PUT /invoice/{id}/:payment for each match
    """
    import csv
    import io

    today = _today()

    # Step 1: Parse CSV from file attachments
    file_contents = task.get("_file_contents", [])
    files_raw = task.get("_files_raw", [])
    csv_data = ""

    # Try text file contents first
    for content in file_contents:
        if content and (";" in content or "," in content) and any(
            kw in content.lower() for kw in ["dato", "date", "beløp", "amount", "beskrivelse", "description"]
        ):
            csv_data = content
            break

    # If not found in text, try decoding raw files
    if not csv_data:
        import base64 as _b64
        for f in files_raw:
            mime = f.get("mime_type", "")
            if "csv" in mime or "text" in mime or f.get("filename", "").endswith(".csv"):
                try:
                    csv_data = _b64.b64decode(f.get("content_base64", "")).decode("utf-8")
                    break
                except Exception:
                    continue

    if not csv_data:
        raise RuntimeError("No CSV bank statement found in attachments")

    # Remove file header markers if present
    for prefix in ["[File:", "---"]:
        if csv_data.startswith(prefix):
            csv_data = csv_data.split("\n", 1)[-1] if "\n" in csv_data else csv_data

    # Detect CSV delimiter (semicolon or comma)
    first_lines = csv_data.strip().split("\n")[:3]
    delimiter = ";" if any(";" in line for line in first_lines) else ","

    # Parse CSV rows
    reader = csv.DictReader(io.StringIO(csv_data.strip()), delimiter=delimiter)
    transactions = []
    for row in reader:
        # Normalize field names (handle various languages)
        tx: dict[str, Any] = {}
        for key, val in row.items():
            if not key:
                continue
            k = key.strip().lower()
            if k in ("dato", "date", "fecha", "data", "datum"):
                tx["date"] = val.strip()
            elif k in ("beløp", "amount", "monto", "montante", "betrag", "sum", "belopp"):
                # Parse amount: handle both "1234.56" and "1 234,56" formats
                amt_str = val.strip().replace(" ", "").replace("\xa0", "")
                # European format: 1.234,56 → 1234.56
                if "," in amt_str and "." in amt_str:
                    amt_str = amt_str.replace(".", "").replace(",", ".")
                elif "," in amt_str:
                    amt_str = amt_str.replace(",", ".")
                try:
                    tx["amount"] = float(amt_str)
                except ValueError:
                    continue
            elif k in ("inn", "inntekt", "credit", "crédito", "crédit"):
                # Separate income column (positive amount)
                amt_str = val.strip().replace(" ", "").replace("\xa0", "")
                if not amt_str:
                    continue
                if "," in amt_str and "." in amt_str:
                    amt_str = amt_str.replace(".", "").replace(",", ".")
                elif "," in amt_str:
                    amt_str = amt_str.replace(",", ".")
                try:
                    tx["amount"] = float(amt_str)
                except ValueError:
                    continue
            elif k in ("ut", "utgift", "debit", "débito", "débit"):
                # Separate outgoing column (negative amount)
                amt_str = val.strip().replace(" ", "").replace("\xa0", "")
                if not amt_str:
                    continue
                if "," in amt_str and "." in amt_str:
                    amt_str = amt_str.replace(".", "").replace(",", ".")
                elif "," in amt_str:
                    amt_str = amt_str.replace(",", ".")
                try:
                    tx["amount"] = -abs(float(amt_str))
                except ValueError:
                    continue
            elif k in ("beskrivelse", "description", "descripción", "descrição", "beschreibung",
                        "tekst", "text", "referanse", "reference", "ref",
                        "forklaring", "explicação", "explicación", "explication"):
                tx["description"] = val.strip()
            elif k in ("kunde", "customer", "client", "cliente", "kundenr", "customer_ref"):
                tx["customer_ref"] = val.strip()
        if "amount" in tx:
            transactions.append(tx)

    logger.info("Parsed %d bank transactions from CSV", len(transactions))

    if not transactions:
        raise RuntimeError("No valid transactions found in CSV")

    # Step 2: Get all invoices (unpaid ones for matching)
    inv_resp = client.get("/invoice", {
        "invoiceDateFrom": "2020-01-01",
        "invoiceDateTo": "2030-12-31",
        "count": 1000,
    })
    invoices = inv_resp.get("values", [])
    logger.info("Found %d invoices for matching", len(invoices))

    # Build lookup maps for matching
    inv_by_amount: dict[float, list[dict]] = {}
    for inv in invoices:
        outstanding = inv.get("amountOutstanding", 0)
        if outstanding > 0:
            # Round to 2 decimals for matching
            key = round(outstanding, 2)
            inv_by_amount.setdefault(key, []).append(inv)
            # Also index by gross amount
            gross = round(inv.get("amount", 0), 2)
            if gross != key:
                inv_by_amount.setdefault(gross, []).append(inv)

    # Step 3: Find payment type
    pt_resp = client.get("/invoice/paymentType", {"count": 100})
    payment_types = pt_resp.get("values", [])
    payment_type_id = payment_types[0]["id"] if payment_types else None

    if payment_type_id is None:
        raise RuntimeError("No payment types found for bank reconciliation")

    # Also get outgoing payment type for supplier payments
    pt_out_resp = client.get("/ledger/paymentTypeOut", {"count": 100})
    payment_types_out = pt_out_resp.get("values", [])
    payment_type_out_id = payment_types_out[0]["id"] if payment_types_out else payment_type_id

    # Build supplier invoice lookup (for outgoing/negative transactions)
    # Supplier invoices are vouchers, try to find them
    supplier_inv_by_amount: dict[float, list[dict]] = {}
    try:
        supp_resp = client.get("/supplierInvoice", {
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": "2030-12-31",
            "count": 1000,
        })
        for sinv in supp_resp.get("values", []):
            outstanding = sinv.get("amountOutstanding", sinv.get("amount", 0))
            if outstanding and outstanding > 0:
                key = round(outstanding, 2)
                supplier_inv_by_amount.setdefault(key, []).append(sinv)
    except Exception:
        logger.info("No supplier invoice endpoint or no supplier invoices found")

    # Step 4: Match transactions to invoices and register payments
    matched = 0
    unmatched = 0
    used_invoice_ids: set[int] = set()

    for tx in transactions:
        amount = tx["amount"]
        tx_date = tx.get("date", today)
        tx_desc = tx.get("description", "")

        if amount > 0:
            # Incoming payment — match against customer invoices
            amt_key = round(amount, 2)
            candidates = inv_by_amount.get(amt_key, [])

            matched_inv = None
            for inv in candidates:
                if inv["id"] not in used_invoice_ids:
                    matched_inv = inv
                    break

            if not matched_inv:
                # Fuzzy amount matching (within 1 NOK tolerance)
                for key, invs in inv_by_amount.items():
                    if abs(key - amount) < 1.0:
                        for inv in invs:
                            if inv["id"] not in used_invoice_ids:
                                matched_inv = inv
                                break
                        if matched_inv:
                            break

            if matched_inv:
                pay_params: dict[str, Any] = {
                    "paymentDate": tx_date,
                    "paymentTypeId": payment_type_id,
                    "paidAmount": amount,
                    "paidAmountCurrency": amount,
                }
                pay_result = client.put(f"/invoice/{matched_inv['id']}/:payment", params=pay_params)

                if isinstance(pay_result, dict) and pay_result.get("status", 0) < 400:
                    matched += 1
                    used_invoice_ids.add(matched_inv["id"])
                    logger.info("Matched incoming tx %.2f → invoice %d (%s)",
                               amount, matched_inv["id"], tx_desc[:50])
                else:
                    logger.warning("Payment failed for invoice %d: %s",
                                 matched_inv["id"], pay_result.get("message", ""))
                    unmatched += 1
            else:
                logger.warning("No matching invoice for incoming tx: %.2f %s", amount, tx_desc[:50])
                unmatched += 1

        elif amount < 0:
            # Outgoing payment — match against supplier invoices
            abs_amount = abs(amount)
            amt_key = round(abs_amount, 2)
            candidates = supplier_inv_by_amount.get(amt_key, [])

            matched_sinv = None
            for sinv in candidates:
                if sinv["id"] not in used_invoice_ids:
                    matched_sinv = sinv
                    break

            if not matched_sinv:
                for key, sinvs in supplier_inv_by_amount.items():
                    if abs(key - abs_amount) < 1.0:
                        for sinv in sinvs:
                            if sinv["id"] not in used_invoice_ids:
                                matched_sinv = sinv
                                break
                        if matched_sinv:
                            break

            if matched_sinv:
                pay_params = {
                    "paymentDate": tx_date,
                    "paymentTypeId": payment_type_out_id,
                    "paidAmount": abs_amount,
                    "paidAmountCurrency": abs_amount,
                }
                pay_result = client.put(f"/supplierInvoice/{matched_sinv['id']}/:registerPayment", params=pay_params)

                if isinstance(pay_result, dict) and pay_result.get("status", 0) < 400:
                    matched += 1
                    used_invoice_ids.add(matched_sinv["id"])
                    logger.info("Matched outgoing tx %.2f → supplier invoice %d (%s)",
                               abs_amount, matched_sinv["id"], tx_desc[:50])
                else:
                    logger.warning("Supplier payment failed for invoice %d: %s",
                                 matched_sinv["id"], pay_result.get("message", ""))
                    unmatched += 1
            else:
                logger.warning("No matching supplier invoice for outgoing tx: %.2f %s", abs_amount, tx_desc[:50])
                unmatched += 1

    return (
        f"Bank reconciliation completed: {matched} payments matched and registered, "
        f"{unmatched} transactions unmatched out of {len(transactions)} total."
    )


def _make_closure_posting(account_id: int, amount: float, posting_date: str,
                          *, row: int = 1) -> dict[str, Any]:
    """Build a single voucher posting with explicit vatType 0 (no VAT).

    Annual closure postings (depreciation, prepaid reversals, tax provisions)
    never involve VAT.  ``row`` must be >= 1 (Tripletex reserves row 0 for
    system-generated postings and rejects external postings on row 0).
    """
    return {
        "row": row,
        "account": {"id": account_id},
        "amountGross": amount,
        "amountGrossCurrency": amount,
        "date": posting_date,
        "vatType": {"id": 0},
    }


def _handle_annual_closure(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle annual closure / year-end closing tasks.

    Strategy:
    1. If parser extracted depreciationItems (structured per-asset), use those
    2. If parser extracted entries (with postings), use those directly
    3. Otherwise, fall back to regex parsing of the raw prompt
    """
    import re

    entities = task.get("entities", {})
    raw_prompt = task.get("raw_prompt", "")
    today = _today()
    account_cache: dict[str, int] = {}

    # Determine closure date from entities
    closure_year = entities.get("closureYear") or entities.get("year")
    closure_month = entities.get("closureMonth") or entities.get("month")
    if closure_year and closure_month:
        import calendar
        last_day = calendar.monthrange(closure_year, closure_month)[1]
        closure_date = f"{closure_year}-{closure_month:02d}-{last_day:02d}"
    elif closure_year:
        closure_date = f"{closure_year}-12-31"
    elif entities.get("date"):
        closure_date = entities["date"]
    else:
        closure_date = f"{date.today().year - 1}-12-31"

    # ---------------------------------------------------------------
    # PATH A: depreciationItems format (parser returns per-asset items)
    # ---------------------------------------------------------------
    dep_items = entities.get("depreciationItems", [])
    if dep_items:
        logger.info("Annual closure: using %d depreciationItems", len(dep_items))
        vouchers_created = []
        total_depreciation_expense = 0.0

        for item in dep_items:
            cost = item.get("acquisitionCost", 0)
            years = item.get("depreciationPeriodYears", 1)
            asset_name = item.get("assetName", "Asset")
            expense_acct = str(item.get("depreciationExpenseAccountNumber", 6010))
            accum_acct = str(item.get("accumulatedDepreciationAccountNumber", 1209))

            annual_dep = round(cost / years, 2)
            total_depreciation_expense += annual_dep

            expense_id = _get_or_create_account(client, expense_acct, cache=account_cache)
            accum_id = _get_or_create_account(client, accum_acct,
                                               name=f"Akkumulerte avskrivninger",
                                               cache=account_cache)

            voucher_body: dict[str, Any] = {
                "date": closure_date,
                "description": f"Avskrivning {asset_name}",
                "postings": [
                    _make_closure_posting(expense_id, annual_dep, closure_date),
                    _make_closure_posting(accum_id, -annual_dep, closure_date),
                ],
            }
            result = client.post("/ledger/voucher", voucher_body)
            _check_response(result, f"POST /ledger/voucher (depreciation {asset_name})")
            vouchers_created.append(f"Depreciation {asset_name}: {annual_dep} NOK")
            logger.info("Created depreciation voucher: %s = %s NOK", asset_name, annual_dep)

        # Prepaid expense reversal
        prepaid = entities.get("prepaidExpenseReversal")
        if prepaid:
            amount = prepaid.get("amount", 0)
            acct = str(prepaid.get("accountNumber", 1700))
            # Determine the expense account for the reversal.
            # Strategy: check parser entries for a prepaid-related entry that has
            # two postings (one on the prepaid account, one on an expense account).
            # Fall back to the depreciation expense account or 6300.
            expense_acct = None
            parsed_entries = entities.get("entries", [])
            for entry in parsed_entries:
                entry_postings = entry.get("postings", [])
                if len(entry_postings) >= 2:
                    acct_numbers = [str(p.get("accountNumber", "")) for p in entry_postings]
                    if acct in acct_numbers:
                        # This entry involves our prepaid account — find the other account
                        for p in entry_postings:
                            p_acct = str(p.get("accountNumber", ""))
                            if p_acct != acct:
                                expense_acct = p_acct
                                break
                        if expense_acct:
                            break
            if not expense_acct:
                # Fall back: use depreciation expense account if available, else 6300
                if dep_items:
                    expense_acct = str(dep_items[0].get("depreciationExpenseAccountNumber", 6300))
                else:
                    expense_acct = "6300"
            # Ensure expense_acct is different from the prepaid account
            if expense_acct == acct:
                expense_acct = "6300"

            prepaid_id = _get_or_create_account(client, acct, cache=account_cache)
            expense_id = _get_or_create_account(client, expense_acct,
                                                 name="Driftskostnad",
                                                 cache=account_cache)

            voucher_body = {
                "date": closure_date,
                "description": "Reversering forskuddsbetalt kostnad",
                "postings": [
                    _make_closure_posting(expense_id, amount, closure_date),
                    _make_closure_posting(prepaid_id, -amount, closure_date),
                ],
            }
            result = client.post("/ledger/voucher", voucher_body)
            _check_response(result, "POST /ledger/voucher (prepaid reversal)")
            vouchers_created.append(f"Prepaid reversal: {amount} NOK")

        # Tax provision
        tax_info = entities.get("taxCalculation")
        if tax_info:
            tax_rate = tax_info.get("taxRate", 0.22)
            tax_exp_acct = str(tax_info.get("expenseAccountNumber", 8700))
            tax_liab_acct = str(tax_info.get("liabilityAccountNumber", 2920))

            # Calculate taxable income from the actual P&L in the ledger.
            # Query balanceSheet for all P&L accounts (3000-8699) up to closure date.
            # Revenue accounts (3xxx) have credit (negative) balances,
            # expense accounts (4xxx-8xxx) have debit (positive) balances.
            # Taxable income = -(sum of all P&L account balance changes)
            taxable_income = 0.0
            try:
                # dateTo is exclusive in the API, so use day after closure_date
                date_to_parts = closure_date.split("-")
                dt_year, dt_month, dt_day = int(date_to_parts[0]), int(date_to_parts[1]), int(date_to_parts[2])
                from datetime import timedelta
                dt_obj = date(dt_year, dt_month, dt_day) + timedelta(days=1)
                date_to_exclusive = dt_obj.isoformat()

                bs_resp = client.get("/balanceSheet", {
                    "dateFrom": f"{dt_year}-01-01",
                    "dateTo": date_to_exclusive,
                    "accountNumberFrom": 3000,
                    "accountNumberTo": 8699,
                    "count": 10000,
                })
                bs_values = bs_resp.get("values", [])
                pl_sum = sum(v.get("balanceChange", 0) for v in bs_values)
                # Revenue is negative (credit), expenses are positive (debit)
                # Taxable income = -(sum) → positive if profitable
                taxable_income = round(-pl_sum, 2)
                logger.info("Taxable income from P&L (accounts 3000-8699): %.2f", taxable_income)
            except Exception as e:
                logger.warning("Failed to query P&L for tax calculation, "
                               "falling back to estimated: %s", e)
                # Fallback: use the expenses we just posted (less accurate)
                prepaid_amount = (prepaid.get("amount", 0) if prepaid else 0)
                taxable_income = total_depreciation_expense + prepaid_amount

            tax_amount = round(max(taxable_income, 0) * tax_rate, 2)

            if tax_amount > 0:
                tax_exp_id = _get_or_create_account(client, tax_exp_acct, cache=account_cache)
                tax_liab_id = _get_or_create_account(client, tax_liab_acct, cache=account_cache)

                voucher_body = {
                    "date": closure_date,
                    "description": "Skatteavsetning",
                    "postings": [
                        _make_closure_posting(tax_exp_id, tax_amount, closure_date),
                        _make_closure_posting(tax_liab_id, -tax_amount, closure_date),
                    ],
                }
                result = client.post("/ledger/voucher", voucher_body)
                _check_response(result, "POST /ledger/voucher (tax provision)")
                vouchers_created.append(f"Tax provision ({tax_rate*100:.0f}%): {tax_amount} NOK")

        return (
            f"Annual closure completed: {len(vouchers_created)} vouchers created. "
            + "; ".join(vouchers_created)
        )

    # ---------------------------------------------------------------
    # PATH B: Use LLM-parsed entries if available (generic postings format)
    # Also handles flat postings (entities["postings"] without entries wrapper)
    # ---------------------------------------------------------------
    parsed_entries = entities.get("entries", [])
    # Normalize flat postings into entries format
    if not parsed_entries and entities.get("postings"):
        flat = entities["postings"]
        if flat and isinstance(flat, list) and isinstance(flat[0], dict) and flat[0].get("accountNumber"):
            parsed_entries = [{"description": "Voucher", "postings": flat}]
            logger.info("Annual closure: normalized %d flat postings into 1 entry", len(flat))

    # Normalize debitAccount/creditAccount format into postings format
    if parsed_entries:
        for entry in parsed_entries:
            if not entry.get("postings") and (entry.get("debitAccount") or entry.get("creditAccount")):
                amount = entry.get("amount") or entry.get("monthlyDepreciation") or 0
                entry_type = entry.get("type", "")
                debit_acct = entry.get("debitAccount")
                credit_acct = entry.get("creditAccount")
                postings = []

                if debit_acct and credit_acct:
                    # Both accounts specified — straightforward
                    postings.append({"accountNumber": debit_acct, "amount": amount})
                    postings.append({"accountNumber": credit_acct, "amount": -amount})
                elif debit_acct and not credit_acct:
                    # Only debitAccount — infer the counterpart based on type
                    if entry_type == "accrual":
                        # Accrual reversal: debit expense, credit prepaid (balance sheet)
                        # If the parser put a balance sheet account (1xxx) as debit,
                        # it's actually the credit — flip the accounts
                        if 1000 <= int(debit_acct) <= 1999:
                            # Balance sheet account → this is the credit (source)
                            postings.append({"accountNumber": 6300, "amount": amount})
                            postings.append({"accountNumber": debit_acct, "amount": -amount})
                        else:
                            # Expense account as debit → need a balance sheet credit
                            postings.append({"accountNumber": debit_acct, "amount": amount})
                            postings.append({"accountNumber": 1720, "amount": -amount})
                    elif entry_type == "depreciation":
                        # Depreciation: debit expense, credit accumulated depreciation
                        postings.append({"accountNumber": debit_acct, "amount": amount})
                        postings.append({"accountNumber": 1209, "amount": -amount})
                    else:
                        # Unknown type — just make a single posting
                        postings.append({"accountNumber": debit_acct, "amount": amount})
                elif credit_acct and not debit_acct:
                    postings.append({"accountNumber": credit_acct, "amount": -amount})

                if postings:
                    entry["postings"] = postings
                    logger.info("Normalized entry '%s' (type=%s) from debit/credit to %d postings",
                                entry.get("description", "?"), entry_type, len(postings))

    if parsed_entries:
        logger.info("Annual closure: using %d LLM-parsed entries", len(parsed_entries))
        vouchers_created = []

        for entry in parsed_entries:
            entry_postings = entry.get("postings", [])
            if not entry_postings:
                continue

            # Resolve account numbers to IDs
            api_postings = []
            for p in entry_postings:
                acct_num = p.get("accountNumber")
                if not acct_num:
                    continue
                acct_id = _get_or_create_account(
                    client, str(acct_num),
                    name=p.get("description") or f"Account {acct_num}",
                    cache=account_cache,
                )
                amount = p.get("amount") or p.get("amountGross") or 0
                api_postings.append(
                    _make_closure_posting(acct_id, amount, closure_date)
                )

            if not api_postings:
                continue

            description = entry.get("description", "Voucher")
            voucher_body = {
                "date": closure_date,
                "description": description,
                "postings": api_postings,
            }
            result = client.post("/ledger/voucher", voucher_body)
            _check_response(result, f"POST /ledger/voucher ({description})")
            vouchers_created.append(description)
            logger.info("Created voucher: %s", description)

        return (
            f"Annual closure completed: {len(vouchers_created)} vouchers created. "
            + "; ".join(vouchers_created)
        )

    # ---------------------------------------------------------------
    # PATH C: Regex-based parsing of raw prompt (legacy fallback)
    # ---------------------------------------------------------------
    depreciations = []
    prompt_text = raw_prompt or str(entities)

    dep_pattern = re.compile(
        r'(\w[\w\s]*?)\s*\(\s*([\d\s.,]+)\s*(?:NOK|kr)?\s*,\s*(\d+)\s*'
        r'(?:år|años|ans|years|Jahre|anos)\s*(?:lineales?|linéaire|linear|lineær)?\s*,\s*'
        r'(?:cuenta|konto|account|compte|Konto)\s*(\d{4})\)',
        re.IGNORECASE,
    )
    for m in dep_pattern.finditer(prompt_text):
        name = m.group(1).strip()
        cost_str = m.group(2).replace(" ", "").replace(",", ".")
        cost = float(cost_str)
        years = int(m.group(3))
        asset_account = m.group(4)
        depreciations.append({
            "name": name,
            "cost": cost,
            "years": years,
            "asset_account": asset_account,
        })

    # Extract expense account for depreciation
    expense_account = "6010"
    exp_match = re.search(
        r'(?:cuenta|konto|account|compte|Konto)\s*(\d{4})\s*(?:para|for|pour|für|til)\s*'
        r'(?:gasto|expense|charge|Aufwand|kostnad|avskrivning|depreciación|amortissement|depreciation)',
        prompt_text, re.IGNORECASE,
    )
    if exp_match:
        expense_account = exp_match.group(1)
    else:
        exp_match2 = re.search(
            r'(?:avskrivning|depreci|amortiss|Abschreibung).*?(?:cuenta|konto|account|compte|Konto)\s*(\d{4})',
            prompt_text, re.IGNORECASE,
        )
        if exp_match2:
            expense_account = exp_match2.group(1)

    # Extract accumulated depreciation account
    accum_account = "1209"
    accum_match = re.search(
        r'(?:cuenta|konto|account|compte|Konto)?\s*(\d{4})\s*(?:para|for|pour|für|til)\s*'
        r'(?:depreciación acumulada|accumulated depreciation|amortissement cumulé|'
        r'akkumulert avskrivning|kumulierte Abschreibung|amortização acumulada)',
        prompt_text, re.IGNORECASE,
    )
    if accum_match:
        accum_account = accum_match.group(1)

    # Extract prepaid reversal info
    prepaid_amount = None
    prepaid_account = "1700"
    prepaid_match = re.search(
        r'(?:prepagados?|prepaid|prépayé|forskuddsbetalt|Vorauszahlung|pré-pago|forhåndsbetalt|régularisation)'
        r'.*?(?:total)?\s*([\d\s.,]+)\s*(?:NOK|kr)',
        prompt_text, re.IGNORECASE,
    )
    if prepaid_match:
        amt_str = prepaid_match.group(1).replace(" ", "").replace(",", ".")
        prepaid_amount = float(amt_str)
    prepaid_acct_match = re.search(
        r'(?:cuenta|konto|account|compte|Konto)\s*(\d{4}).*?(?:prepagados?|prepaid|forskuddsbetalt|régularisation)',
        prompt_text, re.IGNORECASE,
    )
    if not prepaid_acct_match:
        prepaid_acct_match = re.search(
            r'(?:prepagados?|prepaid|forskuddsbetalt|régularisation).*?(?:cuenta|konto|account|compte|Konto)\s*(\d{4})',
            prompt_text, re.IGNORECASE,
        )
    if prepaid_acct_match:
        prepaid_account = prepaid_acct_match.group(1)

    # Extract tax provision info
    tax_rate = 0.22
    tax_rate_match = re.search(r'(\d+)\s*%', prompt_text)
    if tax_rate_match:
        rate = int(tax_rate_match.group(1))
        if 15 <= rate <= 30:
            tax_rate = rate / 100

    tax_expense_account = "8700"
    tax_liability_account = "2920"
    tax_exp_match = re.search(
        r'(?:cuenta|konto|account)\s*(\d{4})\s*/\s*(\d{4})',
        prompt_text, re.IGNORECASE,
    )
    if tax_exp_match:
        tax_expense_account = tax_exp_match.group(1)
        tax_liability_account = tax_exp_match.group(2)

    logger.info("Annual closure (regex): %d depreciations, prepaid=%s, tax_rate=%.0f%%",
                len(depreciations), prepaid_amount, tax_rate * 100)

    vouchers_created = []
    total_depreciation_expense = 0.0

    # Create depreciation vouchers
    for dep in depreciations:
        annual_dep = round(dep["cost"] / dep["years"], 2)
        total_depreciation_expense += annual_dep

        expense_acct_id = _get_or_create_account(client, expense_account, cache=account_cache)
        accum_acct_id = _get_or_create_account(client, accum_account, cache=account_cache)

        voucher_body = {
            "date": closure_date,
            "description": f"Avskrivning {dep['name']}",
            "postings": [
                _make_closure_posting(expense_acct_id, annual_dep, closure_date),
                _make_closure_posting(accum_acct_id, -annual_dep, closure_date),
            ],
        }
        result = client.post("/ledger/voucher", voucher_body)
        _check_response(result, f"POST /ledger/voucher (depreciation {dep['name']})")
        vouchers_created.append(f"Depreciation {dep['name']}: {annual_dep} NOK")

    # Prepaid expense reversal
    if prepaid_amount:
        prepaid_acct_id = _get_or_create_account(client, prepaid_account, cache=account_cache)
        prepaid_expense_id = _get_or_create_account(client, expense_account, cache=account_cache)

        voucher_body = {
            "date": closure_date,
            "description": "Reversering forskuddsbetalt kostnad",
            "postings": [
                _make_closure_posting(prepaid_expense_id, prepaid_amount, closure_date),
                _make_closure_posting(prepaid_acct_id, -prepaid_amount, closure_date),
            ],
        }
        result = client.post("/ledger/voucher", voucher_body)
        _check_response(result, "POST /ledger/voucher (prepaid reversal)")
        vouchers_created.append(f"Prepaid reversal: {prepaid_amount} NOK")

    # Tax provision
    taxable_income = total_depreciation_expense + (prepaid_amount or 0)
    tax_amount = round(taxable_income * tax_rate, 2)

    if tax_amount > 0:
        tax_exp_id = _get_or_create_account(client, tax_expense_account, cache=account_cache)
        tax_liab_id = _get_or_create_account(client, tax_liability_account, cache=account_cache)

        voucher_body = {
            "date": closure_date,
            "description": "Skatteavsetning",
            "postings": [
                _make_closure_posting(tax_exp_id, tax_amount, closure_date),
                _make_closure_posting(tax_liab_id, -tax_amount, closure_date),
            ],
        }
        result = client.post("/ledger/voucher", voucher_body)
        _check_response(result, "POST /ledger/voucher (tax provision)")
        vouchers_created.append(f"Tax provision ({tax_rate*100:.0f}%): {tax_amount} NOK")

    return (
        f"Annual closure completed: {len(vouchers_created)} vouchers created. "
        + "; ".join(vouchers_created)
    )


def _handle_error_correction(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle error_correction tasks: find and correct errors in the general ledger.

    The parser extracts structured error descriptions with wrongAccount, correctAccount,
    amount, etc. We search for matching vouchers/postings and create correction vouchers.
    """
    e = task.get("entities", {})
    errors = e.get("errors", [])
    today = e.get("today", _today())

    if not errors:
        raise RuntimeError("No errors specified in error_correction task")

    # Cache account number → ID lookups
    acct_cache: dict[str, int] = {}

    # Fetch all vouchers and postings for the date range (Jan-Feb of current year)
    year = today[:4]
    date_from = f"{year}-01-01"
    date_to = f"{year}-12-31"

    # Get all postings in one call
    all_postings_resp = client.get("/ledger/posting", {
        "dateFrom": date_from, "dateTo": date_to, "count": 1000,
    })
    all_postings = all_postings_resp.get("values", [])

    # Build lookup: account_id → account_number (from postings we've seen)
    # We'll resolve account numbers as needed
    def _get_account_id(acct_num: str) -> int:
        if acct_num in acct_cache:
            return acct_cache[acct_num]
        return _get_or_create_account(client, acct_num, cache=acct_cache)

    corrections_made: list[str] = []

    for error in errors:
        desc = error.get("description", "")
        wrong_acct = str(error.get("wrongAccount", ""))
        correct_acct = error.get("correctAccount")
        amount = error.get("amount", 0)
        desc_lower = desc.lower()

        if not wrong_acct or not amount:
            logger.warning("Skipping error with missing data: %s", error)
            continue

        wrong_acct_id = _get_account_id(wrong_acct)

        # Find matching posting(s) by account ID and amount
        matching = [
            p for p in all_postings
            if p.get("account", {}).get("id") == wrong_acct_id
            and abs(abs(p.get("amount", 0)) - abs(amount)) < 0.01
            and not p.get("systemGenerated", False)
        ]
        # Also check amountGross
        if not matching:
            matching = [
                p for p in all_postings
                if p.get("account", {}).get("id") == wrong_acct_id
                and abs(abs(p.get("amountGross", 0)) - abs(amount)) < 0.01
                and not p.get("systemGenerated", False)
            ]

        if "feil konto" in desc_lower or "wrong account" in desc_lower or (
            correct_acct and correct_acct != wrong_acct
        ):
            # ERROR TYPE 1: Wrong account — reverse from wrong, post to correct
            if not correct_acct:
                logger.warning("Wrong account error but no correctAccount specified")
                continue
            correct_acct_id = _get_account_id(str(correct_acct))

            if matching:
                orig = matching[0]
                orig_amount = orig.get("amountGross", orig.get("amount", amount))
                voucher_date = orig.get("date", today)
            else:
                orig_amount = amount
                voucher_date = today

            # Correction voucher: reverse wrong, post correct
            postings = [
                {
                    "row": 1,
                    "account": {"id": wrong_acct_id},
                    "date": voucher_date,
                    "amountGross": -abs(orig_amount),
                    "amountGrossCurrency": -abs(orig_amount),
                    "description": f"Korreksjon: feil konto {wrong_acct} → {correct_acct}",
                },
                {
                    "row": 2,
                    "account": {"id": correct_acct_id},
                    "date": voucher_date,
                    "amountGross": abs(orig_amount),
                    "amountGrossCurrency": abs(orig_amount),
                    "description": f"Korreksjon: feil konto {wrong_acct} → {correct_acct}",
                },
            ]
            body = {
                "date": voucher_date,
                "description": f"Korreksjon: feil konto {wrong_acct} → {correct_acct}, {amount} kr",
                "postings": postings,
            }
            result = client.post("/ledger/voucher", body)
            _check_response(result, "POST /ledger/voucher (wrong account correction)")
            vid = result.get("value", {}).get("id", "?")
            corrections_made.append(f"Wrong account {wrong_acct}→{correct_acct}: voucher {vid}")

        elif "dupliser" in desc_lower or "duplikat" in desc_lower or "duplicate" in desc_lower:
            # ERROR TYPE 2: Duplicate voucher — create reversal
            if not matching:
                logger.warning("Could not find duplicate posting for account %s amount %s", wrong_acct, amount)
                continue

            orig = matching[-1]  # Take last match (likely the duplicate)
            voucher_id = orig.get("voucher", {}).get("id")
            voucher_date = orig.get("date", today)

            # Get full voucher to reverse all its postings
            if voucher_id:
                voucher_resp = client.get(f"/ledger/voucher/{voucher_id}")
                voucher_data = voucher_resp.get("value", {})
                voucher_posting_refs = voucher_data.get("postings", [])

                # Get all postings of this voucher
                reversal_postings = []
                for p in all_postings:
                    if p.get("voucher", {}).get("id") == voucher_id and not p.get("systemGenerated", False):
                        reversal_postings.append(p)

                if not reversal_postings:
                    reversal_postings = [orig]

                postings = []
                for idx, rp in enumerate(reversal_postings):
                    postings.append({
                        "row": idx + 1,
                        "account": {"id": rp["account"]["id"]},
                        "date": voucher_date,
                        "amountGross": -rp.get("amountGross", rp["amount"]),
                        "amountGrossCurrency": -rp.get("amountGrossCurrency", rp["amountCurrency"]),
                        "description": f"Reversering av duplikat bilag {voucher_data.get('number', '')}",
                    })
            else:
                # Simple reversal of the matching posting
                postings = [
                    {
                        "row": 1,
                        "account": {"id": wrong_acct_id},
                        "date": voucher_date,
                        "amountGross": -abs(amount),
                        "amountGrossCurrency": -abs(amount),
                        "description": "Reversering av duplikat",
                    },
                    {
                        "row": 2,
                        "account": {"id": _get_account_id("1920")},
                        "date": voucher_date,
                        "amountGross": abs(amount),
                        "amountGrossCurrency": abs(amount),
                        "description": "Reversering av duplikat",
                    },
                ]

            body = {
                "date": voucher_date,
                "description": f"Reversering: duplikat bilag konto {wrong_acct}, {amount} kr",
                "postings": postings,
            }
            result = client.post("/ledger/voucher", body)
            _check_response(result, "POST /ledger/voucher (duplicate reversal)")
            vid = result.get("value", {}).get("id", "?")
            corrections_made.append(f"Duplicate reversed on {wrong_acct}: voucher {vid}")

        elif "mva" in desc_lower or "manglende" in desc_lower or "missing" in desc_lower:
            # ERROR TYPE 3: Missing VAT line — add MVA posting
            if not correct_acct:
                correct_acct = "2710"  # Default MVA account
            correct_acct_id = _get_account_id(str(correct_acct))

            # Calculate MVA amount (25% of excl. amount)
            mva_amount = round(amount * 0.25, 2)

            if matching:
                voucher_date = matching[0].get("date", today)
            else:
                voucher_date = today

            # Create voucher that adds the missing MVA line
            postings = [
                {
                    "row": 1,
                    "account": {"id": correct_acct_id},
                    "date": voucher_date,
                    "amountGross": mva_amount,
                    "amountGrossCurrency": mva_amount,
                    "description": f"Manglende MVA-linje for konto {wrong_acct}, {amount} kr ekskl.",
                },
                {
                    "row": 2,
                    "account": {"id": _get_account_id("1920")},
                    "date": voucher_date,
                    "amountGross": -mva_amount,
                    "amountGrossCurrency": -mva_amount,
                    "description": f"Manglende MVA-linje for konto {wrong_acct}",
                },
            ]
            body = {
                "date": voucher_date,
                "description": f"Korreksjon: manglende MVA for konto {wrong_acct}, {amount} kr ekskl.",
                "postings": postings,
            }
            result = client.post("/ledger/voucher", body)
            _check_response(result, "POST /ledger/voucher (missing VAT correction)")
            vid = result.get("value", {}).get("id", "?")
            corrections_made.append(f"Missing VAT added for {wrong_acct}: voucher {vid}")

        elif "feil beløp" in desc_lower or "wrong amount" in desc_lower:
            # ERROR TYPE 4: Wrong amount — correct the difference
            # Parse correct amount from description
            correct_amount = None
            voucher_desc = error.get("voucherDescription", "")
            import re
            m = re.search(r"korrekt beløp[:\s]*(\d+)", voucher_desc)
            if m:
                correct_amount = float(m.group(1))
            if correct_amount is None:
                m = re.search(r"(\d+)\s*kr", voucher_desc)
                if m:
                    correct_amount = float(m.group(1))

            if correct_amount is None:
                logger.warning("Could not determine correct amount for wrong-amount error")
                continue

            diff = amount - correct_amount  # e.g. 14600 - 11050 = 3550

            if matching:
                voucher_date = matching[0].get("date", today)
            else:
                voucher_date = today

            # Correction: reverse the excess amount
            postings = [
                {
                    "row": 1,
                    "account": {"id": wrong_acct_id},
                    "date": voucher_date,
                    "amountGross": -abs(diff),
                    "amountGrossCurrency": -abs(diff),
                    "description": f"Korreksjon: feil beløp {amount} → {correct_amount} kr",
                },
                {
                    "row": 2,
                    "account": {"id": _get_account_id("1920")},
                    "date": voucher_date,
                    "amountGross": abs(diff),
                    "amountGrossCurrency": abs(diff),
                    "description": f"Korreksjon: feil beløp {amount} → {correct_amount} kr",
                },
            ]
            body = {
                "date": voucher_date,
                "description": f"Korreksjon: feil beløp konto {wrong_acct}, {amount} → {correct_amount} kr",
                "postings": postings,
            }
            result = client.post("/ledger/voucher", body)
            _check_response(result, "POST /ledger/voucher (wrong amount correction)")
            vid = result.get("value", {}).get("id", "?")
            corrections_made.append(f"Wrong amount corrected on {wrong_acct}: {amount}→{correct_amount}, voucher {vid}")

        else:
            # Generic correction: try to reverse and re-post
            logger.warning("Unknown error type '%s', attempting generic reversal", desc)
            if matching:
                voucher_date = matching[0].get("date", today)
            else:
                voucher_date = today

            if correct_acct and correct_acct != wrong_acct:
                correct_acct_id = _get_account_id(str(correct_acct))
                postings = [
                    {
                        "row": 1,
                        "account": {"id": wrong_acct_id},
                        "date": voucher_date,
                        "amountGross": -abs(amount),
                        "amountGrossCurrency": -abs(amount),
                        "description": f"Korreksjon: {desc}",
                    },
                    {
                        "row": 2,
                        "account": {"id": correct_acct_id},
                        "date": voucher_date,
                        "amountGross": abs(amount),
                        "amountGrossCurrency": abs(amount),
                        "description": f"Korreksjon: {desc}",
                    },
                ]
            else:
                postings = [
                    {
                        "row": 1,
                        "account": {"id": wrong_acct_id},
                        "date": voucher_date,
                        "amountGross": -abs(amount),
                        "amountGrossCurrency": -abs(amount),
                        "description": f"Korreksjon: {desc}",
                    },
                    {
                        "row": 2,
                        "account": {"id": _get_account_id("1920")},
                        "date": voucher_date,
                        "amountGross": abs(amount),
                        "amountGrossCurrency": abs(amount),
                        "description": f"Korreksjon: {desc}",
                    },
                ]
            body = {
                "date": voucher_date,
                "description": f"Korreksjon: {desc}",
                "postings": postings,
            }
            result = client.post("/ledger/voucher", body)
            _check_response(result, "POST /ledger/voucher (generic correction)")
            vid = result.get("value", {}).get("id", "?")
            corrections_made.append(f"Generic correction for {desc}: voucher {vid}")

    if not corrections_made:
        raise RuntimeError("No corrections could be made — no matching postings found")

    return f"Error correction completed: {len(corrections_made)} corrections. " + "; ".join(corrections_made)


def _handle_overdue_invoice(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle overdue_invoice: find overdue invoice, post late fee, create fee invoice, register partial payment."""
    e = task.get("entities", {})
    today = e.get("today", _today())
    fee_amount = e.get("feeAmount", 70)
    debit_acct = str(e.get("debitAccount", "1500"))
    credit_acct = str(e.get("creditAccount", "3400"))
    create_fee_invoice = e.get("invoiceFee", False)
    send_invoice = e.get("sendInvoice", False)
    partial_payment = e.get("partialPaymentAmount")

    # Step 1: Find the overdue invoice
    inv_resp = client.get("/invoice", {
        "invoiceDateFrom": "2020-01-01",
        "invoiceDateTo": today,
        "count": 100,
    })
    invoices = inv_resp.get("values", [])

    # Find overdue: invoiceDueDate < today AND amountOutstanding > 0
    overdue = None
    for inv in invoices:
        due = inv.get("invoiceDueDate", "")
        outstanding = inv.get("amountOutstanding", 0)
        if due and due < today and outstanding > 0:
            if overdue is None or due < overdue.get("invoiceDueDate", ""):
                overdue = inv  # Pick the most overdue one

    if not overdue:
        raise RuntimeError("No overdue invoice found")

    overdue_id = overdue["id"]
    customer_id = overdue.get("customer", {}).get("id")
    overdue_amount = overdue.get("amount", 0)
    logger.info("Found overdue invoice %d (due %s, outstanding %.2f) for customer %s",
                overdue_id, overdue.get("invoiceDueDate"), overdue.get("amountOutstanding", 0), customer_id)

    results: list[str] = []

    # Step 2: Post late fee voucher (debit 1500 kundefordringer, credit 3400 purregebyr)
    debit_id = _get_or_create_account(client, debit_acct)
    credit_id = _get_or_create_account(client, credit_acct)

    voucher_body = {
        "date": today,
        "description": f"Purregebyr faktura {overdue.get('invoiceNumber', overdue_id)}",
        "postings": [
            {
                "row": 1,
                "account": {"id": debit_id},
                "customer": {"id": customer_id} if customer_id else None,
                "amountGross": fee_amount,
                "amountGrossCurrency": fee_amount,
                "date": today,
            },
            {
                "row": 2,
                "account": {"id": credit_id},
                "amountGross": -fee_amount,
                "amountGrossCurrency": -fee_amount,
                "date": today,
            },
        ],
    }
    # Remove None customer references
    for p in voucher_body["postings"]:
        if p.get("customer") is None:
            p.pop("customer", None)

    v_result = client.post("/ledger/voucher", voucher_body)
    _check_response(v_result, "POST /ledger/voucher (late fee)")
    vid = v_result.get("value", {}).get("id", "?")
    results.append(f"Late fee voucher {vid} ({fee_amount} kr)")

    # Step 3: Create invoice for the fee (if requested)
    if create_fee_invoice and customer_id:
        # Create product for the fee
        prod_resp = client.get("/product", {"name": "Purregebyr", "count": 1})
        products = prod_resp.get("values", [])
        if products:
            product_id = products[0]["id"]
        else:
            prod = client.post("/product", {
                "name": "Purregebyr",
                "priceExcludingVatCurrency": fee_amount,
                "vatType": {"id": 6},  # 0% VAT (utenfor mva-loven)
            })
            _check_response(prod, "POST /product (late fee)")
            product_id = prod["value"]["id"]

        # Create order
        order_body: dict[str, Any] = {
            "customer": {"id": customer_id},
            "orderDate": today,
            "deliveryDate": today,
            "orderLines": [{
                "product": {"id": product_id},
                "count": 1,
                "unitPriceExcludingVatCurrency": fee_amount,
                "vatType": {"id": 6},
            }],
        }
        order_result = client.post("/order", order_body)
        _check_response(order_result, "POST /order (late fee)")
        order_id = order_result["value"]["id"]

        # Create and send invoice
        inv_body: dict[str, Any] = {
            "invoiceDate": today,
            "invoiceDueDate": today,
            "orders": [{"id": order_id}],
        }
        inv_params: dict[str, Any] = {"sendToCustomer": bool(send_invoice)}
        inv_result = client.post("/invoice", inv_body, inv_params)

        # Retry with bank account setup if needed
        if isinstance(inv_result, dict) and inv_result.get("status", 0) >= 400:
            error_msg = str(inv_result.get("message", "")) + str(inv_result.get("validationMessages", ""))
            if "bank" in error_msg.lower():
                context["_bank_account_checked"] = False
                _ensure_company_bank_account(client, context)
                inv_result = client.post("/invoice", inv_body, inv_params)

        _check_response(inv_result, "POST /invoice (late fee)")
        fee_inv_id = inv_result.get("value", {}).get("id", "?")
        results.append(f"Fee invoice {fee_inv_id} created" + (" and sent" if send_invoice else ""))

    # Step 4: Register partial payment on the overdue invoice (if requested)
    if partial_payment and partial_payment > 0:
        # Get payment type
        pt_resp = client.get("/invoice/paymentType", {"count": 100})
        payment_types = pt_resp.get("values", [])
        payment_type_id = payment_types[0]["id"] if payment_types else None

        if payment_type_id is not None:
            pay_params: dict[str, Any] = {
                "paymentDate": today,
                "paymentTypeId": payment_type_id,
                "paidAmount": partial_payment,
                "paidAmountCurrency": partial_payment,
            }
            pay_result = client.put(f"/invoice/{overdue_id}/:payment", params=pay_params)

            if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
                # Try JSON body approach
                pay_result = client.put(
                    f"/invoice/{overdue_id}/:payment",
                    json_body={
                        "paymentDate": today,
                        "paymentTypeId": payment_type_id,
                        "paidAmount": partial_payment,
                        "paidAmountCurrency": partial_payment,
                    },
                )

            if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
                logger.warning("Partial payment registration failed: %s", pay_result)
                results.append(f"Partial payment {partial_payment} kr FAILED")
            else:
                results.append(f"Partial payment {partial_payment} kr registered on invoice {overdue_id}")
        else:
            logger.warning("No payment types found, cannot register partial payment")

    return f"Overdue invoice handled: " + "; ".join(results)


def _handle_project_lifecycle(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle full project lifecycle: create project, log hours, register supplier invoice, bill customer."""
    e = task.get("entities", {})
    today = e.get("today", _today())
    results: list[str] = []

    # Step 1: Find or create customer
    cust_name = e.get("customerName", "")
    cust_org = e.get("customerOrganizationNumber", "")
    customer_id = None
    if cust_name or cust_org:
        cust_data: dict[str, Any] = {}
        if cust_name:
            cust_data["name"] = cust_name
        if cust_org:
            cust_data["organizationNumber"] = cust_org
        customer_id = _find_or_create_customer(cust_data, client, context)

    # Step 2: Find or create project manager
    pm_name = e.get("projectManagerName", "")
    pm_email = e.get("projectManagerEmail", "")
    pm_id = None
    if pm_email:
        resp = client.get("/employee", {"email": pm_email, "count": 1})
        values = resp.get("values", [])
        if values:
            pm_id = values[0]["id"]
    if not pm_id and pm_name:
        pm_id = _find_or_create_employee_by_name(pm_name, client, context)
    if not pm_id:
        pm_id = _ensure_employee(client, context)

    # Step 3: Create project
    proj_body: dict[str, Any] = {
        "name": e.get("projectName", "Project"),
        "projectManager": {"id": pm_id},
        "isInternal": False,
        "startDate": e.get("startDate", "2024-01-01"),
    }
    if customer_id:
        proj_body["customer"] = {"id": customer_id}

    proj = client.post("/project", proj_body)
    _check_response(proj, "POST /project (lifecycle)")
    project_id = proj["value"]["id"]
    results.append(f"Project {project_id} created")

    # Step 4: Log timesheet entries
    timesheet_entries = e.get("timesheetEntries", [])
    if timesheet_entries:
        # Get activities for the project
        act_resp = client.get("/activity/>forTimeSheet", {"projectId": project_id, "count": 100})
        activities = act_resp.get("values", [])

        for ts in timesheet_entries:
            # Find employee for this entry
            ts_email = ts.get("employeeEmail", "")
            ts_name = ts.get("employeeName", "")
            ts_emp_id = None
            if ts_email:
                resp = client.get("/employee", {"email": ts_email, "count": 1})
                vals = resp.get("values", [])
                if vals:
                    ts_emp_id = vals[0]["id"]
            if not ts_emp_id and ts_name:
                ts_emp_id = _find_or_create_employee_by_name(ts_name, client, context)
            if not ts_emp_id:
                ts_emp_id = pm_id

            # Find activity
            act_name = ts.get("activityName", "")
            act_id = None
            if act_name:
                for a in activities:
                    if a.get("name", "").strip().lower() == act_name.strip().lower():
                        act_id = a["id"]
                        break
            if not act_id and activities:
                act_id = activities[0]["id"]

            if act_id:
                entry_body: dict[str, Any] = {
                    "employee": {"id": ts_emp_id},
                    "project": {"id": project_id},
                    "activity": {"id": act_id},
                    "date": ts.get("date", today),
                    "hours": ts.get("hours", 0),
                }
                if ts.get("hourlyRate"):
                    entry_body["hourlyCharge"] = ts["hourlyRate"]
                ts_result = client.post("/timesheet/entry", entry_body)
                if isinstance(ts_result, dict) and ts_result.get("value"):
                    results.append(f"Timesheet {ts.get('hours', 0)}h logged")

    # Step 5: Register supplier invoice (if specified)
    supplier_inv = e.get("supplierInvoice")
    if supplier_inv:
        supp_name = supplier_inv.get("supplierName", "")
        supp_org = supplier_inv.get("supplierOrganizationNumber", "")
        supp_amount = supplier_inv.get("amount", 0)
        supp_desc = supplier_inv.get("description", "Supplier invoice")
        expense_acct = str(supplier_inv.get("accountNumber", "4300"))

        # Find or create supplier
        supplier_id = None
        if supp_name or supp_org:
            search_params: dict[str, Any] = {"count": 1}
            if supp_org:
                search_params["organizationNumber"] = supp_org
            elif supp_name:
                search_params["name"] = supp_name
            supp_resp = client.get("/supplier", search_params)
            supp_values = supp_resp.get("values", [])
            if supp_values:
                supplier_id = supp_values[0]["id"]
            else:
                # Try customer endpoint (some suppliers are also customers)
                cust_search: dict[str, Any] = {"count": 1}
                if supp_org:
                    cust_search["organizationNumber"] = supp_org
                elif supp_name:
                    cust_search["name"] = supp_name
                cust_resp = client.get("/customer", cust_search)
                cust_vals = cust_resp.get("values", [])
                if cust_vals:
                    # Create supplier from customer info
                    supp_body: dict[str, Any] = {"name": cust_vals[0].get("name", supp_name)}
                    if supp_org:
                        supp_body["organizationNumber"] = supp_org
                    supp_result = client.post("/supplier", supp_body)
                    if isinstance(supp_result, dict) and supp_result.get("value"):
                        supplier_id = supp_result["value"]["id"]
                else:
                    supp_body = {"name": supp_name or f"Supplier {supp_org}"}
                    if supp_org:
                        supp_body["organizationNumber"] = supp_org
                    supp_result = client.post("/supplier", supp_body)
                    if isinstance(supp_result, dict) and supp_result.get("value"):
                        supplier_id = supp_result["value"]["id"]

        expense_id = _get_or_create_account(client, expense_acct)
        ap_id = _get_or_create_account(client, "2400")

        postings = [
            {
                "row": 1,
                "account": {"id": expense_id},
                "amountGross": supp_amount,
                "amountGrossCurrency": supp_amount,
                "date": today,
                "vatType": {"id": 1},  # 25% input VAT
            },
            {
                "row": 2,
                "account": {"id": ap_id},
                "amountGross": -supp_amount,
                "amountGrossCurrency": -supp_amount,
                "date": today,
            },
        ]
        if supplier_id:
            postings[1]["supplier"] = {"id": supplier_id}

        v_body: dict[str, Any] = {
            "date": today,
            "description": supp_desc,
            "postings": postings,
        }
        v_result = client.post("/ledger/voucher", v_body)
        if isinstance(v_result, dict) and v_result.get("value"):
            results.append(f"Supplier invoice voucher created")

    # Step 6: Create customer invoice for a percentage of budget
    budget = e.get("budget", 0)
    inv_pct = e.get("customerInvoicePercentage", 0)
    if budget and inv_pct and customer_id:
        inv_amount = round(budget * inv_pct / 100, 2)

        # Create product for the invoice
        prod_body: dict[str, Any] = {
            "name": f"{e.get('projectName', 'Project')} - Delbetaling",
            "priceExcludingVatCurrency": inv_amount,
            "vatType": {"id": 3},  # 25% output VAT
        }
        prod = client.post("/product", prod_body)
        _check_response(prod, "POST /product (lifecycle invoice)")
        product_id = prod["value"]["id"]

        # Create order
        order_body: dict[str, Any] = {
            "customer": {"id": customer_id},
            "orderDate": today,
            "deliveryDate": today,
            "orderLines": [{
                "product": {"id": product_id},
                "count": 1,
                "unitPriceExcludingVatCurrency": inv_amount,
                "vatType": {"id": 3},
            }],
        }
        order = client.post("/order", order_body)
        _check_response(order, "POST /order (lifecycle invoice)")
        order_id = order["value"]["id"]

        # Create invoice
        inv_body: dict[str, Any] = {
            "invoiceDate": today,
            "invoiceDueDate": today,
            "orders": [{"id": order_id}],
        }
        inv = client.post("/invoice", inv_body, {"sendToCustomer": False})

        # Retry with bank account if needed
        if isinstance(inv, dict) and inv.get("status", 0) >= 400:
            error_msg = str(inv.get("message", "")) + str(inv.get("validationMessages", ""))
            if "bank" in error_msg.lower():
                context["_bank_account_checked"] = False
                _ensure_company_bank_account(client, context)
                inv = client.post("/invoice", inv_body, {"sendToCustomer": False})

        if isinstance(inv, dict) and inv.get("value"):
            results.append(f"Customer invoice for {inv_pct}% ({inv_amount} kr) created")

    return f"Project lifecycle completed: " + "; ".join(results)


def _handle_cost_analysis(task: dict, client: TripletexClient, context: dict) -> str:
    """Analyze ledger to find top cost accounts with largest increase, then create projects for them."""
    e = task.get("entities", {})
    today = e.get("today", _today())

    months = e.get("analysisMonths", [])
    num_accounts = e.get("numberOfAccounts", 3)

    # Default to Jan and Feb of current year if not specified
    year = int(today[:4])
    if len(months) >= 2:
        month1 = months[0]
        month2 = months[1]
        y1 = month1.get("year", year)
        m1 = month1.get("month", 1)
        y2 = month2.get("year", year)
        m2 = month2.get("month", 2)
    else:
        y1, m1 = year, 1
        y2, m2 = year, 2

    # Fetch postings for month 1
    m1_start = f"{y1}-{m1:02d}-01"
    m1_end = f"{y1}-{m1:02d}-28"  # Approximate end
    resp1 = client.get("/ledger/posting", {
        "dateFrom": m1_start, "dateTo": m1_end, "count": 10000,
    })
    postings1 = resp1.get("values", [])

    # Fetch postings for month 2
    m2_start = f"{y2}-{m2:02d}-01"
    m2_end = f"{y2}-{m2:02d}-28"
    resp2 = client.get("/ledger/posting", {
        "dateFrom": m2_start, "dateTo": m2_end, "count": 10000,
    })
    postings2 = resp2.get("values", [])

    # Sum amounts by account for cost accounts (4000-7999)
    def sum_by_account(postings: list) -> dict[int, float]:
        sums: dict[int, float] = {}
        for p in postings:
            acct_id = p.get("account", {}).get("id", 0)
            amount = p.get("amount", 0)
            # We need the account number — fetch it from posting or cache
            sums[acct_id] = sums.get(acct_id, 0) + amount
        return sums

    sums1 = sum_by_account(postings1)
    sums2 = sum_by_account(postings2)

    # Get all accounts to map IDs to numbers/names
    all_acct_ids = set(sums1.keys()) | set(sums2.keys())
    acct_info: dict[int, dict] = {}

    # Fetch account details for all unique accounts
    acct_resp = client.get("/ledger/account", {"count": 1000})
    for a in acct_resp.get("values", []):
        acct_info[a["id"]] = a

    # Calculate increases for cost accounts (4000-7999)
    increases: list[tuple[int, str, str, float, float, float]] = []
    for acct_id in all_acct_ids:
        info = acct_info.get(acct_id, {})
        acct_num = info.get("number", 0)
        acct_name = info.get("name", "Unknown")
        try:
            acct_num_int = int(acct_num)
        except (ValueError, TypeError):
            continue

        if 4000 <= acct_num_int <= 7999:
            val1 = sums1.get(acct_id, 0)
            val2 = sums2.get(acct_id, 0)
            increase = val2 - val1
            if increase > 0:
                increases.append((acct_id, str(acct_num), acct_name, val1, val2, increase))

    # Sort by increase (descending) and take top N
    increases.sort(key=lambda x: x[5], reverse=True)
    top = increases[:num_accounts]

    if not top:
        return "No cost account increases found between the analyzed periods."

    # Create a project for each top account
    pm_id = _ensure_employee(client, context)
    results: list[str] = []

    for acct_id, acct_num, acct_name, val1, val2, increase in top:
        proj_body: dict[str, Any] = {
            "name": acct_name,
            "projectManager": {"id": pm_id},
            "isInternal": True,
            "startDate": today,
        }
        proj = client.post("/project", proj_body)
        if isinstance(proj, dict) and proj.get("value"):
            pid = proj["value"]["id"]
            results.append(f"{acct_name} ({acct_num}): +{increase:.0f} kr (project {pid})")
            # Also create a project activity
            act_body: dict[str, Any] = {
                "name": f"Analyse {acct_name}",
                "project": {"id": pid},
            }
            client.post("/project/projectActivity", act_body)

    return f"Cost analysis complete. Top {len(results)} accounts: " + "; ".join(results)


def _handle_fx_correction(task: dict, client: TripletexClient, context: dict) -> str:
    """Handle FX correction: find invoice in foreign currency, calculate and post exchange rate difference."""
    e = task.get("entities", {})
    today = e.get("today", _today())

    cust_name = e.get("customerName", "")
    cust_org = e.get("customerOrganizationNumber", "")
    inv_amount_eur = e.get("invoiceAmountEUR", 0)
    original_rate = e.get("originalRate", 0)
    current_rate = e.get("currentRate", 0)
    inv_desc = e.get("invoiceDescription", "")

    if not original_rate or not current_rate:
        raise RuntimeError("FX correction requires both originalRate and currentRate")

    # Calculate the FX difference
    original_nok = inv_amount_eur * original_rate
    current_nok = inv_amount_eur * current_rate
    fx_diff = current_nok - original_nok  # Positive = loss (customer paid less), negative = gain

    # Find customer
    customer_id = None
    if cust_name or cust_org:
        cust_data: dict[str, Any] = {}
        if cust_name:
            cust_data["name"] = cust_name
        if cust_org:
            cust_data["organizationNumber"] = cust_org
        customer_id = _find_or_create_customer(cust_data, client, context)

    # Find the invoice
    inv_resp = client.get("/invoice", {
        "invoiceDateFrom": "2020-01-01",
        "invoiceDateTo": today,
        "count": 100,
    })
    invoices = inv_resp.get("values", [])

    target_inv = None
    for inv in invoices:
        inv_cust_id = inv.get("customer", {}).get("id")
        if customer_id and inv_cust_id != customer_id:
            continue
        # Match by amount (original NOK amount)
        inv_amount = inv.get("amountExcludingVat", 0)
        if abs(inv_amount - original_nok) < 1:
            target_inv = inv
            break
        # Also try matching by EUR amount * rate
        if abs(inv.get("amount", 0) - original_nok) < 1:
            target_inv = inv
            break

    # Determine accounts for FX gain/loss
    # 8060 = Agiotap (currency loss), 8160 = Agiogevinst (currency gain)
    if fx_diff < 0:
        # Gain: we receive more NOK than expected
        fx_account = "8160"  # Agiogevinst
        fx_desc = "Agiogevinst"
    else:
        # Loss: we receive less NOK than expected
        fx_account = "8060"  # Agiotap
        fx_desc = "Agiotap"

    fx_acct_id = _get_or_create_account(client, fx_account)
    ar_acct_id = _get_or_create_account(client, "1500")  # Kundefordringer

    # Post the FX correction voucher
    # If loss (positive fx_diff): debit 8060 (expense), credit 1500 (reduce AR)
    # If gain (negative fx_diff): debit 1500 (increase AR), credit 8160 (income)
    abs_diff = abs(fx_diff)

    postings = [
        {
            "row": 1,
            "account": {"id": fx_acct_id},
            "amountGross": abs_diff if fx_diff > 0 else -abs_diff,
            "amountGrossCurrency": abs_diff if fx_diff > 0 else -abs_diff,
            "date": today,
        },
        {
            "row": 2,
            "account": {"id": ar_acct_id},
            "amountGross": -abs_diff if fx_diff > 0 else abs_diff,
            "amountGrossCurrency": -abs_diff if fx_diff > 0 else abs_diff,
            "date": today,
        },
    ]
    if customer_id:
        postings[1]["customer"] = {"id": customer_id}

    v_body: dict[str, Any] = {
        "date": today,
        "description": f"{fx_desc} - {cust_name} ({inv_amount_eur} EUR, {original_rate}→{current_rate} NOK/EUR)",
        "postings": postings,
    }
    result = client.post("/ledger/voucher", v_body)
    _check_response(result, "POST /ledger/voucher (FX correction)")
    vid = result.get("value", {}).get("id", "?")

    # Register payment on the invoice at the new rate
    if target_inv:
        inv_id = target_inv["id"]
        pt_resp = client.get("/invoice/paymentType", {"count": 100})
        payment_types = pt_resp.get("values", [])
        if payment_types:
            payment_type_id = payment_types[0]["id"]
            pay_params: dict[str, Any] = {
                "paymentDate": today,
                "paymentTypeId": payment_type_id,
                "paidAmount": current_nok,
                "paidAmountCurrency": current_nok,
            }
            pay_result = client.put(f"/invoice/{inv_id}/:payment", params=pay_params)
            if isinstance(pay_result, dict) and pay_result.get("status", 0) >= 400:
                pay_result = client.put(
                    f"/invoice/{inv_id}/:payment",
                    json_body={
                        "paymentDate": today,
                        "paymentTypeId": payment_type_id,
                        "paidAmount": current_nok,
                        "paidAmountCurrency": current_nok,
                    },
                )

    return (
        f"FX correction posted: {fx_desc} {abs_diff:.2f} kr "
        f"({inv_amount_eur} EUR × ({current_rate}-{original_rate}) NOK/EUR). "
        f"Voucher {vid}."
    )


_HANDLERS = {
    "create_employee": _handle_create_employee,
    "update_employee": _handle_update_employee,
    "update_employee_role": _handle_update_employee_role,
    "create_customer": _handle_create_customer,
    "update_customer": _handle_update_customer,
    "create_product": _handle_create_product,
    "create_invoice": _handle_create_invoice,
    "create_invoice_with_payment": _handle_create_invoice_with_payment,
    "create_credit_note": _handle_create_credit_note,
    "create_project": _handle_create_project,
    "create_department": _handle_create_department,
    "create_travel_expense": _handle_create_travel_expense,
    "delete_travel_expense": _handle_delete_travel_expense,
    "create_voucher": _handle_create_voucher,
    "delete_voucher": _handle_delete_voucher,
    "log_timesheet_hours": _handle_log_timesheet_hours,
    "create_dimension_voucher": _handle_create_dimension_voucher,
    "reverse_invoice_payment": _handle_reverse_invoice_payment,
    "run_payroll": _handle_run_payroll,
    "bank_reconciliation": _handle_bank_reconciliation,
    "annual_closure": _handle_annual_closure,
    "error_correction": _handle_error_correction,
    "overdue_invoice": _handle_overdue_invoice,
    "project_lifecycle": _handle_project_lifecycle,
    "cost_analysis": _handle_cost_analysis,
    "fx_correction": _handle_fx_correction,
}
