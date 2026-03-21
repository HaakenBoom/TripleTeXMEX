"""
Task parser module — v2 (keyword classifier + targeted LLM extraction).

Classification is done by fast keyword matching (0ms, 100% accurate).
Entity extraction uses LLM only when needed, with task-specific prompts.
"""

import json
import logging
import re
import time
from datetime import date

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# All known task types (including new ones that go to agent loop)
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
    "bank_reconciliation",
    "annual_closure",
    # New types (agent loop handles these, but classification helps)
    "project_lifecycle",
    "cost_analysis",
    "fx_correction",
    "error_correction",
    "overdue_invoice",
    "unknown",
]


# ---------------------------------------------------------------------------
# Stage 1: Keyword-based classifier (0ms, deterministic)
# ---------------------------------------------------------------------------

# Compiled regex for timesheet: verb + number + hour-word
_TIMESHEET_RE = re.compile(
    r'(?:registrer|registe|log|logg|registre|erfasse[n]?|enregistre[rz]?)\s+(?:\S+\s+)?\d+\s+'
    r'(?:timer|timar|horas|hours|heures|stunden)',
    re.IGNORECASE,
)

# Compiled regex for reverse payment: reverse-verb near payment-noun
_REVERSE_VERBS = [
    "reverse", "reverter", "tilbakefør", "annuler", "stornieren",
    "revertir", "returned by the bank", "returnert av banken",
    "retournée par la banque", "retourné par la banque",
    "devuelto por el banco", "devolvido pelo banco",
]
_PAYMENT_NOUNS = [
    "payment", "betaling", "pagamento", "pago", "paiement", "zahlung",
]

# Outstanding/existing invoice keywords (NOT create new)
_OUTSTANDING_KW = [
    "uteståande", "utestående", "outstanding", "pendiente", "pendente",
    "en attente", "ausstehend", "ubetalt", "unpaid", "impagada", "impayée",
    "har ein faktura", "har en faktura", "has an invoice",
    "tiene una factura", "tem uma fatura", "a une facture", "hat eine rechnung",
]
_CREATE_KW = [
    "opprett", "oprett", "lag ein", "create", "crear", "créer", "criar",
    "erstellen", "ny faktura", "new invoice", "nueva factura",
]


