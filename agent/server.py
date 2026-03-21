import json
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Absolute path for logs — never depends on working directory
LOG_DIR = Path(__file__).parent.parent / "run_logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI()


def _save_server_log(prompt: str, base_url: str, result: str | None, error: str | None, elapsed: float):
    """Save a log entry from the server level — guaranteed to run even if solver crashes."""
    filename = f"server_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    data = {
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt[:500],
        "base_url": base_url,
        "result": result[:500] if result else None,
        "error": error,
        "elapsed_seconds": round(elapsed, 2),
    }
    try:
        with open(LOG_DIR / filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Server log saved: %s", LOG_DIR / filename)
    except Exception as e:
        logger.error("FAILED to save server log: %s", e)
        # Last resort: print to stderr
        print(f"SERVER LOG DUMP: {json.dumps(data, default=str)}", flush=True)


@app.post("/solve")
async def solve(request: Request):
    import time
    start = time.time()
    prompt = ""
    base_url = ""

    try:
        body = await request.json()

        prompt = body["prompt"]
        files = body.get("files", [])
        creds = body["tripletex_credentials"]
        base_url = creds["base_url"]
        token = creds["session_token"]

        logger.info("=" * 60)
        logger.info("NEW TASK RECEIVED")
        logger.info("  Prompt: %s", prompt[:200])
        logger.info("  Files: %d", len(files))
        logger.info("  API URL: %s", base_url)
        logger.info("=" * 60)

        result = None
        error = None
        try:
            from agent.solver import solve_task
            result = solve_task(prompt, files, base_url, token)
            logger.info("Solver completed: %s", result[:200] if result else "no output")
        except Exception as e:
            error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error("Solver error: %s", e, exc_info=True)

        elapsed = time.time() - start
        _save_server_log(prompt, base_url, result, error, elapsed)

        return JSONResponse({"status": "completed"})

    except Exception as e:
        elapsed = time.time() - start
        error = f"REQUEST PARSE ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        logger.error("Failed to parse request: %s", e, exc_info=True)
        _save_server_log(prompt or "PARSE_FAILED", base_url or "unknown", None, error, elapsed)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/")
async def root():
    """Handle root path probes from the competition platform."""
    return {"status": "ok", "endpoints": ["/solve", "/health"]}


@app.post("/")
async def root_post(request: Request):
    """Redirect root POST to /solve — the platform may hit / instead of /solve."""
    logger.warning("Received POST on / — redirecting to /solve handler")
    return await solve(request)


@app.get("/health")
async def health():
    return {"status": "ok"}
