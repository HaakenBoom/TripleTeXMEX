# Tripletex — Examples & Implementation Guide

## Minimal /solve Endpoint
```python
import base64
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/solve")
async def solve(request: Request):
    body = await request.json()
    prompt = body["prompt"]
    files = body.get("files", [])
    creds = body["tripletex_credentials"]

    base_url = creds["base_url"]
    token = creds["session_token"]
    auth = ("0", token)

    for f in files:
        data = base64.b64decode(f["content_base64"])
        Path(f["filename"]).write_bytes(data)

    # TODO: Use an LLM to interpret the prompt and execute
    # the appropriate Tripletex API calls

    return JSONResponse({"status": "completed"})
```

### Run
```bash
pip install fastapi uvicorn requests
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Expose via HTTPS (for testing)
```bash
npx cloudflared tunnel --url http://localhost:8000
```

## Tripletex API Examples

### List employees
```python
resp = requests.get(
    f"{base_url}/employee",
    auth=auth,
    params={"fields": "id,firstName,lastName,email"}
)
employees = resp.json()["values"]
```

### Create a customer
```python
resp = requests.post(
    f"{base_url}/customer",
    auth=auth,
    json={
        "name": "Acme AS",
        "email": "post@acme.no",
        "isCustomer": True
    }
)
customer_id = resp.json()["value"]["id"]
```

### Create an invoice
```python
today = "2026-03-03"
resp = requests.post(
    f"{base_url}/invoice",
    auth=auth,
    json={
        "invoiceDate": today,
        "invoiceDueDate": today,
        "customer": {"id": customer_id},
        "orders": [{"id": order_id}]
    }
)
```

### Search for a specific entity
```python
resp = requests.get(
    f"{base_url}/customer",
    auth=auth,
    params={
        "name": "Acme",
        "fields": "id,name,email",
        "count": 10
    }
)
matches = resp.json()["values"]
```

## Common Task Patterns
| Pattern | Example | API Flow |
|---------|---------|----------|
| Create single entity | "Create employee Ola Nordmann" | `POST /employee` |
| Create with linking | "Create invoice for customer" | `GET /customer` → `POST /order` → `POST /invoice` |
| Modify existing | "Add phone to contact" | `GET /customer` → `PUT /customer/{id}` |
| Delete/reverse | "Delete travel expense" | `GET /travelExpense` → `DELETE /travelExpense/{id}` |
| Multi-step setup | "Register payment" | `POST /customer` → `POST /invoice` → `POST /payment` |

## Agent Design Strategy
1. **Parse the prompt** — Use LLM to extract task type, entity names, field values, relationships
2. **Handle files** — Decode base64, extract data from PDFs/images
3. **Map to API calls** — Determine endpoints and order; create prerequisites first
4. **Verify work** — Query back to confirm entities exist with correct values
5. **Handle errors** — Parse Tripletex error messages, retry with corrections

## Common Errors
| Error | Cause | Fix |
|-------|-------|-----|
| 401 Unauthorized | Wrong auth format | Use Basic Auth with username `0` and session token |
| 404 Not Found | Wrong endpoint path | Check Tripletex v2 API docs |
| 422 Validation Error | Missing required fields | Read error message for specifics |
| Empty values array | No results found | Broaden search parameters |
| Timeout (5 min) | Agent too slow | Optimize API calls |

## Efficiency Optimization Tips
- **Plan before calling** — Parse prompt fully before making any API calls
- **Avoid trial-and-error** — Every 4xx error reduces efficiency bonus
- **Minimize GET calls** — If you created something, you already know its ID from the response
- **Batch where possible** — Some endpoints accept lists
- **Read error messages** — Fix in one retry, not several

## General Tips
- Sandbox starts empty — create prerequisites before invoices
- `?fields=*` to see all available fields
- Some tasks require enabling modules first (e.g., department accounting)
- Norwegian characters (æ, ø, å) work fine — send as UTF-8
- All API calls through proxy are logged — use for debugging
- Prompts come in 7 languages — agent must handle all