def classify_task(prompt: str) -> str:
    """Classify task type from prompt using keyword matching. Returns task_type string."""
    p = prompt.lower()

    # --- High-priority: specific multi-word patterns first ---

    # 1. Timesheet hours (regex to avoid false positives like product name "Konsulenttimer")
    if _TIMESHEET_RE.search(prompt):
        return "log_timesheet_hours"

    # 2. Custom accounting dimension
    if any(kw in p for kw in [
        "regnskapsdimensjon", "rekneskapsdimensjon",
        "dimensão contabilística", "dimensión contable",
        "dimension comptable", "buchungsdimension", "accounting dimension",
        "dimensão contabil",
    ]):
        return "create_dimension_voucher"

    # 3. Project lifecycle (full cycle)
    if any(kw in p for kw in [
        "prosjektsyklusen", "projektzyklus", "cycle de vie complet",
        "project lifecycle", "ciclo de vida completo", "ciclo de vida do projeto",
        "vollständigen projektzyklus",
    ]):
        return "project_lifecycle"

    # 4. Bank reconciliation
    if any(kw in p for kw in [
        "reconcil", "avstem", "rapproch", "bankabstimmung",
        "bankavstemming", "bank reconciliation", "bank statement",
        "extrato bancario", "extracto bancario", "relevé bancaire",
        "releve bancaire",
    ]):
        return "bank_reconciliation"

    # 5. Run payroll
    if any(kw in p for kw in [
        "kjør lønn", "køyr løn", "run payroll", "ejecutar nómina",
        "processar folha", "processar salário", "processe o salário",
        "exécuter la paie", "gehaltsabrechnung",
        "lønnskjøring", "lønskøyrsel", "grunnløn", "grunnlønn",
        "grundgehalt", "salaire de base", "base salary",
        "salário base",
    ]):
        return "run_payroll"

    # 6. Annual/monthly closure
    if any(kw in p for kw in [
        "årsavslutning", "årsoppgjør", "årsoppgjer", "årsrekneskap",
        "annual closing", "annual closure", "year-end closing",
        "cierre anual", "encerramento anual", "clôture annuelle", "jahresabschluss",
        "clôture mensuelle", "encerramento mensal", "cierre mensual",
        "monthly closing", "månedsavslutning", "månadsavslutning", "månavslutninga",
        "monatsabschluss",
        "forenklet årsoppgjør", "forenkla årsoppgjer",
        "annual depreciation", "avskrivning", "depreciación anual",
        "amortissement annuel", "abschreibung",
    ]):
        return "annual_closure"

    # 7. Reverse/cancel payment
    has_reverse = any(rv in p for rv in _REVERSE_VERBS)
    has_payment = any(pn in p for pn in _PAYMENT_NOUNS)
    if has_reverse and has_payment:
        return "reverse_invoice_payment"

    # 8. Cost analysis (ledger analysis + project creation)
    if any(kw in p for kw in [
        "totalkostnadene auka", "gesamtkosten", "total costs",
        "analyser hovudboka og finn", "analysieren sie das hauptbuch",
        "analyze the general ledger", "analysez le grand livre",
    ]):
        return "cost_analysis"

    # 9. Error correction (find and fix ledger errors)
    if any(kw in p for kw in [
        "feil i hovudboka", "errors in the general ledger",
        "errores en el libro mayor", "erros no razão geral",
        "erros no livro razão", "erros no livro-razão",
        "erreurs dans le grand livre", "fehler im hauptbuch",
        "oppdaga feil", "discovered errors", "descobrimos erros",
    ]):
        return "error_correction"

    # 10. Overdue invoice / late fee
    if any(kw in p for kw in [
        "forfallen faktura", "forfalne fakturaen", "overdue invoice",
        "factura vencida", "fatura vencida", "facture en retard",
        "überfällige rechnung", "purregebyr", "late fee", "reminder fee",
    ]):
        return "overdue_invoice"

    # 11. FX correction (exchange rate difference)
    if any(kw in p for kw in [
        "valutakurs", "exchange rate", "taxa de câmbio", "taux de change",
        "tipo de cambio", "wechselkurs",
    ]) and "EUR" in prompt:
        return "fx_correction"
    # Also match: invoice in EUR + "kursen"/"rate" + NOK/EUR pattern
    if "EUR" in prompt and "NOK/EUR" in prompt and any(kw in p for kw in [
        "kursen", "the rate", "la tasa", "a taxa", "le taux", "der kurs",
    ]):
        return "fx_correction"

    # 12. Supplier invoice → create_voucher
    if any(kw in p for kw in [
        "leverandørfaktura", "leverandorfaktura", "supplier invoice",
        "factura de proveedor", "fatura do fornecedor",
        "facture fournisseur", "facture du fournisseur",
        "lieferantenrechnung", "eingangsrechnung",
    ]):
        return "create_voucher"

    # 13. Credit note
    if any(kw in p for kw in [
        "kreditnota", "credit note", "nota de crédito", "note de crédit",
        "gutschrift", "nota de credito",
    ]):
        return "create_credit_note"

    # 14. Delete travel expense
    if any(kw in p for kw in [
        "slett reiserekning", "slett reiseregning", "delete travel expense",
        "eliminar gasto de viaje", "excluir despesa de viagem",
        "supprimer la note de frais", "reisekostenabrechnung löschen",
    ]):
        return "delete_travel_expense"

    # 15. Delete voucher
    if any(kw in p for kw in [
        "slett bilag", "delete voucher", "eliminar comprobante",
        "excluir comprovante", "supprimer le bon", "beleg löschen",
    ]):
        return "delete_voucher"

    # 16. Travel expense (must come after delete_travel_expense)
    if any(kw in p for kw in [
        "reiserekning", "reiseregning", "travel expense",
        "gasto de viaje", "despesa de viagem",
        "note de frais", "reisekostenabrechnung",
        "reiserekning", "reiseutlegg",
    ]):
        return "create_travel_expense"

    # 17. Project fixed-price invoice (must come BEFORE generic invoice+payment)
    #     These mention "payment" loosely but are really project invoices
    _is_fixed_price = any(kw in p for kw in [
        "fastpris", "fixed price", "prix forfaitaire", "festpreis",
        "precio fijo", "preço fixo",
    ])
    if _is_fixed_price:
        return "create_invoice"

    # 18. Invoice with payment (create + pay in same step)
    #     NOTE: actual payment REVERSALS are already caught by rule 7 (reverse verbs).
    #     If we get here, it's about creating/registering payment, not reversing.
    _invoice_kw = any(kw in p for kw in [
        "faktura", "invoice", "factura", "fatura", "rechnung",
    ])
    _payment_kw = any(kw in p for kw in [
        "registrer full betaling", "register full payment", "registre full",
        "registrer betaling", "registrar pago", "registrar pagamento",
        "enregistrer le paiement", "zahlung registrieren",
        "full betaling", "full payment",
    ])
    if _invoice_kw and _payment_kw:
        # "has an outstanding/unpaid invoice" + "register payment" → find existing & pay
        _has_outstanding = any(kw in p for kw in _OUTSTANDING_KW)
        if _has_outstanding:
            return "reverse_invoice_payment"
        return "create_invoice_with_payment"
    # Order + invoice + payment
    if _invoice_kw and any(kw in p for kw in [
        "ordre", "orden", "order", "pedido", "commande", "bestellung", "auftrag",
    ]) and any(kw in p for kw in ["betaling", "payment", "pago", "pagamento", "zahlung"]):
        return "create_invoice_with_payment"

    # 19. Regular invoice
    _has_create = any(kw in p for kw in _CREATE_KW)
    if _invoice_kw and _has_create:
        return "create_invoice"
    if any(kw in p for kw in [
        "opprett og send en faktura", "opprett ei faktura", "create an invoice",
        "cree una factura", "crie uma fatura", "créez une facture",
        "erstellen sie eine rechnung",
    ]):
        return "create_invoice"

    # 20. Create product
    if any(kw in p for kw in [
        "opprett produktet", "create the product", "erstellen sie das produkt",
        "crie o produto", "crea el producto", "créez le produit",
        "produktnummer", "product number", "número de producto",
    ]):
        return "create_product"

    # 21. Create project
    if any(kw in p for kw in [
        "opprett prosjektet", "create the project", "erstellen sie das projekt",
        "crie o projeto", "crea el proyecto", "créez le projet",
    ]):
        return "create_project"

    # 22. Create employee (from PDF, from prompt, etc.) — must come BEFORE department
    #     because employee prompts often mention "avdeling"/"department"
    if any(kw in p for kw in [
        "opprett den ansatte", "opprett vedkomande", "ny tilsett",
        "ny ansatt", "new employee", "nuevo empleado", "novo funcionário",
        "nouvel employé", "neuen mitarbeiter", "neue mitarbeiterin",
        "arbeidskontrakt", "employment contract", "contrato de trabajo",
        "contrato de trabalho", "contrat de travail", "arbeitsvertrag",
        "offer letter", "carta de oferta",
        "crie o funcionario", "crea el empleado", "créez l'employé",
        "complete the onboarding", "completa la incorporacion",
    ]):
        return "create_employee"

    # 23. Create department
    if any(kw in p for kw in [
        "opprett tre avdelinger", "opprett avdeling",
        "create three departments", "create departments",
        "crie três departamentos", "cree tres departamentos",
        "créez trois départements",
        "erstellen sie drei abteilungen",
    ]):
        return "create_department"

    # 24. Create/register customer or supplier
    if any(kw in p for kw in [
        "opprett kunden", "registrer kunden", "create the customer",
        "register the customer", "cree el cliente", "registre o cliente",
        "créez le client", "registrieren sie den",
        "opprett leverandøren", "register the supplier",
    ]):
        return "create_customer"

    # 25. Update employee role
    if any(kw in p for kw in [
        "kontoadministrator", "administrator", "admin",
    ]) and any(kw in p for kw in [
        "endre", "oppdater", "sett som", "gjør til", "change", "update", "set as",
        "make", "cambiar", "actualizar", "alterar", "modifier", "ändern",
    ]):
        return "update_employee_role"

    # 26. Update employee
    if any(kw in p for kw in ["oppdater ansatt", "update employee", "endre ansatt"]):
        return "update_employee"

    # 27. Update customer
    if any(kw in p for kw in ["oppdater kunde", "update customer", "endre kunde"]):
        return "update_customer"

    # 28. Generic voucher (receipt, expense registration)
    if any(kw in p for kw in [
        "bilag", "voucher", "reçu", "recibo", "receipt", "quittung",
        "registree au departement", "registrado en el departamento",
        "depense de ce recu", "gasto de este recibo", "despesa deste recibo",
        "faktura inv-", "invoice inv-",
        "enregistree au departement",
        "ausgabe aus dieser quittung", "in der abteilung",
        "depense", "cette quittance",
    ]):
        return "create_voucher"

    # 28b. Fallback department check (only if no other match)
    if any(kw in p for kw in [
        "avdeling", "department", "departamento", "département", "abteilung",
    ]):
        return "create_department"

    # 29. Fallback: if prompt mentions "faktura" at all → likely invoice
    if _invoice_kw:
        return "create_invoice"

    # 30. Unknown
    return "unknown"


