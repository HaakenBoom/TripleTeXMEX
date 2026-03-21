# Tripletex API - Relevant Endpoints & Schemas Reference

> Extracted from `openapi_full.json`. None of the schemas define explicit `required` fields in the OpenAPI spec -- all fields are technically optional at the schema level. However, the API will reject requests missing logically necessary fields (noted below as "effectively required").

---

## 1. POST /employee

- **Summary**: Create one employee.
- **Request Body**: `Employee` schema (see below)
- **Response 201**: `ResponseWrapperEmployee` -> `{ value: Employee }`

## 2. POST /customer

- **Summary**: Create one customer.
- **Request Body**: `Customer` schema (see below)
- **Response 201**: `ResponseWrapperCustomer` -> `{ value: Customer }`

## 3. POST /product

- **Summary**: Create new product.
- **Request Body**: `Product` schema (see below)
- **Response 201**: `ResponseWrapperProduct` -> `{ value: Product }`

## 4. POST /order

- **Summary**: Create order.
- **Request Body**: `Order` schema (see below)
- **Response 201**: `ResponseWrapperOrder` -> `{ value: Order }`

## 5. POST /invoice

- **Summary**: Create invoice.
- **Request Body**: `Invoice` schema (see below)
- **Response 201**: `ResponseWrapperInvoice` -> `{ value: Invoice }`
- **Query Parameters**:
  - `sendToCustomer` (boolean, default: true) - Whether to send the invoice to the customer
  - `paymentTypeId` (int32, optional) - Payment type ID
  - `paidAmount` (number, optional) - Prepaid amount

## 6. POST /project

- **Summary**: Create project.
- **Request Body**: `Project` schema (see below)
- **Response 201**: `ResponseWrapperProject` -> `{ value: Project }`

## 7. POST /department

- **Summary**: Create department.
- **Request Body**: `Department` schema (see below)
- **Response 201**: `ResponseWrapperDepartment` -> `{ value: Department }`

## 8. POST /travelExpense

- **Summary**: Create travel expense.
- **Request Body**: `TravelExpense` schema (see below)
- **Response 201**: `ResponseWrapperTravelExpense` -> `{ value: TravelExpense }`

## 9. PUT /employee/{id}

- **Summary**: Update employee.
- **Path Parameter**: `id` (int64, required)
- **Request Body**: `Employee` schema
- **Response 200**: `ResponseWrapperEmployee` -> `{ value: Employee }`

## 10. PUT /customer/{id}

- **Summary**: Update customer.
- **Path Parameter**: `id` (int64, required)
- **Request Body**: `Customer` schema
- **Response 200**: `ResponseWrapperCustomer` -> `{ value: Customer }`

## 11. POST /ledger/voucher

- **Summary**: Add new voucher.
- **Request Body**: `Voucher` schema (see below)
- **Response 201**: `ResponseWrapperVoucher` -> `{ value: Voucher }`
- **Query Parameters**:
  - `sendToLedger` (boolean, default: true) - Whether to send the voucher to the ledger

## 12. DELETE /travelExpense/{id}

- **Summary**: Delete travel expense.
- **Path Parameter**: `id` (int64, required)
- **Response 204**: No content (successful deletion)
- **No request body.**

---

# Schema Definitions

All schemas share common base fields: `id` (int64), `version` (int32), `changes` (array, readOnly), `url` (string, readOnly).

---

## Employee

Effectively required for creation: `firstName`, `lastName`

