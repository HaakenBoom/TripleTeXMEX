import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Thread-safe list to collect all API calls for the current run
_call_log: list[dict] = []
_call_log_lock = threading.Lock()

LOG_DIR = Path("run_logs")
LOG_DIR.mkdir(exist_ok=True)


def get_call_log() -> list[dict]:
    """Return and clear the current call log."""
    with _call_log_lock:
        log = list(_call_log)
        _call_log.clear()
        return log


def _record_call(method: str, url: str, status: int, request_body=None, request_params=None, response_body=None):
    """Record an API call for post-run analysis."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "method": method,
        "url": url,
        "status": status,
        "request_params": request_params,
        "request_body": request_body,
        "response_body": response_body,
    }
    with _call_log_lock:
        _call_log.append(entry)


class TripletexClient:
    def __init__(self, base_url: str, session_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = ("0", session_token)
        self._got_403 = False

    def _safe_json(self, resp: requests.Response) -> dict:
        """Parse JSON response, returning an error dict if parsing fails."""
        try:
            return resp.json()
        except Exception:
            logger.warning("Non-JSON response (%d): %s", resp.status_code, resp.text[:200])
            return {"status": resp.status_code, "message": resp.text[:500], "error": "non-json response"}

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        if self._got_403:
            return {"status": 403, "message": "Session invalid (early bail)"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.info(f"GET {url} params={params}")
        resp = requests.get(url, auth=self.auth, params=params, timeout=30)
        logger.info(f"  -> {resp.status_code}")
        body = self._safe_json(resp)
        _record_call("GET", url, resp.status_code, request_params=params, response_body=body)
        if resp.status_code == 403:
            self._got_403 = True
        return body

    def post(self, endpoint: str, json_body: dict, params: dict | None = None) -> dict:
        if self._got_403:
            return {"status": 403, "message": "Session invalid (early bail)"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.info(f"POST {url} body={json.dumps(json_body, ensure_ascii=False)[:300]} params={params}")
        resp = requests.post(url, auth=self.auth, json=json_body, params=params, timeout=30)
        resp_text = resp.text[:1000]
        logger.info(f"  -> {resp.status_code} {resp_text}")
        body = self._safe_json(resp)
        _record_call("POST", url, resp.status_code, request_body=json_body, request_params=params, response_body=body)
        if resp.status_code == 403:
            self._got_403 = True
        return body

    def put(self, endpoint: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        if self._got_403:
            return {"status": 403, "message": "Session invalid (early bail)"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.info(f"PUT {url} body={json.dumps(json_body, ensure_ascii=False)[:300] if json_body else None} params={params}")
        resp = requests.put(url, auth=self.auth, json=json_body, params=params, timeout=30)
        resp_text = resp.text[:1000]
        logger.info(f"  -> {resp.status_code} {resp_text}")
        body = self._safe_json(resp)
        _record_call("PUT", url, resp.status_code, request_body=json_body, request_params=params, response_body=body)
        if resp.status_code == 403:
            self._got_403 = True
        return body

    def delete(self, endpoint: str) -> dict:
        if self._got_403:
            return {"status": 403, "message": "Session invalid (early bail)"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.info(f"DELETE {url}")
        resp = requests.delete(url, auth=self.auth, timeout=30)
        logger.info(f"  -> {resp.status_code}")
        resp_body = self._safe_json(resp) if resp.content else {"status": resp.status_code}
        _record_call("DELETE", url, resp.status_code, response_body=resp_body)
        if resp.status_code == 403:
            self._got_403 = True
        return resp_body