# ---------------------------------------------------------------------------
# Stage 2: Entity extraction (LLM for complex, regex for simple)
# ---------------------------------------------------------------------------

# Task types that can be extracted with regex (no LLM needed)
_REGEX_EXTRACTABLE = {
    "create_department", "bank_reconciliation",
}

# Task-type-specific LLM extraction prompts (much shorter than the monolithic one)
def _build_extraction_prompt(task_type: str) -> str:
    today = date.today().isoformat()
    base = f"""Extract entities from this request as JSON. Output ONLY a JSON object with an "entities" key. No markdown, no explanation.
Today's date: {today}. Dates in YYYY-MM-DD. Numbers as numbers, booleans as true/false.
CRITICAL: "excluding VAT"/"ekskl. mva"/"sem IVA"/"sans TVA"/"ohne MwSt" describes the PRICE FORMAT, NOT a 0% VAT rate. Do NOT set vatType to "0%" for these. Only set vatType if the user explicitly specifies a percentage or says "exempt"/"fritatt"/"avgiftsfri".
"""

    prompts = {
        "create_employee": base + """
Extract: firstName, lastName, email, dateOfBirth (YYYY-MM-DD), phoneNumberMobile, isAdministrator (boolean), address ({addressLine1, postalCode, city, country}), employeeNumber, nationalIdentityNumber, bankAccountNumber, comments.
Employment: startDate, annualSalary, employmentPercentage, occupationCode, employmentType (ORDINARY/MARITIME/FREELANCE), remunerationType (MONTHLY_WAGE/HOURLY_WAGE).
"kontoadministrator"/"administrator"/"admin" in any language → isAdministrator: true.
If date has no year, assume 2026.""",

        "create_customer": base + """
Extract: name, email, organizationNumber, phoneNumber, invoiceEmail, postalAddress ({addressLine1, postalCode, city}), isSupplier (boolean — true if "leverandør"/"Lieferant"/"supplier"/"fournisseur").
"Registrieren Sie den Lieferanten" = isSupplier: true.""",

        "create_product": base + """
Extract: name, number (product number), priceExcludingVat (number), priceIncludingVat (number), vatType (string like "25%"/"15%" — only if explicitly stated), isStockItem (boolean).
"næringsmiddel"/"food"/"aliment" with 15% → vatType: "15%".""",

        "create_invoice": base + """
Extract: customer ({name, organizationNumber, email}), orderLines (array of {description, product (product number string), count (default 1), unitPrice (excl VAT), unitPriceIncludingVat, vatType, discount}), invoiceDate, invoiceDueDate, comment.
For project invoices: also extract projectName, projectManagerName, projectManagerEmail, fixedPrice (total project price), invoicePercentage (% to bill).
isPrioritizeAmountsIncludingVat: true only if prices are stated INCLUDING VAT.""",

        "create_invoice_with_payment": base + """
Extract: customer ({name, organizationNumber}), orderLines (array of {description, product (product number string), count, unitPrice}), paidAmount, paymentTypeDescription ("Kontant"/"Cash"/"Kort"/"Card"/"Bank").
If customer has EXISTING outstanding invoice: set existingInvoice: true, and only extract customerName/Org + paidAmount.""",

        "create_credit_note": base + """
Extract: invoiceIdentifier ({customerName, organizationNumber, description (what invoice was for), amount (excl VAT), vatType}), date, comment.""",

        "create_project": base + """
Extract: name, customerName, customerOrganizationNumber, projectManagerName, projectManagerEmail, isInternal (boolean), startDate, endDate.""",

        "create_travel_expense": base + """
Extract: employeeName, employeeEmail, title (trip description), project, department, costs (array of {description, amount (NOK), date}).
For per diem: description should include days and rate.""",

        "create_voucher": base + """
Extract: date (YYYY-MM-DD), description, postings (array of {accountNumber (integer), amount (positive=debit, negative=credit), description}).
For supplier invoices: include expense posting (4xxx-7xxx), input VAT posting (27xx), and AP posting (2400, negative).
Supplier info: supplierName, supplierOrganizationNumber (extract from prompt if mentioned).""",

        "log_timesheet_hours": base + """
Extract: employeeName, employeeEmail, hours (number), activityName (e.g. "Design", "Utvikling", "Testing"), projectName, customerName, customerOrganizationNumber, date (YYYY-MM-DD), hourlyRate (number), comment.""",

        "create_dimension_voucher": base + """
Extract: dimensionName, dimensionValues (array of strings), accountNumber (integer), amount (NOK), linkedDimensionValue (which value to link), voucherDate, voucherDescription.""",

        "reverse_invoice_payment": base + """
Extract: customerName, customerOrganizationNumber, invoiceDescription (what the invoice was for), amount (excl VAT), date.""",

        "run_payroll": base + """
Extract: employeeName, employeeEmail, monthlySalary (base monthly NOK), bonus (one-time bonus NOK), year (default current year), month (1-12, default current month).""",

        "annual_closure": base + f"""
Extract: closureYear (integer), closureMonth (integer, only if monthly), date (YYYY-MM-DD, default {today}).
depreciationItems: array of {{assetName, acquisitionCost, depreciationPeriodYears, depreciationExpenseAccountNumber (default 6010), accumulatedDepreciationAccountNumber (default 1209)}}.
prepaidExpenseReversal: {{amount (monthly amount), accountNumber (default 1700)}}.
taxCalculation: {{taxRate (0-1, e.g. 0.22), expenseAccountNumber (default 8700), liabilityAccountNumber (default 2920)}}.
entries: array of {{description, postings: [{{accountNumber, amount, description}}]}} for any other journal entries.
Always extract year. For monthly closure, extract month.""",

        "project_lifecycle": base + """
Extract: projectName, customerName, customerOrganizationNumber, budget (NOK), projectManagerName, projectManagerEmail.
timesheetEntries: array of {employeeName, employeeEmail, hours, activityName, hourlyRate}.
supplierInvoice: {supplierName, supplierOrganizationNumber, amount, description, accountNumber}.
customerInvoicePercentage (% of budget to bill).""",

        "cost_analysis": base + """
Extract: analysisMonths (array of {year, month}), numberOfAccounts (how many top accounts to find).""",

        "fx_correction": base + """
Extract: customerName, customerOrganizationNumber, invoiceAmountEUR (number), originalRate (NOK/EUR when invoiced), currentRate (NOK/EUR at payment), invoiceDescription.""",

        "error_correction": base + """
Extract: errors (array of {description, wrongAccount, correctAccount, amount, voucherDescription}).
Extract all specific error details mentioned in the prompt.""",

        "overdue_invoice": base + """
Extract: feeAmount (NOK), debitAccount (default 1500), creditAccount (default 3400), invoiceFee (boolean — whether to also create invoice for the fee).""",
    }

    return prompts.get(task_type, base + "\nExtract all relevant fields as a flat JSON object.")