| Field | Type | Notes |
|-------|------|-------|
| firstName | string | |
| lastName | string | |
| employeeNumber | string | |
| dateOfBirth | string | |
| email | string | |
| phoneNumberMobileCountry | ref -> Country | |
| phoneNumberMobile | string | |
| phoneNumberHome | string | |
| phoneNumberWork | string | |
| nationalIdentityNumber | string | |
| dnumber | string | |
| internationalId | ref -> InternationalId | |
| bankAccountNumber | string | |
| iban | string | IBAN field |
| bic | string | BIC (SWIFT) field |
| creditorBankCountryId | int32 | Country of creditor bank |
| usesAbroadPayment | boolean | Domestic vs abroad remittance. Requires Autopay + valid IBAN/BIC/country combo |
| userType | enum | `STANDARD`, `EXTENDED`, `NO_ACCESS` |
| allowInformationRegistration | boolean | readOnly |
| isContact | boolean | Whether employee is an external contact |
| isProxy | boolean | readOnly - accounting/auditor office |
| comments | string | |
| address | ref -> Address | |
| department | ref -> Department | |
| employments | array of Employment | |
| holidayAllowanceEarned | ref -> HolidayAllowanceEarned | |
| employeeCategory | ref -> EmployeeCategory | |
| displayName | string | readOnly |
| pictureId | int32 | readOnly |
| companyId | int32 | readOnly |

---

## Customer

Effectively required for creation: `name`

| Field | Type | Notes |
|-------|------|-------|
| name | string | |
| organizationNumber | string | |
| globalLocationNumber | int64 | min: 0 |
| supplierNumber | int32 | min: 0 |
| customerNumber | int32 | min: 0 |
| isSupplier | boolean | Also a supplier? |
| isCustomer | boolean | readOnly |
| isInactive | boolean | |
| accountManager | ref -> Employee | |
| department | ref -> Department | |
| email | string | |
| invoiceEmail | string | |
| overdueNoticeEmail | string | |
| bankAccounts | array of string | DEPRECATED |
| phoneNumber | string | |
| phoneNumberMobile | string | |
| description | string | |
| language | enum | `NO`, `EN` |
| displayName | string | |
| isPrivateIndividual | boolean | |
| singleCustomerInvoice | boolean | Multiple orders on one invoice |
| invoiceSendMethod | enum | `EMAIL`, `EHF`, `EFAKTURA`, `AVTALEGIRO`, `VIPPS`, `PAPER`, `MANUAL` |
| emailAttachmentType | enum | `LINK`, `ATTACHMENT` |
| postalAddress | ref -> Address | |
| physicalAddress | ref -> Address | |
| deliveryAddress | ref -> DeliveryAddress | |
| category1 | ref -> CustomerCategory | |
| category2 | ref -> CustomerCategory | |
| category3 | ref -> CustomerCategory | |
| invoicesDueIn | int32 | min: 0, max: 10000. Days/months until due |
| invoicesDueInType | enum | `DAYS`, `MONTHS`, `RECURRING_DAY_OF_MONTH` |
| currency | ref -> Currency | |
| bankAccountPresentation | array of CompanyBankAccountPresentation | |
| ledgerAccount | ref -> Account | |
| isFactoring | boolean | Send invoices to factoring |
| invoiceSendSMSNotification | boolean | |
| invoiceSMSNotificationNumber | string | Norwegian phone number |
| isAutomaticSoftReminderEnabled | boolean | |
| isAutomaticReminderEnabled | boolean | |
| isAutomaticNoticeOfDebtCollectionEnabled | boolean | |
| discountPercentage | number | Default discount % |
| website | string | |

---

## Product

Effectively required for creation: `name`

| Field | Type | Notes |
|-------|------|-------|
| name | string | |
| number | string | |
| description | string | |
| orderLineDescription | string | |
| ean | string | |
| elNumber | string | readOnly |
| nrfNumber | string | readOnly |
| costExcludingVatCurrency | number | Purchase cost excl VAT in product currency |
| expenses | number | |
| expensesInPercent | number | readOnly |
| costPrice | number | readOnly |
| profit | number | readOnly |
| profitInPercent | number | readOnly |
| priceExcludingVatCurrency | number | Purchase price excl VAT in product currency |
| priceIncludingVatCurrency | number | Purchase price incl VAT in product currency |
| isInactive | boolean | |
| discountGroup | ref -> DiscountGroup | |
| productUnit | ref -> ProductUnit | |
| isStockItem | boolean | |
| stockOfGoods | number | readOnly |
| availableStock | number | readOnly |
| incomingStock | number | readOnly |
| outgoingStock | number | readOnly |
| vatType | ref -> VatType | |
| currency | ref -> Currency | |
| department | ref -> Department | |
| account | ref -> Account | |
| supplier | ref -> Supplier | |
| resaleProduct | ref -> Product | |
| isDeletable | boolean | DEPRECATED - always false |
| hasSupplierProductConnected | boolean | |
| weight | number | |
| weightUnit | enum | `kg`, `g`, `hg` |
| volume | number | |
| volumeUnit | enum | `cm3`, `dm3`, `m3` |
| hsnCode | string | |
| image | ref -> Document | |
| mainSupplierProduct | ref -> SupplierProduct | |
| minStockLevel | number | Min stock for Logistics Basics |
| displayName | string | readOnly |
| displayNumber | string | readOnly |

