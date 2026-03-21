# Tripletex — Sandbox Account

## Getting Your Sandbox
1. Go to the Tripletex submission page on the platform
2. Click "Get Sandbox Account" — provisioned instantly
3. You receive: Tripletex UI URL, API base URL, Session token

## Web UI Login
- URL: https://kkpqfuj-amager.tripletex.dev
- Enter the email shown on your sandbox card
- First time: click "Forgot password" to set up Visma Connect account
- Once set up, same credentials work for **all** Tripletex test accounts (including competition ones)

## API Authentication
**Basic Auth** — username: `0`, password: `<session_token>`

```python
import requests

BASE_URL = "https://kkpqfuj-amager.tripletex.dev/v2"
SESSION_TOKEN = "your-session-token-here"

response = requests.get(
    f"{BASE_URL}/employee",
    auth=("0", SESSION_TOKEN),
    params={"fields": "id,firstName,lastName,email"}
)
print(response.json())
```

```bash
curl -u "0:your-session-token-here" \
  "https://kkpqfuj-amager.tripletex.dev/v2/employee?fields=id,firstName,lastName"
```

## Sandbox vs Competition
| Aspect | Sandbox | Competition |
|--------|---------|-------------|
| Account | Persistent, yours to keep | Fresh account per submission |
| API access | Direct to Tripletex | Via authenticated proxy |
| Data | Accumulates over time | Starts empty each time |
| Scoring | None | Automated field-by-field |

## Tips
- Create test data manually in the UI, then query via API to learn response format
- Practice the same operations your agent will need
- **Token expires March 31, 2026**
- One sandbox per team, shared by all members
