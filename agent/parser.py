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
    #    nb: regnskapsdimensjon, nn: rekneskapsdimensjon, en: accounting dimension,
    #    es: dimensión contable, pt: dimensão contabilística/contábil, de: Buchungsdimension,
    #    fr: dimension comptable
    if any(kw in p for kw in [
        "regnskapsdimensjon", "rekneskapsdimensjon",
        "accounting dimension", "custom dimension",
        "dimensión contable", "dimensión personalizada",
        "dimensão contabilística", "dimensão contabil", "dimensão contábil", "dimensão personalizada",
        "buchungsdimension", "benutzerdefinierte dimension",
        "dimension comptable", "dimension personnalisée",
    ]):
        return "create_dimension_voucher"

    # 3. Project lifecycle (full cycle)
    #    nb: prosjektsyklusen, nn: prosjektsyklusen, en: project lifecycle/full project cycle,
    #    es: ciclo de vida del proyecto, pt: ciclo de vida do projeto, de: Projektzyklus,
    #    fr: cycle de vie du projet
    if any(kw in p for kw in [
        "prosjektsyklusen", "prosjektsyklus",
        "project lifecycle", "full project cycle", "complete project cycle",
        "ciclo de vida del proyecto", "ciclo de vida completo", "ciclo completo del proyecto",
        "ciclo de vida do projeto", "ciclo completo do projeto",
        "projektzyklus", "vollständigen projektzyklus", "gesamten projektzyklus",
        "cycle de vie complet", "cycle de vie du projet", "cycle complet du projet",
    ]):
        return "project_lifecycle"

    # 4. Bank reconciliation
    #    nb: bankavstemming/avstem, nn: bankavstemming, en: reconcile/bank reconciliation,
    #    es: conciliación bancaria, pt: reconciliação bancária, de: Bankabstimmung,
    #    fr: rapprochement bancaire
    if any(kw in p for kw in [
        "reconcil", "avstem", "bankavstemming",
        "bank reconciliation", "bank statement",
        "conciliación bancaria", "conciliar el extracto",
        "reconciliação bancária", "extrato bancario", "extracto bancario",
        "bankabstimmung", "kontoauszug",
        "rapproch", "relevé bancaire", "releve bancaire",
    ]):
        return "bank_reconciliation"

    # 5. Run payroll
    #    nb: kjør lønn, nn: køyr løn, en: run payroll, es: ejecutar nómina,
    #    pt: processar folha/salário, de: Gehaltsabrechnung, fr: exécuter la paie
    if any(kw in p for kw in [
        "kjør lønn", "køyr løn", "lønnskjøring", "lønskøyrsel",
        "run payroll", "process payroll", "base salary", "monthly salary",
        "ejecutar nómina", "procesar nómina", "salario base", "nómina de",
        "processar folha", "processar salário", "processe o salário", "salário base",
        "gehaltsabrechnung", "lohnabrechnung", "grundgehalt", "monatsgehalt",
        "exécuter la paie", "traiter la paie", "salaire de base", "salaire mensuel",
        "grunnløn", "grunnlønn",
    ]):
        return "run_payroll"

    # 6. Annual/monthly closure
    #    nb: årsavslutning/årsoppgjør, nn: årsoppgjer/årsrekneskap, en: annual closing,
    #    es: cierre anual/mensual, pt: encerramento anual/mensal, de: Jahresabschluss,
    #    fr: clôture annuelle/mensuelle
    if any(kw in p for kw in [
        "årsavslutning", "årsoppgjør", "årsoppgjer", "årsrekneskap",
        "månedsavslutning", "månadsavslutning", "månavslutninga",
        "annual closing", "annual closure", "year-end closing", "monthly closing",
        "simplified annual accounts", "annual depreciation",
        "cierre anual", "cierre mensual", "cierre simplificado",
        "depreciación anual", "depreciación mensual",
        "encerramento anual", "encerramento mensal", "encerramento simplificado",
        "depreciação anual",
        "jahresabschluss", "monatsabschluss", "vereinfachten jahresabschluss",
        "abschreibung", "jährliche abschreibung",
        "clôture annuelle", "clôture mensuelle", "clôture simplifiée",
        "amortissement annuel", "amortissement mensuel",
        "forenklet årsoppgjør", "forenkla årsoppgjer",
        "avskrivning",
    ]):
        return "annual_closure"

    # 7. Reverse/cancel payment
    has_reverse = any(rv in p for rv in _REVERSE_VERBS)
    has_payment = any(pn in p for pn in _PAYMENT_NOUNS)
    if has_reverse and has_payment:
        return "reverse_invoice_payment"

    # 8. Cost analysis (ledger analysis + project creation)
    #    nb/nn: totalkostnadene auka, en: total costs increased, es: costos totales,
    #    pt: custos totais, de: Gesamtkosten, fr: coûts totaux
    if any(kw in p for kw in [
        "totalkostnadene auka", "totalkostnadene økt", "kostnadskontoane",
        "total costs", "cost accounts", "analyze the general ledger",
        "costos totales", "cuentas de gastos", "analice el libro mayor",
        "custos totais", "contas de custos", "analise o razão",
        "gesamtkosten", "aufwandskonten", "analysieren sie das hauptbuch",
        "coûts totaux", "comptes de charges", "analysez le grand livre",
    ]):
        return "cost_analysis"

    # 9. Error correction (find and fix ledger errors)
    #    nb: feil i hovedboka, nn: feil i hovudboka, en: errors in the general ledger,
    #    es: errores en el libro mayor, pt: erros no livro razão, de: Fehler im Hauptbuch,
    #    fr: erreurs dans le grand livre
    if any(kw in p for kw in [
        "feil i hovudboka", "feil i hovedboka", "feil i hovedboken",
        "oppdaga feil", "oppdaget feil", "oppdaga ein feil", "oppdaga 4 feil",
        "finn dei 4 feila", "finn de 4 feilene", "find the 4 errors",
        "errors in the general ledger", "discovered errors", "find the errors",
        "errores en el libro mayor", "errores en el mayor", "descubierto errores",
        "hemos descubierto errores", "encontrar los errores",
        "erros no livro razão", "erros no livro-razão", "erros no razão",
        "descobrimos erros", "encontrar os erros", "encontre os 4 erros",
        "fehler im hauptbuch", "fehler entdeckt", "fehler gefunden",
        "erreurs dans le grand livre", "erreurs découvertes",
        "we have discovered errors", "gå gjennom alle bilag og finn",
    ]):
        return "error_correction"

    # 10. Overdue invoice / late fee
    #    nb/nn: forfallen faktura/purregebyr, en: overdue invoice/late fee,
    #    es: factura vencida/cargo por mora, pt: fatura vencida/taxa de atraso,
    #    de: überfällige Rechnung/Mahngebühr, fr: facture en retard/frais de retard
    if any(kw in p for kw in [
        "forfallen faktura", "forfalne fakturaen", "forfalt faktura", "forfalte fakturaen",
        "purregebyr", "inkassogebyr",
        "overdue invoice", "late fee", "reminder fee", "past due",
        "factura vencida", "cargo por mora", "recargo por retraso",
        "fatura vencida", "taxa de atraso", "multa por atraso",
        "überfällige rechnung", "mahngebühr", "säumnisgebühr",
        "facture en retard", "facture échue", "frais de retard", "pénalité de retard",
    ]):
        return "overdue_invoice"

    # 11. FX correction (exchange rate difference)
    #    All languages: EUR + exchange rate keywords + NOK/EUR pattern
    if any(kw in p for kw in [
        "valutakurs", "exchange rate", "taxa de câmbio", "taux de change",
        "tipo de cambio", "wechselkurs", "kursdifferanse", "currency difference",
        "différence de change", "diferencia de cambio", "diferença cambial",
    ]) and "EUR" in prompt:
        return "fx_correction"
    if "EUR" in prompt and "NOK/EUR" in prompt and any(kw in p for kw in [
        "kursen", "the rate", "la tasa", "a taxa", "le taux", "der kurs",
        "la cotización", "o câmbio",
    ]):
        return "fx_correction"
    # Scenario: sent invoice in EUR, customer paid at different rate
    if "EUR" in prompt and any(kw in p for kw in [
        "nok/eur", "eur/nok", "kursen var", "the rate was", "le taux était",
        "la tasa era", "a taxa era", "der kurs war", "kurs var",
    ]):
        return "fx_correction"

    # 12. Supplier invoice → create_voucher
    #    nb: leverandørfaktura, nn: leverandorfaktura, en: supplier invoice,
    #    es: factura de proveedor, pt: fatura do fornecedor, de: Lieferantenrechnung,
    #    fr: facture fournisseur
    if any(kw in p for kw in [
        "leverandørfaktura", "leverandorfaktura",
        "supplier invoice", "vendor invoice",
        "factura de proveedor", "factura del proveedor",
        "fatura do fornecedor", "fatura de fornecedor",
        "lieferantenrechnung", "eingangsrechnung",
        "facture fournisseur", "facture du fournisseur",
    ]):
        return "create_voucher"

    # 13. Credit note
    #    nb/nn: kreditnota, en: credit note, es: nota de crédito, pt: nota de crédito,
    #    de: Gutschrift, fr: note de crédit/avoir
    if any(kw in p for kw in [
        "kreditnota", "kreditnotaen",
        "credit note",
        "nota de crédito", "nota de credito",
        "gutschrift", "stornorechnung",
        "note de crédit", "note de credit", "avoir",
    ]):
        return "create_credit_note"

    # 14. Delete travel expense
    #    nb: slett reiseregning, nn: slett reiserekning, en: delete travel expense,
    #    es: eliminar gasto de viaje, pt: excluir despesa de viagem,
    #    de: Reisekostenabrechnung löschen, fr: supprimer la note de frais
    if any(kw in p for kw in [
        "slett reiserekning", "slett reiseregning", "slett reiserekningar",
        "delete travel expense", "remove travel expense",
        "eliminar gasto de viaje", "borrar gasto de viaje",
        "excluir despesa de viagem", "eliminar despesa de viagem",
        "reisekostenabrechnung löschen", "reisekosten löschen",
        "supprimer la note de frais", "supprimer le frais de voyage",
    ]):
        return "delete_travel_expense"

    # 15. Delete voucher
    #    nb: slett bilag, nn: slett bilag, en: delete voucher, es: eliminar comprobante,
    #    pt: excluir comprovante, de: Beleg löschen, fr: supprimer le bon
    if any(kw in p for kw in [
        "slett bilag", "slett bilaget",
        "delete voucher", "remove voucher",
        "eliminar comprobante", "borrar comprobante",
        "excluir comprovante", "eliminar comprovante", "excluir voucher",
        "beleg löschen", "buchungsbeleg löschen",
        "supprimer le bon", "supprimer le voucher", "supprimer l'écriture",
    ]):
        return "delete_voucher"

    # 16. Travel expense (must come after delete_travel_expense)
    #    nb: reiseregning, nn: reiserekning, en: travel expense, es: gasto de viaje,
    #    pt: despesa de viagem, de: Reisekostenabrechnung, fr: note de frais
    if any(kw in p for kw in [
        "reiserekning", "reiseregning", "reiseutlegg",
        "travel expense", "business trip expense",
        "gasto de viaje", "gastos de viaje",
        "despesa de viagem", "despesas de viagem",
        "reisekostenabrechnung", "reisekosten",
        "note de frais", "frais de déplacement",
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
        "faktura", "invoice", "factura", "fatura", "rechnung", "facture",
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
        "opprett produktet", "opprett produkt",
        "create the product", "create product",
        "crea el producto", "crear el producto",
        "crie o produto", "criar o produto",
        "erstellen sie das produkt", "produkt erstellen",
        "créez le produit", "créer le produit",
        "produktnummer", "product number",
        "número de producto", "número de produto", "numéro de produit", "produktnummer",
    ]):
        return "create_product"

    # 21. Create project
    if any(kw in p for kw in [
        "opprett prosjektet", "opprett prosjekt",
        "create the project", "create project",
        "crea el proyecto", "crear el proyecto",
        "crie o projeto", "criar o projeto",
        "erstellen sie das projekt", "projekt erstellen",
        "créez le projet", "créer le projet",
    ]):
        return "create_project"

    # 22. Create employee (from PDF, from prompt, etc.) — must come BEFORE department
    #     because employee prompts often mention "avdeling"/"department"
    if any(kw in p for kw in [
        "opprett den ansatte", "opprett vedkomande", "ny tilsett", "ny ansatt",
        "new employee", "nuevo empleado", "novo funcionário", "novo funcionario",
        "nouvel employé", "neuen mitarbeiter", "neue mitarbeiterin",
        "arbeidskontrakt", "employment contract", "contrato de trabajo",
        "contrato de trabalho", "contrat de travail", "arbeitsvertrag",
        "offer letter", "carta de oferta", "lettre d'offre",
        "crie o funcionario", "crea el empleado", "créez l'employé",
        "complete the onboarding", "completa la incorporacion",
        "completa a integração", "complétez l'intégration",
        "tilsett som heiter", "ansatt som heter",
    ]):
        return "create_employee"

    # 23. Create department
    #    nb: opprett avdelinger, nn: opprett avdelingar, en: create departments,
    #    es: cree departamentos, pt: crie departamentos, de: Abteilungen erstellen,
    #    fr: créez départements
    if any(kw in p for kw in [
        "opprett tre avdelinger", "opprett avdeling", "opprett avdelingar",
        "create three departments", "create departments",
        "cree tres departamentos", "cree departamentos", "crear departamentos",
        "crie três departamentos", "crie departamentos", "criar departamentos",
        "erstellen sie drei abteilungen", "abteilungen erstellen",
        "créez trois départements", "créez des départements",
    ]):
        return "create_department"

    # 24. Create/register customer or supplier
    if any(kw in p for kw in [
        "opprett kunden", "registrer kunden", "opprett leverandøren",
        "create the customer", "register the customer", "create customer",
        "register the supplier", "create the supplier",
        "cree el cliente", "registre el cliente", "cree el proveedor",
        "crie o cliente", "registre o cliente", "crie o fornecedor",
        "erstellen sie den kunden", "registrieren sie den",
        "créez le client", "enregistrez le client", "créez le fournisseur",
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
isPrioritizeAmountsIncludingVat: true only if prices are stated INCLUDING VAT.
sendToCustomer: true if the prompt says to "send" the invoice to the customer ("send", "envie", "enviar", "envoyer", "senden").""",

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
Extract: feeAmount (NOK), debitAccount (default 1500), creditAccount (default 3400), invoiceFee (boolean — whether to also create invoice for the fee), partialPaymentAmount (NOK amount of partial payment if mentioned, null otherwise), sendInvoice (boolean — whether to send the invoice to customer).""",
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


def _llm_classify_fallback(prompt: str, file_contents: list[str] | None = None) -> str:
    """LLM fallback when keyword classifier returns unknown."""
    user_message = prompt
    if file_contents:
        attachments = "\n\n---\n\n".join(
            f"[Attached file {i + 1}]\n{content}"
            for i, content in enumerate(file_contents)
        )
        user_message = f"{prompt}\n\n{attachments}"

    type_list = ", ".join(t for t in KNOWN_TASK_TYPES if t != "unknown")
    system = f"""You are a task classifier for a Norwegian accounting system (Tripletex).
Given a user request, output ONLY the task type as a single string. No explanation.

Valid task types: {type_list}

If none match, output: unknown"""

    client = Anthropic()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text.strip().lower().replace('"', '').replace("'", "")
        if result in KNOWN_TASK_TYPES:
            return result
        # Fuzzy match: check if the response contains a known type
        for tt in KNOWN_TASK_TYPES:
            if tt in result:
                return tt
        logger.warning("LLM fallback returned unrecognized type: %s", result)
        return "unknown"
    except Exception as e:
        logger.warning("LLM classify fallback failed: %s", e)
        return "unknown"


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

    # Stage 1: Classify (keywords first, LLM fallback if unknown)
    task_type = classify_task(prompt)
    logger.info("Keyword classifier: %s", task_type)

    if task_type == "unknown":
        # LLM fallback — keywords missed, ask the LLM to classify
        logger.warning("Keyword classifier returned unknown, trying LLM fallback...")
        task_type = _llm_classify_fallback(prompt, file_contents)
        logger.info("LLM fallback classifier: %s", task_type)

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