---

## Order

Effectively required for creation: `customer`, `deliveryDate`, `orderDate`

| Field | Type | Notes |
|-------|------|-------|
| customer | ref -> Customer | |
| contact | ref -> Contact | |
| attn | ref -> Contact | |
| receiverEmail | string | |
| overdueNoticeEmail | string | |
| number | string | |
| reference | string | |
| ourContact | ref -> Contact | |
| ourContactEmployee | ref -> Employee | |
| department | ref -> Department | |
| orderDate | string | |
| project | ref -> Project | |
| invoiceComment | string | Shown on invoice |
| currency | ref -> Currency | |
| invoicesDueIn | int32 | min: 0, max: 10000 |
| status | enum | `NOT_CHOSEN`, `NEW`, `CONFIRMATION_SENT`, `READY_FOR_PICKING`, `PICKED`, `PACKED`, `READY_FOR_SHIPPING`, `READY_FOR_INVOICING`, `INVOICED`, `CANCELLED` (Logistics only) |
| invoicesDueInType | enum | `DAYS`, `MONTHS`, `RECURRING_DAY_OF_MONTH` |
| isShowOpenPostsOnInvoices | boolean | |
| isClosed | boolean | Closed orders cannot be invoiced |
| deliveryDate | string | |
| deliveryAddress | ref -> DeliveryAddress | |
| deliveryComment | string | |
| isPrioritizeAmountsIncludingVat | boolean | |
| orderLineSorting | enum | `ID`, `PRODUCT`, `PRODUCT_DESCENDING`, `CUSTOM` |
| orderGroups | array of OrderGroup | |
| orderLines | array of OrderLine | Can embed new OrderLines |
| isSubscription | boolean | Enables periodical invoicing |
| subscriptionDuration | int32 | min: 0 |
| subscriptionDurationType | enum | `MONTHS`, `YEAR` |
| subscriptionPeriodsOnInvoice | int32 | min: 0 |
| subscriptionPeriodsOnInvoiceType | enum | `MONTHS` (readOnly) |
| subscriptionInvoicingTimeInAdvanceOrArrears | enum | `ADVANCE`, `ARREARS` |
| subscriptionInvoicingTime | int32 | min: 0 |
| subscriptionInvoicingTimeType | enum | `DAYS`, `MONTHS` |
| isSubscriptionAutoInvoicing | boolean | |
| preliminaryInvoice | ref -> Invoice | |
| sendMethodDescription | string | |
| invoiceOnAccountVatHigh | boolean | |
| markUpOrderLines | number | Mark-up % for order lines |
| discountPercentage | number | Default discount % |
| displayName | string | readOnly |
| customerName | string | readOnly |
| canCreateBackorder | boolean | readOnly |
| attachment | array of Document | readOnly |
| travelReports | array of TravelExpense | readOnly |

---

## OrderLine

Effectively required: `order` (when creating standalone), or embedded in Order's `orderLines` array

