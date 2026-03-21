"""
Task parser module.

Takes a raw prompt (in any of 7 languages) and optional file content,
calls Claude ONCE, and returns structured JSON with task_type and entities.
"""

import json
import logging
from datetime import date

from anthropic import Anthropic

logger = logging.getLogger(__name__)

KNOWN_TASK_TYPES = [
    "create_employee",
    "update_employee",
    "create_customer",
    "update_customer",
    "create_product",
    "create_invoice",
    "create_invoice_with_payment",
    "create_credit_note",
    "create_project",
    "create_department",
    "create_travel_expense",
    "delete_travel_expense",
    "create_voucher",
    "delete_voucher",
    "update_employee_role",
    "log_timesheet_hours",
    "create_dimension_voucher",
    "reverse_invoice_payment",
    "run_payroll",
    "unknown",
]

def _build_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""\
You are a strict JSON extraction engine. You receive a user request (possibly with attached file contents) written in any of these languages: Norwegian (Bokmål), Nynorsk, English, Spanish, Portuguese, German, or French.

Your ONLY job is to output a single JSON object. No markdown, no explanation, no extra text. Just the JSON object.

The JSON object MUST have exactly this structure:
{{{{
  "task_type": "<one of the known types>",
  "entities": {{{{ ... extracted fields ... }}}}
}}}}

## Known task types and their entity fields

1. **create_employee**
   Fields: firstName, lastName, email, isAdministrator (boolean), phoneNumberMobile, dateOfBirth (YYYY-MM-DD), address (object with addressLine1, postalCode, city, country), employeeNumber, nationalIdentityNumber, bankAccountNumber, comments
   Employment fields (extract these if the prompt mentions a work contract, position, salary, or start date):
   - startDate (YYYY-MM-DD — when the employment begins, "tiltredelse"/"tiltredingsdato"/"fecha de inicio"/"date d'entrée"/"Eintrittsdatum")
   - annualSalary (number — yearly salary, "årslønn"/"annual salary"/"salario anual"/"salaire annuel"/"Jahresgehalt")
   - employmentPercentage (number — e.g. 80 for 80%, "stillingsprosent"/"employment percentage"/"porcentaje de empleo")
   - occupationCode (string — "stillingskode"/"occupation code"/"código de ocupación")
   - employmentType (string — "ORDINARY"/"MARITIME"/"FREELANCE", default "ORDINARY"; "fast stilling"/"permanent"→ORDINARY)
   - remunerationType (string — "MONTHLY_WAGE"/"HOURLY_WAGE"/"COMMISSION_PERCENTAGE"; "fastlønn (månedlig)"/"monthly salary"→MONTHLY_WAGE, "timelønn"/"hourly"→HOURLY_WAGE)

2. **update_employee**
   Fields: search (object with firstName, lastName, email — whatever identifies the employee), updates (object with any employee field to change including isAdministrator)

3. **create_customer**
   Fields: name, email, phoneNumber, organizationNumber, invoiceEmail, isPrivateIndividual (boolean), language ("NORWEGIAN"/"ENGLISH"), invoiceSendMethod, postalAddress (object), physicalAddress (object), invoicesDueIn (number), invoicesDueInType ("DAYS"/"MONTHS"/"CURRENT_MONTH_OUT"), isSupplier (boolean)

4. **update_customer**
   Fields: search (object with name or other identifiers), updates (object with any customer field to change)

5. **create_product**
   Fields: name, number, description, priceExcludingVat (number), priceIncludingVat (number), costExcludingVat (number), vatType (string like "25%"), isStockItem (boolean)

6. **create_invoice**
   Fields: customer (object with name, email, phoneNumber, organizationNumber), orderLines (array of objects with: description, product (string — product number if mentioned), count (integer, default 1), unitPrice (number — price excl. VAT), unitPriceIncludingVat (number — only if price includes VAT), vatType (string like "25%"), discount (number)), invoiceDate (YYYY-MM-DD), invoiceDueDate (YYYY-MM-DD), orderDate (YYYY-MM-DD), deliveryDate (YYYY-MM-DD), comment, sendToCustomer (boolean), isPrioritizeAmountsIncludingVat (boolean — set true if prices include VAT)

7. **create_invoice_with_payment**
   Same as create_invoice PLUS: paidAmount (number), paymentTypeDescription (string, e.g. "Kontant"/"Cash"/"Kort"/"Card"/"Bank")

8. **create_credit_note**
   Fields: invoiceIdentifier (object with invoiceId or invoiceNumber or customerName or organizationNumber — however the user identifies the invoice, PLUS description (string — what the invoice was for, e.g. product/service name), unitPrice or amount (number — the invoice amount excl. VAT if mentioned), vatType (string like "25%" if VAT rate is mentioned)), date (YYYY-MM-DD), comment