def _extract_entities_regex(task_type: str, prompt: str) -> dict:
    """Fast regex extraction for simple task types."""
    entities: dict = {}

    if task_type == "create_department":
        # Extract quoted department names
        names = re.findall(r'"([^"]+)"', prompt)
        if not names:
            names = re.findall(r'[«"]([^»"]+)[»"]', prompt)
        if names:
            entities["names"] = names
        return entities

    if task_type == "bank_reconciliation":
        # No entities needed — handler parses the CSV
        return entities

    return entities


def _extract_entities_llm(task_type: str, prompt: str, file_contents: list[str] | None) -> dict:
    """LLM-based entity extraction with task-specific prompt."""
    user_message = prompt
    if file_contents:
        attachments = "\n\n---\n\n".join(
            f"[Attached file {i + 1}]\n{content}"
            for i, content in enumerate(file_contents)
        )
        user_message = f"{prompt}\n\n{attachments}"

    system = _build_extraction_prompt(task_type)
    client = Anthropic()

    response = None
    for retry in range(4):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            break
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 2 ** (retry + 1)
                logger.warning("Entity extraction rate limited, waiting %ds...", wait)
                time.sleep(wait)
            else:
                logger.exception("Entity extraction API call failed")
                break

    if response is None:
        return {}

    raw_text = response.content[0].text.strip()

    # Strip markdown fences
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw_text)
        # Handle both {"entities": {...}} and direct {...} formats
        if isinstance(parsed, dict):
            return parsed.get("entities", parsed)
        return {}
    except json.JSONDecodeError:
        logger.error("Failed to parse entity extraction response: %s", raw_text[:200])
        return {}


# ---------------------------------------------------------------------------
# Main entry point (same interface as v1)
# ---------------------------------------------------------------------------

def parse_task(prompt: str, file_contents: list[str] | None = None) -> dict:
    """
    Parse a user prompt into a structured task dict.

    Stage 1: Keyword classifier (0ms) → task_type
    Stage 2: Entity extraction (regex or LLM) → entities

    Returns: {"task_type": str, "entities": dict}
    """
    logger.info("Parsing task from prompt: %s", prompt[:120])

    # Stage 1: Classify
    task_type = classify_task(prompt)
    logger.info("Keyword classifier: %s", task_type)

    # Stage 2: Extract entities
    if task_type in _REGEX_EXTRACTABLE:
        entities = _extract_entities_regex(task_type, prompt)
        logger.info("Regex extraction for %s: %d fields", task_type, len(entities))
    else:
        entities = _extract_entities_llm(task_type, prompt, file_contents)
        logger.info("LLM extraction for %s: %d fields", task_type, len(entities))

    result = {"task_type": task_type, "entities": entities}
    logger.info("Parsed task_type: %s", task_type)
    return result