| Field | Type | Notes |
|-------|------|-------|
| product | ref -> Product | |
| inventory | ref -> Inventory | |
| inventoryLocation | ref -> InventoryLocation | |
| description | string | |
| count | number | Quantity |
| unitCostCurrency | number | Unit purchase cost excl VAT in order currency |
| unitPriceExcludingVatCurrency | number | Unit price excl VAT. If only one of excl/incl is given, the other is calculated |
| unitPriceIncludingVatCurrency | number | Unit price incl VAT |
| currency | ref -> Currency | |
| markup | number | Markup % |
| discount | number | Discount % |
| vatType | ref -> VatType | |
| amountExcludingVatCurrency | number | readOnly - total excl VAT |
| amountIncludingVatCurrency | number | readOnly - total incl VAT |
| vendor | ref -> Company | |
| order | ref -> Order | |
| isSubscription | boolean | |
| subscriptionPeriodStart | string | |
| subscriptionPeriodEnd | string | |
| orderGroup | ref -> OrderGroup | |
| sortIndex | int32 | min: 0. Presentation order (for CUSTOM sorting) |
| isPicked | boolean | Logistics only |
| pickedDate | string | Logistics only |
| orderedQuantity | number | Backorder: original quantity ordered |
| isCharged | boolean | Whether the line is charged |
| displayName | string | readOnly |

---

## Invoice

Effectively required for creation: `invoiceDate`, `invoiceDueDate`, `orders` (array with at least one Order ref)

| Field | Type | Notes |
|-------|------|-------|
| invoiceNumber | int32 | min: 0. Set to 0 for auto-generation |
| invoiceDate | string | |
| customer | ref -> Customer | |
| invoiceDueDate | string | |
| kid | string | Customer identification number |
| invoiceComment | string | readOnly - from order |
| comment | string | Invoice-specific comment |
| orders | array of Order | Only one order per invoice supported currently |
| orderLines | array of OrderLine | readOnly |
| travelReports | array of TravelExpense | readOnly |
| projectInvoiceDetails | array of ProjectInvoiceDetails | readOnly |
| voucher | ref -> Voucher | |
| deliveryDate | string | readOnly |
| amount | number | readOnly - in company currency (NOK) |
| amountCurrency | number | readOnly - in specified currency |
| amountExcludingVat | number | readOnly |
| amountExcludingVatCurrency | number | readOnly |
| amountRoundoff | number | readOnly |
| amountRoundoffCurrency | number | readOnly |
| amountOutstanding | number | readOnly |
| amountCurrencyOutstanding | number | readOnly |
| amountOutstandingTotal | number | readOnly |
| amountCurrencyOutstandingTotal | number | readOnly |
| sumRemits | number | readOnly |
| currency | ref -> Currency | |
| isCreditNote | boolean | readOnly |
| isCharged | boolean | readOnly |
| isApproved | boolean | readOnly |
| creditedInvoice | int64 | readOnly - ID of original if credit note |
| isCredited | boolean | readOnly |
| postings | array of Posting | readOnly |
| reminders | array of Reminder | readOnly |
| invoiceRemarks | string | DEPRECATED - use invoiceRemark |
| invoiceRemark | ref -> InvoiceRemark | |
| paymentTypeId | int32 | min: 0. For prepaid invoices |
| paidAmount | number | Prepaid amount |
| ehfSendStatus | enum | DEPRECATED. `DO_NOT_SEND`, `SEND`, `SENT`, `SEND_FAILURE_RECIPIENT_NOT_FOUND` |
| isPeriodizationPossible | boolean | readOnly |
| documentId | int32 | readOnly |

---

## Project

Effectively required for creation: `name`, `projectManager`, `isInternal`