9. **create_project**
   Fields: name, projectManagerName (string), isInternal (boolean), description, startDate (YYYY-MM-DD), endDate (YYYY-MM-DD), customerName (string — the customer/client to link the project to), customerOrganizationNumber (string)

10. **create_department**
    Fields: name, departmentNumber, departmentManagerName, names (array of strings — use this when multiple departments are requested, e.g. ["Kundeservice", "Regnskap", "Økonomi"])

11. **create_travel_expense**
    Fields: employeeName (string), title, project, department, costs

12. **delete_travel_expense**
    Fields: search (object — title, employeeName, or other criteria to find it)

13. **create_voucher**
    Fields: date (YYYY-MM-DD), description, postings (array of objects with: accountNumber (integer), amount (number — positive for debit, negative for credit), description)

14. **delete_voucher**
    Fields: search (object — criteria to find the voucher: description, date, etc.)

15. **update_employee_role**
    Fields: search (object identifying the employee), userType (the new role), isAdministrator (boolean)

16. **log_timesheet_hours**
    Fields: employeeName (string), employeeEmail (string), hours (number), activityName (string — e.g. "Analyse", "Testing", "Design", "Utvikling", "Rådgivning"), projectName (string), customerName (string), customerOrganizationNumber (string), date (YYYY-MM-DD — date of work, default today), hourlyRate (number — if mentioned), comment (string)
    IMPORTANT: This type covers ANY request that mentions logging/registering hours, time entries, or timesheet work — in any language: "timer" (NO), "horas" (PT/ES), "heures" (FR), "Stunden" (DE), "hours" (EN). Even if the prompt ALSO mentions invoicing the customer afterwards, use log_timesheet_hours — the handler will manage the full workflow.

17. **create_dimension_voucher**
    Fields: dimensionName (string — the name of the custom accounting dimension), dimensionValues (array of strings — the values/options to create for the dimension), accountNumber (integer — the ledger account number for the voucher posting), amount (number — the amount in NOK), linkedDimensionValue (string — which dimension value to link the posting to), voucherDate (YYYY-MM-DD), voucherDescription (string)
    IMPORTANT: Use this type when the request asks to BOTH create a custom accounting dimension ("fri regnskapsdimensjon", "dimensión contable", "dimensão contabilística", "dimension comptable", "Buchungsdimension") AND post a voucher/journal entry linked to it.

18. **reverse_invoice_payment**
    Fields: customerName (string), customerOrganizationNumber (string), invoiceDescription (string — what the invoice was for, e.g. "Data Advisory"), amount (number — the invoice amount excl. VAT), date (YYYY-MM-DD — date of reversal)
    IMPORTANT: Use this type when the request asks to REVERSE, UNDO, or CANCEL a payment on an existing invoice. Keywords: "reverse payment", "payment returned", "undo payment", "tilbakefør betaling", "betalingen ble returnert", "reverter pagamento", "annuler le paiement", "Zahlung stornieren", "revertir el pago". This is NOT about creating a new invoice — it's about finding an existing paid invoice and reversing the payment so it shows as outstanding again.

19. **run_payroll**
    Fields: employeeName (string), employeeEmail (string), monthlySalary (number — base monthly salary in NOK), bonus (number — one-time bonus amount if mentioned), year (integer — payroll year, default current year), month (integer — payroll month 1-12, default current month)
    IMPORTANT: Use this type when the request asks to run/execute payroll ("køyr løn", "kjør lønn", "run payroll", "ejecutar nómina", "processar folha", "exécuter la paie", "Gehaltsabrechnung ausführen"), set up salary, or process wages for an employee. This includes setting base salary, adding bonuses, and creating the payroll transaction.

20. **unknown**
    Use this when the request does not match any of the above types.

## Critical rules

