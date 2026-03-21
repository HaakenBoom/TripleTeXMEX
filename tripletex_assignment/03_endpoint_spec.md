# Tripletex — Endpoint Specification

## /solve Endpoint
- **Method:** POST
- **Content-Type:** application/json
- **Timeout:** 300 seconds (5 minutes)

## Request Format
```json
{
  "prompt": "Opprett en ansatt med navn Ola Nordmann, ola@example.org. Han skal være kontoadministrator.",
  "files": [
    {
      "filename": "faktura.pdf",
      "content_base64": "JVBERi0xLjQg...",
      "mime_type": "application/pdf"
    }
  ],
  "tripletex_credentials": {
    "base_url": "https://<provided-per-submission>/v2",
    "session_token": "abc123..."
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `prompt` | string | The task in natural language (7 possible languages) |
| `files` | array | Attachments (PDFs, images) — may be empty |
| `files[].filename` | string | Original filename |
| `files[].content_base64` | string | Base64-encoded file content |
| `files[].mime_type` | string | MIME type (application/pdf, image/png, etc.) |
| `tripletex_credentials.base_url` | string | Proxy API URL — use this, not standard Tripletex URL |
| `tripletex_credentials.session_token` | string | Session token for authentication |

## Response Format
```json
{"status": "completed"}
```
Must return HTTP 200.

## Authentication to Tripletex API
**Basic Auth:** username `0`, password = `session_token` from request.

## Optional: Protect Your Endpoint
Set an API key when submitting — we send it as:
```
Authorization: Bearer <your-api-key>
```

## Requirements
- Endpoint must be **HTTPS**
- Must respond within **5 minutes**
- Must return `{"status": "completed"}` with HTTP 200
- All Tripletex API calls must go through the provided `base_url` (proxy)

## Available Tripletex API Endpoints
| Endpoint | Methods | Description |
|----------|---------|-------------|
| `/employee` | GET, POST, PUT | Manage employees |
| `/customer` | GET, POST, PUT | Manage customers |
| `/product` | GET, POST | Manage products |
| `/invoice` | GET, POST | Create and query invoices |
| `/order` | GET, POST | Manage orders |
| `/travelExpense` | GET, POST, PUT, DELETE | Travel expense reports |
| `/project` | GET, POST | Manage projects |
| `/department` | GET, POST | Manage departments |
| `/ledger/account` | GET | Query chart of accounts |
| `/ledger/posting` | GET | Query ledger postings |
| `/ledger/voucher` | GET, POST, DELETE | Manage vouchers |

## API Tips
- `?fields=id,firstName,lastName,*` to select specific fields
- `?from=0&count=100` for pagination
- POST/PUT take JSON body
- DELETE uses ID in URL path: `DELETE /employee/123`
- List responses wrapped: `{"fullResultSize": N, "values": [...]}`