| Field | Type | Notes |
|-------|------|-------|
| name | string | |
| number | string | Auto-generated if NULL |
| description | string | |
| projectManager | ref -> Employee | |
| department | ref -> Department | |
| mainProject | ref -> Project | |
| startDate | string | |
| endDate | string | |
| customer | ref -> Customer | |
| isClosed | boolean | |
| isReadyForInvoicing | boolean | |
| isInternal | boolean | |
| isOffer | boolean | true = Project Offer, false = Project (default false) |
| isFixedPrice | boolean | true = fixed price, false = hourly rate |
| projectCategory | ref -> ProjectCategory | |
| deliveryAddress | ref -> Address | |
| boligmappaAddress | ref -> Address | |
| displayNameFormat | enum | `NAME_STANDARD`, `NAME_INCL_CUSTOMER_NAME`, `NAME_INCL_PARENT_NAME`, `NAME_INCL_PARENT_NUMBER`, `NAME_INCL_PARENT_NAME_AND_NUMBER` |
| reference | string | |
| externalAccountsNumber | string | |
| vatType | ref -> VatType | |
| fixedprice | number | Fixed price in project currency |
| currency | ref -> Currency | |
| markUpOrderLines | number | Mark-up % for order lines |
| markUpFeesEarned | number | Mark-up % for fees earned |
| isPriceCeiling | boolean | Hourly rate project has price ceiling |
| priceCeilingAmount | number | In project currency |
| projectHourlyRates | array of ProjectHourlyRate | |
| forParticipantsOnly | boolean | Only participants can register info |
| participants | array of ProjectParticipant | |
| contact | ref -> Contact | |
| attention | ref -> Contact | |
| invoiceComment | string | For project invoices |
| preliminaryInvoice | ref -> Invoice | |
| generalProjectActivitiesPerProjectOnly | boolean | |
| projectActivities | array of ProjectActivity | |
| invoiceDueDate | int32 | |
| invoiceDueDateType | enum | `DAYS`, `MONTHS`, `RECURRING_DAY_OF_MONTH` |
| invoiceReceiverEmail | string | Overrides customer default |
| overdueNoticeEmail | string | Overrides customer default |
| accessType | enum | `NONE`, `READ`, `WRITE` |
| useProductNetPrice | boolean | |
| ignoreCompanyProductDiscountAgreement | boolean | |
| invoiceOnAccountVatHigh | boolean | |
| accountingDimensionValues | array of AccountingDimensionValue | BETA |
| displayName | string | readOnly |
| discountPercentage | number | readOnly |
| contributionMarginPercent | number | readOnly |
| numberOfSubProjects | int32 | readOnly |
| numberOfProjectParticipants | int32 | readOnly |
| orderLines | array of ProjectOrderLine | readOnly |
| invoicingPlan | array of Invoice | readOnly |
| customerName | string | readOnly |
| hierarchyLevel | int32 | readOnly |
| hierarchyNameAndNumber | string | readOnly |
| projectManagerNameAndNumber | string | readOnly |
| totalInvoicedOnAccountAmountAbsoluteCurrency | number | readOnly |
| invoiceReserveTotalAmountCurrency | number | readOnly |

---

## Department

Effectively required for creation: `name`

| Field | Type | Notes |
|-------|------|-------|
| name | string | |
| departmentNumber | string | |
| departmentManager | ref -> Employee | |
| displayName | string | readOnly |
| isInactive | boolean | |
| businessActivityTypeId | int32 | readOnly. Tax category/VAT report separation |

---

## TravelExpense

Effectively required for creation: `employee`, `title`

| Field | Type | Notes |
|-------|------|-------|
| employee | ref -> Employee | |
| title | string | |
| project | ref -> Project | |
| department | ref -> Department | |
| approvedBy | ref -> Employee | readOnly context |
| completedBy | ref -> Employee | readOnly context |
| rejectedBy | ref -> Employee | readOnly context |
| freeDimension1 | ref -> AccountingDimensionValue | |
| freeDimension2 | ref -> AccountingDimensionValue | |
| freeDimension3 | ref -> AccountingDimensionValue | |
| payslip | ref -> Payslip | |
| vatType | ref -> VatType | |
| paymentCurrency | ref -> Currency | |
| travelDetails | ref -> TravelDetails | |
| voucher | ref -> Voucher | |
| attachment | ref -> Document | |
| attestationSteps | array of AttestationStep | |
| attestation | ref -> Attestation | |
| isCompleted | boolean | readOnly |
| isApproved | boolean | readOnly |
| rejectedComment | string | readOnly |
| isChargeable | boolean | |
| isFixedInvoicedAmount | boolean | |
| isMarkupInvoicedPercent | boolean | |
| isIncludeAttachedReceiptsWhenReinvoicing | boolean | |
| completedDate | string | readOnly |
| approvedDate | string | readOnly |
| date | string | readOnly |
| travelAdvance | number | |
| fixedInvoicedAmount | number | |
| markupInvoicedPercent | number | |
| amount | number | readOnly |
| chargeableAmountCurrency | number | readOnly |
| paymentAmount | number | readOnly |
| chargeableAmount | number | readOnly |
| lowRateVAT | number | readOnly |
| mediumRateVAT | number | readOnly |
| highRateVAT | number | readOnly |
| paymentAmountCurrency | number | readOnly |
| number | int32 | readOnly |
| invoice | ref -> Invoice | |
| perDiemCompensations | array of PerDiemCompensation | |
| mileageAllowances | array of MileageAllowance | readOnly |
| accommodationAllowances | array of AccommodationAllowance | readOnly |
| costs | array of Cost | |
| attachmentCount | int32 | readOnly |
| state | enum | readOnly. `ALL`, `REJECTED`, `OPEN`, `APPROVED`, `SALARY_PAID`, `DELIVERED` |
| displayName | string | readOnly |
| type | int32 | readOnly |