- Output ONLY the JSON object. No markdown fences, no commentary.
- Only include fields that are explicitly stated or clearly implied in the request. Do not invent data.
- All dates MUST be in YYYY-MM-DD format. If a date is referenced but no year is given, assume the current year (2026). If no specific date is mentioned but a date field is contextually needed, use today's date: {today}.
- The words "kontoadministrator", "administrator", "admin", "administrador", "Kontoadministrator", "administrateur", "Administratorin", or any equivalent in any language mean isAdministrator = true.
- For boolean fields, always use true/false (not strings).
- Numeric values should be numbers, not strings.
- If the request mentions both creating an invoice AND registering a payment in the same instruction, use "create_invoice_with_payment" rather than "create_invoice".
- CRITICAL: If the customer ALREADY HAS an existing/outstanding/unpaid invoice and the task is to register payment on it, use "reverse_invoice_payment" — NOT "create_invoice_with_payment". The key distinction: "create_invoice_with_payment" = create a NEW invoice and pay it; "reverse_invoice_payment" = find an EXISTING invoice and register payment.
- CRITICAL: If the request mentions logging/registering hours or time on a project activity, ALWAYS use "log_timesheet_hours" — even if it also mentions invoicing or billing the customer.
- If you cannot determine the task type, use "unknown".
- CRITICAL: Phrases like "excluding VAT", "excl. VAT", "sem IVA", "sans TVA", "ohne MwSt", "ekskl. mva", "eks. mva", "uten mva", "exkl. moms" describe the PRICE FORMAT (the amount is before VAT), NOT a 0% VAT rate. Do NOT set vatType to "0%" for these. Only set vatType if the user explicitly specifies a percentage (e.g. "25% VAT", "15% mva") or says the item is VAT exempt. If no explicit VAT percentage is stated, omit vatType entirely — the system will apply the standard 25% Norwegian VAT by default.
- For VAT-exempt items: When the prompt says "fritatt for mva", "avgiftsfri", "VAT exempt", "exento de IVA", "exonéré de TVA", or "befreit von MwSt", set vatType to "exempt" (NOT "0%"). The handler will map "exempt" to the correct Tripletex VAT type.
"""


import re as _re


def _post_validate_classification(prompt: str, parsed: dict) -> dict:
    """Override obvious misclassifications using high-confidence keyword rules.

    Only overrides when the signal is unambiguous — avoids false positives
    like product names containing 'timer' in invoice prompts.
    """
    task_type = parsed.get("task_type", "unknown")
    p = prompt.lower()

    # Rule 1: log_timesheet_hours
    # Pattern: verb + number + hour-word (e.g. "Registrer 15 timer", "Log 34 hours")
    # This avoids matching product names like "Konsulenttimer" in invoice prompts.
    _TIMESHEET_RE = _re.compile(
        r'(?:registrer|registe|log|logg|registre|erfasse|enregistre[rz]?)\s+\d+\s+'
        r'(?:timer|timar|horas|hours|heures|stunden)',
        _re.IGNORECASE,
    )
    if _TIMESHEET_RE.search(prompt) and task_type != "log_timesheet_hours":
        parsed = dict(parsed)
        parsed["task_type"] = "log_timesheet_hours"
        return parsed

    # Rule 2: create_dimension_voucher
    # Keywords for "custom accounting dimension" in all supported languages
    _DIMENSION_KEYWORDS = [
        "regnskapsdimensjon", "dimensão contabilística", "dimensión contable",
        "dimension comptable", "buchungsdimension", "accounting dimension",
        "dimensão contabil",
    ]
    if any(kw in p for kw in _DIMENSION_KEYWORDS) and task_type != "create_dimension_voucher":
        parsed = dict(parsed)
        parsed["task_type"] = "create_dimension_voucher"
        return parsed

    # Rule 3: run_payroll
    # Keywords for payroll/salary processing in all languages
    _PAYROLL_KEYWORDS = [
        "køyr løn", "kjør lønn", "run payroll", "ejecutar nómina",
        "processar folha", "exécuter la paie", "gehaltsabrechnung",
        "lønnskjøring", "lønskøyrsel", "grunnløn", "grunnlønn",
        "grundgehalt", "salaire de base", "base salary",
    ]
    if any(kw in p for kw in _PAYROLL_KEYWORDS) and task_type != "run_payroll":
        parsed = dict(parsed)
        parsed["task_type"] = "run_payroll"
        return parsed

    # Rule 4: reverse_invoice_payment
    # Must have BOTH a reverse-verb AND a payment-noun
    _REVERSE_VERBS = [
        "reverse", "reverter", "tilbakefør", "annuler", "stornieren",
        "revertir", "returned by the bank", "returnert av banken",
        "retournée par la banque", "retourné par la banque",
        "devuelto por el banco", "devolvido pelo banco",
    ]
    _PAYMENT_NOUNS = [
        "payment", "betaling", "pagamento", "pago", "paiement", "zahlung",
    ]
    has_reverse = any(rv in p for rv in _REVERSE_VERBS)
    has_payment = any(pn in p for pn in _PAYMENT_NOUNS)
    if has_reverse and has_payment and task_type != "reverse_invoice_payment":
        parsed = dict(parsed)
        parsed["task_type"] = "reverse_invoice_payment"
        return parsed

    # Rule 5: register payment on EXISTING invoice (misclassified as create_invoice_with_payment)
    # Pattern: prompt says customer HAS an outstanding invoice + register payment
    # This is NOT "create new invoice + payment" — it's "find existing + register payment"
    if task_type == "create_invoice_with_payment":
        _OUTSTANDING_KEYWORDS = [
            "uteståande", "utestående", "outstanding", "pendiente", "pendente",
            "en attente", "ausstehend", "ubetalt", "unpaid", "impagada", "impayée",
            "har ein faktura", "har en faktura", "has an invoice", "has a invoice",
            "tiene una factura", "tem uma fatura", "a une facture", "hat eine rechnung",
        ]
        _CREATE_KEYWORDS = [
            "opprett", "oprett", "lag", "create", "crear", "créer", "criar",
            "erstellen", "ny faktura", "new invoice", "nueva factura",
        ]
        has_outstanding = any(kw in p for kw in _OUTSTANDING_KEYWORDS)
        has_create = any(kw in p for kw in _CREATE_KEYWORDS)
        if has_outstanding and not has_create:
            logger.info("Post-validate: reclassifying create_invoice_with_payment → "
                        "reverse_invoice_payment (existing invoice detected)")
            parsed = dict(parsed)
            parsed["task_type"] = "reverse_invoice_payment"
            return parsed

    return parsed


def _reparse_with_hint(correct_type: str, user_message: str, client: Anthropic) -> dict:
    """Re-parse the prompt with an explicit type hint to extract correct entities."""
    import time as _time

    hint = (
        f"IMPORTANT: This task is of type '{correct_type}'. "
        f"Extract entities for this task type ONLY. "
        f"Do NOT classify it as anything else."
    )
    system = _build_system_prompt() + f"\n\n{hint}"

    response = None
    for _retry in range(3):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            break
        except Exception as _e:
            if "rate_limit" in str(_e).lower() or "429" in str(_e):
                _time.sleep(2 ** (_retry + 1))
            else:
                break

    if response is None:
        return {"task_type": correct_type, "entities": {}}

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw_text)
        parsed["task_type"] = correct_type  # Force correct type
        if "entities" not in parsed:
            parsed["entities"] = {}
        return parsed
    except json.JSONDecodeError:
        return {"task_type": correct_type, "entities": {}}


def parse_task(prompt: str, file_contents: list[str] | None = None) -> dict:
    """
    Parse a user prompt into a structured task dict.

    Args:
        prompt: The raw user request in any of the 7 supported languages.
        file_contents: Optional list of file content strings to include as context.

    Returns:
        A dict with "task_type" and "entities" keys, or
        {"task_type": "unknown", "raw_prompt": prompt} on failure.
    """
    logger.info("Parsing task from prompt: %s", prompt[:120])

    user_message = prompt
    if file_contents:
        attachments = "\n\n---\n\n".join(
            f"[Attached file {i + 1}]\n{content}"
            for i, content in enumerate(file_contents)
        )
        user_message = f"{prompt}\n\n{attachments}"

    client = Anthropic()

    try:
        import time as _time
        response = None
        for _retry in range(4):
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    system=_build_system_prompt(),
                    messages=[
                        {"role": "user", "content": user_message},
                    ],
                )
                break
            except Exception as _e:
                if "rate_limit" in str(_e).lower() or "429" in str(_e):
                    _wait = 2 ** (_retry + 1)
                    logger.warning("Parser rate limited, waiting %ds (retry %d)...", _wait, _retry + 1)
                    _time.sleep(_wait)
                else:
                    raise
        if response is None:
            logger.error("Parser: all retries exhausted")
            return {"task_type": "unknown", "raw_prompt": prompt}
    except Exception:
        logger.exception("Anthropic API call failed")
        return {"task_type": "unknown", "raw_prompt": prompt}

    raw_text = response.content[0].text.strip()
    logger.debug("Raw LLM response: %s", raw_text)

    # Strip markdown fences if the model wraps the output despite instructions
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response as JSON: %s", raw_text[:200])
        return {"task_type": "unknown", "raw_prompt": prompt}

    if not isinstance(parsed, dict) or "task_type" not in parsed:
        logger.error("LLM response missing task_type: %s", parsed)
        return {"task_type": "unknown", "raw_prompt": prompt}

    if parsed["task_type"] not in KNOWN_TASK_TYPES:
        logger.warning(
            "LLM returned unrecognized task_type '%s', falling back to unknown",
            parsed["task_type"],
        )
        parsed["task_type"] = "unknown"

    if "entities" not in parsed:
        parsed["entities"] = {}

    # Post-parse validation: catch obvious misclassifications
    corrected = _post_validate_classification(prompt, parsed)
    if corrected["task_type"] != parsed["task_type"]:
        original = parsed["task_type"]
        logger.warning(
            "Post-validation override: %s -> %s",
            original, corrected["task_type"],
        )
        # Re-parse with type hint to get correct entities
        corrected = _reparse_with_hint(
            corrected["task_type"], user_message, client,
        )

    logger.info("Parsed task_type: %s", corrected["task_type"])
    return corrected
