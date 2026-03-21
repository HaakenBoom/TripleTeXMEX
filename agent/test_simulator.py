"""Realistic Tripletex API simulator based on actual competition run data.

Replicates real API behavior including:
- Pre-seeded data (department, employee, products with VAT types)
- Proper validation (vatType must be {"id": N}, "count" not "quantity", unique names)
- Bank account requirement for invoices
- Real error response format (status, code, message, validationMessages)
- Real response envelope format ({"value": {...}} and {"fullResultSize": N, "values": [...]})
- Endpoint quirks (GET /company → 400, GET /company/{id} → 200)

Usage:
    python -m agent.test_simulator
"""

import json
import logging
import re
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Realistic Fake Tripletex API
# ---------------------------------------------------------------------------

def _error_response(status: int, code: int, message: str,
                    validation_messages: list[dict] | None = None) -> dict:
    """Build a Tripletex-style error response."""
    return {
        "status": status,
        "code": code,
        "message": message,
        "link": "https://tripletex.no/v2-docs/",
        "developerMessage": "VALIDATION_ERROR" if status == 422 else None,
        "validationMessages": validation_messages,
        "requestId": "fake-request-id",
    }


class FakeTripletexClient:
    """Realistic Tripletex API simulator based on actual competition run data."""

    # Realistic large IDs matching what the real API returns
    COMPANY_ID = 107827685
    DEPT_ID = 697386
    ADMIN_EMPLOYEE_ID = 18169775

    def __init__(self):
        self._next_id = 108244700  # Realistic starting ID for new entities
        self._company_has_bank_account = False
        self._call_log: list[dict] = []  # Track every API call for scoring

        # Pre-seeded data matching real competition accounts
        self._store: dict[str, list[dict]] = {
            "employee": [self._seed_admin_employee()],
            "customer": [],
            "product": self._seed_products(),
            "order": [],
            "invoice": [],
            "project": [],
            "department": [self._seed_department()],
            "travelExpense": [],
            "ledger/voucher": [],
            "ledger/account": self._seed_accounts(),
            "ledger/paymentType": self._seed_payment_types(),
            "ledger/vatType": self._seed_vat_types(),
        }

        self._company = {
            "id": self.COMPANY_ID,
            "version": 5,
            "name": "NM i AI Pool 09 5f4fb3c0",
            "organizationNumber": "280635383",
            "email": "",
            "phoneNumber": "98765432",
            "phoneNumberMobile": "",
            "faxNumber": "",
            "type": "AS",
        }

    # -- Seed data (matches real API responses) --

    def _seed_department(self) -> dict:
        return {
            "id": self.DEPT_ID,
            "version": 0,
            "name": "Avdeling",
            "departmentNumber": "",
            "departmentManager": None,
            "displayName": "Avdeling",
            "isInactive": False,
        }

    def _seed_admin_employee(self) -> dict:
        return {
            "id": self.ADMIN_EMPLOYEE_ID,
            "version": 1,
            "firstName": "Admin",
            "lastName": "NM",
            "displayName": "Admin NM",
            "employeeNumber": "",
            "email": "nmiai-pool@test.no",
            "phoneNumberMobile": "",
            "nationalIdentityNumber": "",
            "bankAccountNumber": "",
            "userType": None,
            "allowInformationRegistration": True,
            "comments": "",
            "address": None,
            "department": {"id": self.DEPT_ID},
        }

    def _seed_products(self) -> list[dict]:
        """Pre-seeded products matching real competition data."""
        return [
            {
                "id": 84376857, "version": 0,
                "name": "Service réseau", "number": "1340",
                "priceExcludingVatCurrency": 10500.0,
                "priceIncludingVatCurrency": 13125.0,
                "vatType": {"id": 3},
                "isInactive": False,
            },
            {
                "id": 84376858, "version": 0,
                "name": "Stockage cloud", "number": "9754",
                "priceExcludingVatCurrency": 11000.0,
                "priceIncludingVatCurrency": 12650.0,
                "vatType": {"id": 31},
                "isInactive": False,
            },
            {
                "id": 84376861, "version": 0,
                "name": "Session de formation", "number": "7005",
                "priceExcludingVatCurrency": 5850.0,
                "priceIncludingVatCurrency": 5850.0,
                "vatType": {"id": 6},
                "isInactive": False,
            },
        ]

    def _seed_accounts(self) -> list[dict]:
        """Chart of accounts with standard Norwegian account numbers."""
        accounts = [
            (1920, "Bank", True), (1500, "Kundefordringer", False),
            (2400, "Leverandørgjeld", False), (3000, "Salgsinntekter", False),
            (4000, "Varekostnad", False), (5000, "Lønn", False),
            (6300, "Reisekostnader", False), (7700, "Avskrivninger", False),
        ]
        return [
            {
                "id": 353300900 + i, "version": 0,
                "number": num, "name": name,
                "isBankAccount": is_bank, "bankAccountNumber": "",
            }
            for i, (num, name, is_bank) in enumerate(accounts)
        ]

    def _seed_payment_types(self) -> list[dict]:
        return [
            {"id": 1, "description": "Kontant"},
            {"id": 2, "description": "Bankinnskudd"},
            {"id": 3, "description": "Kort"},
        ]

    def _seed_vat_types(self) -> list[dict]:
        """Real Norwegian VAT types with correct IDs from production."""
        return [
            {"id": 3, "number": "3", "name": "Utgående mva høy sats 25%", "percentage": 25.0},
            {"id": 31, "number": "31", "name": "Utgående mva middels sats 15%", "percentage": 15.0},
            {"id": 5, "number": "5", "name": "Utgående mva lav sats 12%", "percentage": 12.0},
            {"id": 6, "number": "6", "name": "Fritatt for mva 0%", "percentage": 0.0},
            {"id": 52, "number": "52", "name": "Inngående mva høy sats 25%", "percentage": 25.0},
            {"id": 53, "number": "53", "name": "Inngående mva middels sats 15%", "percentage": 15.0},
            {"id": 51, "number": "51", "name": "Inngående mva lav sats 12%", "percentage": 12.0},
        ]

    # -- Helpers --

    def _log_call(self, method: str, endpoint: str, status: int) -> None:
        self._call_log.append({"method": method, "endpoint": endpoint, "status": status})

    def get_call_stats(self) -> dict:
        """Return summary: total calls, errors, breakdown by method."""
        total = len(self._call_log)
        errors = sum(1 for c in self._call_log if c["status"] >= 400)
        by_method = {}
        for c in self._call_log:
            by_method[c["method"]] = by_method.get(c["method"], 0) + 1
        return {"total": total, "errors_4xx": errors, "by_method": by_method, "calls": self._call_log}

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _get_collection(self, endpoint: str) -> str:
        """Map endpoint path to store key."""
        ep = endpoint.strip("/").split("/")
        if len(ep) >= 2 and ep[0] == "ledger":
            return f"ledger/{ep[1]}"
        return ep[0]

    def _match_filter(self, item: dict, key: str, val: str) -> bool:
        """Check if an item matches a filter parameter."""
        item_val = item.get(key)
        if item_val is None:
            return False
        return str(item_val).lower() == str(val).lower()

    # -- API methods --

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        result = self._get_impl(endpoint, params)
        status = result.get("status", 200)
        self._log_call("GET", endpoint, status)
        return result

    def _get_impl(self, endpoint: str, params: dict | None = None) -> dict:
        ep = endpoint.strip("/")

        # Special endpoints
        if ">whoAmI" in ep:
            return {
                "value": {
                    "employeeId": self.ADMIN_EMPLOYEE_ID,
                    "companyId": self.COMPANY_ID,
                    "company": {"id": self.COMPANY_ID},
                    "employee": {"id": self.ADMIN_EMPLOYEE_ID},
                }
            }

        # GET /company (no ID) → 400 Method Not Allowed (real behavior)
        if ep == "company":
            return _error_response(400, 4000, "HTTP 405 Method Not Allowed")

        # GET /company/{id} → OK
        if re.match(r"company/\d+$", ep):
            return {"value": dict(self._company)}

        # GET /company/settings → 422 (real behavior)
        if ep.startswith("company/") and not ep[-1].isdigit():
            return _error_response(422, 21000,
                                   f"Wrong data format! Expected number. For input string: \"{ep.split('/')[-1]}\"")

        col = self._get_collection(ep)
        items = self._store.get(col, [])

        # Apply filters
        if params:
            filtered = items
            for key, val in params.items():
                if key in ("fields", "count", "from", "sorting"):
                    continue
                filtered = [i for i in filtered if self._match_filter(i, key, val)]
            items = filtered

        count = int(params.get("count", 100)) if params else 100
        return {"fullResultSize": len(items), "values": items[:count]}

    def post(self, endpoint: str, json_body: dict, params: dict | None = None) -> dict:
        result = self._post_impl(endpoint, json_body, params)
        status = result.get("status", 201)
        self._log_call("POST", endpoint, status)
        return result

    def _post_impl(self, endpoint: str, json_body: dict, params: dict | None = None) -> dict:
        col = self._get_collection(endpoint)

        # -- Validation rules (match real Tripletex behavior) --

        # Product: reject duplicate name or number
        if col == "product":
            for existing in self._store.get("product", []):
                if json_body.get("name") and existing.get("name") == json_body["name"]:
                    return _error_response(422, 18000, "Validering feilet.", [{
                        "field": "name",
                        "message": f"Produktnavnet \"{json_body['name']}\" er allerede registrert.",
                        "path": None, "rootId": None,
                    }])
                if json_body.get("number") and existing.get("number") == json_body["number"]:
                    return _error_response(422, 18000, "Validering feilet.", [{
                        "field": "number",
                        "message": f"Produktnummeret {json_body['number']} er i bruk.",
                        "path": None, "rootId": None,
                    }])

        # Order: validate order lines
        if col == "order" and "orderLines" in json_body:
            for i, line in enumerate(json_body["orderLines"]):
                # vatType must be {"id": N}, not a string
                vat = line.get("vatType")
                if vat is not None and not isinstance(vat, dict):
                    return _error_response(422, 16000, "Request mapping failed", [{
                        "field": "orderLines.vatType",
                        "message": "Verdien er ikke av korrekt type for dette feltet.",
                        "path": f"orderLines[{i}].vatType",
                        "rootId": None,
                    }])
                # "quantity" is not a valid field — must use "count"
                if "quantity" in line:
                    return _error_response(422, 16000, "Request mapping failed", [{
                        "field": "quantity",
                        "message": "Feltet eksisterer ikke i objektet.",
                        "path": None, "rootId": None,
                    }])

        # Invoice: require company bank account
        if col == "invoice" and not self._company_has_bank_account:
            return _error_response(422, 18000, "Validering feilet.", [{
                "field": None,
                "message": "Faktura kan ikke opprettes før selskapet har registrert et bankkontonummer.",
                "path": None, "rootId": None,
            }])

        # Employee: validate required fields
        if col == "employee":
            missing = []
            for field in ("firstName", "lastName"):
                if not json_body.get(field):
                    missing.append(field)
            if missing:
                return _error_response(422, 18000, "Validering feilet.", [
                    {"field": f, "message": f"{f} er påkrevd.", "path": None, "rootId": None}
                    for f in missing
                ])

        # -- Create entity --
        entity = dict(json_body)
        entity["id"] = self._new_id()
        entity["version"] = 0

        # Invoice with payment params
        if col == "invoice" and params:
            if params.get("paidAmount"):
                entity["paidAmount"] = params["paidAmount"]
            if params.get("paymentTypeId"):
                entity["paymentTypeId"] = params["paymentTypeId"]

        self._store.setdefault(col, []).append(entity)
        logger.info("FAKE POST /%s -> id=%d", col, entity["id"])
        return {"value": entity}

    def put(self, endpoint: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        result = self._put_impl(endpoint, json_body, params)
        status = result.get("status", 200)
        self._log_call("PUT", endpoint, status)
        return result

    def _put_impl(self, endpoint: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        ep = endpoint.strip("/")

        # PUT /company → update company (NOTE: real Company schema has NO bankAccountNumber)
        if ep == "company":
            if json_body:
                self._company.update({
                    k: v for k, v in json_body.items()
                    if k not in ("id",) and v is not None
                })
                if json_body.get("version") is not None:
                    self._company["version"] = json_body["version"] + 1
                # NOTE: bankAccountNumber on Company is silently ignored in real API
                # but we keep this for backwards compat in tests
                if json_body.get("bankAccountNumber"):
                    self._company_has_bank_account = True
                    logger.info("FAKE PUT /company -> bank account set (fallback)")
            logger.info("FAKE PUT /company -> updated (version=%d)", self._company.get("version", 0))
            return {"value": dict(self._company)}

        # PUT /invoice/{id}/:createCreditNote
        if "/:createCreditNote" in ep:
            inv_id = int(ep.split("/")[1])
            # Verify invoice exists
            found = any(i["id"] == inv_id for i in self._store.get("invoice", []))
            if not found:
                return _error_response(404, 20000, f"Invoice {inv_id} not found")
            credit_note = {
                "id": self._new_id(),
                "type": "credit_note",
                "originalInvoiceId": inv_id,
                "date": params.get("date") if params else None,
                "comment": params.get("comment") if params else None,
            }
            self._store.setdefault("invoice", []).append(credit_note)
            logger.info("FAKE PUT %s -> credit note id=%d", ep, credit_note["id"])
            return {"value": credit_note}

        # PUT /invoice/{id}/:payment
        if "/:payment" in ep:
            inv_id = int(ep.split("/")[1])
            for inv in self._store.get("invoice", []):
                if inv["id"] == inv_id:
                    inv["paidAmount"] = params.get("paidAmount") if params else 0
                    logger.info("FAKE PUT %s -> payment registered", ep)
                    return {"value": inv}
            return _error_response(404, 20000, f"Invoice {inv_id} not found")

        # Regular PUT — update entity
        col = self._get_collection(ep)
        parts = ep.split("/")
        try:
            entity_id = int(parts[-1])
        except ValueError:
            return _error_response(422, 21000, f"Wrong data format! Expected number.")

        for i, item in enumerate(self._store.get(col, [])):
            if item["id"] == entity_id:
                if json_body:
                    self._store[col][i] = {**item, **json_body}
                    self._store[col][i]["version"] = item.get("version", 0) + 1
                    # If updating a ledger account with bankAccountNumber, enable invoicing
                    if col == "ledger/account" and json_body.get("bankAccountNumber"):
                        self._company_has_bank_account = True
                        logger.info("FAKE PUT /%s/%d -> bank account number set, invoicing enabled", col, entity_id)
                logger.info("FAKE PUT /%s/%d -> updated", col, entity_id)
                return {"value": self._store[col][i]}

        return _error_response(404, 20000, f"Entity {entity_id} not found in {col}")

    def delete(self, endpoint: str) -> dict:
        result = self._delete_impl(endpoint)
        status = result.get("status", 200)
        self._log_call("DELETE", endpoint, status)
        return result

    def _delete_impl(self, endpoint: str) -> dict:
        ep = endpoint.strip("/")
        col = self._get_collection(ep)
        try:
            entity_id = int(ep.split("/")[-1])
        except ValueError:
            return _error_response(422, 21000, "Wrong data format! Expected number.")

        before = len(self._store.get(col, []))
        self._store[col] = [i for i in self._store.get(col, []) if i["id"] != entity_id]
        after = len(self._store.get(col, []))

        if after == before:
            return _error_response(404, 20000, f"Entity {entity_id} not found")

        logger.info("FAKE DELETE /%s/%d", col, entity_id)
        return {"status": 200}


# ---------------------------------------------------------------------------
# Test cases — the real invoice task from the competition run
# ---------------------------------------------------------------------------

TEST_CASES = [
    # -- Simple entity creation --
    {
        "name": "Create employee (Norwegian, simple)",
        "prompt": "Opprett en ansatt med navn Lisa Fjord og e-post lisa@test.no",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Lisa", "lastName": "Fjord"},
            "expected": {
                "firstName": "Lisa",
                "lastName": "Fjord",
                "email": "lisa@test.no",
                "userType": "NO_ACCESS",
            }
        }
    },
    {
        "name": "Create employee (administrator)",
        "prompt": "Opprett en ansatt med navn Ola Nordmann, ola@example.org. Han skal være kontoadministrator.",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Ola", "lastName": "Nordmann"},
            "expected": {
                "firstName": "Ola",
                "lastName": "Nordmann",
                "email": "ola@example.org",
                "userType": "EXTENDED",
            }
        }
    },
    {
        "name": "Create employee (English admin)",
        "prompt": "Create an employee named John Smith with email john@test.com. He should be an administrator.",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "John", "lastName": "Smith"},
            "expected": {
                "firstName": "John",
                "lastName": "Smith",
                "email": "john@test.com",
                "userType": "EXTENDED",
            }
        }
    },
    {
        "name": "Create employee (German admin)",
        "prompt": "Erstellen Sie einen Mitarbeiter namens Hans Müller mit E-Mail hans@test.de. Er soll Kontoadministrator sein.",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Hans", "lastName": "Müller"},
            "expected": {
                "firstName": "Hans",
                "lastName": "Müller",
                "email": "hans@test.de",
                "userType": "EXTENDED",
            }
        }
    },
    {
        "name": "Create employee (Spanish admin)",
        "prompt": "Cree un empleado llamado María García con correo maria@test.es. Ella debe ser administradora de cuenta.",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "María", "lastName": "García"},
            "expected": {
                "firstName": "María",
                "lastName": "García",
                "email": "maria@test.es",
                "userType": "EXTENDED",
            }
        }
    },
    {
        "name": "Create employee (French admin)",
        "prompt": "Créez un employé nommé Pierre Dubois avec l'email pierre@test.fr. Il doit être administrateur du compte.",
        "checks": {
            "collection": "employee",
            "find": {"firstName": "Pierre", "lastName": "Dubois"},
            "expected": {
                "firstName": "Pierre",
                "lastName": "Dubois",
                "email": "pierre@test.fr",
                "userType": "EXTENDED",
            }
        }
    },
    {
        "name": "Create customer",
        "prompt": "Opprett en kunde med navn Acme AS og e-post post@acme.no",
        "checks": {
            "collection": "customer",
            "find": {"name": "Acme AS"},
            "expected": {
                "name": "Acme AS",
                "email": "post@acme.no",
            }
        }
    },
    {
        "name": "Create department",
        "prompt": "Opprett en avdeling med navn Salg",
        "checks": {
            "collection": "department",
            "find": {"name": "Salg"},
            "expected": {
                "name": "Salg",
            }
        }
    },
    # -- The actual failing task from the competition run --
    {
        "name": "Create invoice with VAT types (French — real competition task)",
        "prompt": "Créez une facture pour le client Colline SARL (nº org. 942447647) avec trois lignes de produit : Service réseau (1340) à 10500 NOK avec 25 % TVA, Stockage cloud (9754) à 11000 NOK avec 15 % TVA (alimentaire), et Session de formation (7005) à 5850 NOK avec 0 % TVA (exonéré).",
        "checks": {
            "collection": "invoice",
            "find": {},
            "expected": {
                "invoiceDate": "2026-03-20",
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(test_case: dict) -> dict:
    """Run a single test case through parser + handler."""
    name = test_case["name"]
    prompt = test_case["prompt"]
    checks = test_case["checks"]

    logger.info("=" * 60)
    logger.info("TEST: %s", name)
    logger.info("Prompt: %s", prompt[:100])

    fake_client = FakeTripletexClient()

    from agent.parser import parse_task
    from agent.handlers import prefetch_context, execute_task

    task = parse_task(prompt)
    logger.info("Parsed: %s", json.dumps(task, ensure_ascii=False)[:300])

    context = prefetch_context(fake_client)

    try:
        result = execute_task(task, fake_client, context)
        logger.info("Handler result: %s", result)
    except Exception as e:
        logger.error("Handler FAILED: %s", e, exc_info=True)
        return {"name": name, "passed": False, "error": str(e), "score": 0}

    if result is None:
        return {"name": name, "passed": False, "error": "Handler returned None (unknown task type)", "score": 0}

    # Verify results
    collection = checks["collection"]
    find_criteria = checks["find"]
    expected = checks["expected"]

    items = fake_client._store.get(collection, [])

    match = None
    if find_criteria:
        for item in items:
            if all(str(item.get(k, "")).lower() == str(v).lower() for k, v in find_criteria.items()):
                match = item
                break
    else:
        # No criteria — find any non-seed item (id > 100000000)
        for item in reversed(items):
            if item.get("id", 0) > 100000000:
                match = item
                break

    if not match:
        logger.error("FAIL: Entity not found in '%s'", collection)
        logger.error("Store: %s", json.dumps(items, ensure_ascii=False, default=str)[:500])
        return {"name": name, "passed": False, "error": "Entity not found in store", "score": 0}

    total_checks = len(expected)
    passed_checks = 0
    failures = []

    for field, expected_val in expected.items():
        actual_val = match.get(field)
        if str(actual_val).lower() == str(expected_val).lower():
            passed_checks += 1
        else:
            failures.append(f"{field}: expected '{expected_val}', got '{actual_val}'")

    score = passed_checks / total_checks if total_checks > 0 else 0
    passed = score == 1.0

    if passed:
        logger.info("PASS: All %d checks passed", total_checks)
    else:
        logger.warning("PARTIAL: %d/%d checks passed", passed_checks, total_checks)
        for f in failures:
            logger.warning("  FAIL: %s", f)

    stats = fake_client.get_call_stats()

    return {
        "name": name,
        "passed": passed,
        "score": score,
        "checks": f"{passed_checks}/{total_checks}",
        "failures": failures,
        "task_type": task.get("task_type"),
        "api_calls": stats["total"],
        "errors_4xx": stats["errors_4xx"],
        "by_method": stats["by_method"],
    }


def main():
    """Run all test cases."""
    print("\n" + "=" * 60)
    print("TRIPLETEX REALISTIC SIMULATOR")
    print("Based on actual competition run data")
    print("=" * 60 + "\n")

    results = []
    for tc in TEST_CASES:
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

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        detail = r.get("checks", "")
        task_type = r.get("task_type", "?")
        failures = r.get("failures", [])
        error = r.get("error", "")
        calls = r.get("api_calls", "?")
        errs = r.get("errors_4xx", "?")
        print(f"  [{status}] {r['name']} (parsed as: {task_type}) {detail}  |  API: {calls} calls, {errs} errors")
        if failures:
            for f in failures:
                print(f"         -> {f}")
        if error and not failures:
            print(f"         -> {error}")
        if isinstance(calls, int):
            total_calls += calls
        if isinstance(errs, int):
            total_errors += errs

    print(f"\n  Total: {passed}/{total} passed, {failed} failed")
    print(f"  API calls: {total_calls} total, {total_errors} errors (4xx)")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