---

## Voucher

Effectively required for creation: `date`, `description`

| Field | Type | Notes |
|-------|------|-------|
| date | string | |
| number | int32 | readOnly. System-generated |
| tempNumber | int32 | readOnly. Temporary voucher number |
| year | int32 | readOnly. System-generated |
| description | string | |
| voucherType | ref -> VoucherType | Must NOT be 'Outgoing Invoice' type. Use null or the Invoice endpoint instead |
| reverseVoucher | ref -> Voucher | |
| postings | array of Posting | The ledger postings for this voucher |
| document | ref -> Document | |
| attachment | ref -> Document | |
| externalVoucherNumber | string | Max 70 characters |
| ediDocument | ref -> Document | |
| supplierVoucherType | enum | readOnly. `TYPE_SUPPLIER_INVOICE_SIMPLE`, `TYPE_SUPPLIER_INVOICE_DETAILED` |
| wasAutoMatched | boolean | readOnly |
| vendorInvoiceNumber | string | |
| displayName | string | readOnly |
| numberAsString | string | readOnly |

### Posting (sub-schema used in Voucher.postings)

Each posting represents a ledger entry line.

| Field | Type | Notes |
|-------|------|-------|
| date | string | |
| description | string | |
| account | ref -> Account | The ledger account |
| amortizationAccount | ref -> Account | For amortization |
| amortizationStartDate | string | |
| amortizationEndDate | string | |
| customer | ref -> Customer | |
| supplier | ref -> Supplier | |
| employee | ref -> Employee | |
| project | ref -> Project | |
| product | ref -> Product | |
| department | ref -> Department | |
| vatType | ref -> VatType | |
| amount | number | Amount in company currency |
| amountCurrency | number | Amount in specified currency |
| amountGross | number | |
| amountGrossCurrency | number | |
| currency | ref -> Currency | |
| closeGroup | ref -> CloseGroup | |
| invoiceNumber | string | |
| termOfPayment | string | |
| row | int32 | min: 0 |
| type | enum | readOnly. `INCOMING_PAYMENT`, `INCOMING_PAYMENT_OPPOSITE`, `INCOMING_INVOICE_CUSTOMER_POSTING`, `INVOICE_EXPENSE`, `OUTGOING_INVOICE_CUSTOMER_POSTING`, `WAGE` |
| quantityAmount1 | number | |
| quantityType1 | ref -> ProductUnit | |
| quantityAmount2 | number | |
| quantityType2 | ref -> ProductUnit | |
| postingRuleId | int32 | Payment type ID (internal payments only) |
| freeAccountingDimension1 | ref -> AccountingDimensionValue | |
| freeAccountingDimension2 | ref -> AccountingDimensionValue | |
| freeAccountingDimension3 | ref -> AccountingDimensionValue | |
| asset | ref -> Asset | |

---

## Notes on Reference Fields

When a field is `ref -> SomeSchema`, you typically pass an object with just `{ "id": <int> }` to reference an existing entity, rather than embedding the full object. For example:

```json
{
  "customer": { "id": 12345 },
  "department": { "id": 67 },
  "projectManager": { "id": 89 }
}
```

## Response Wrapper Pattern

All successful creation/update responses follow the pattern:
```json
{
  "value": { /* full entity object */ }
}
```

The `value` field contains the complete entity with all fields populated (including readOnly fields like `id`, `version`, `url`, `displayName`, etc.).
