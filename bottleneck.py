"""Bottleneck analyzer — finds exactly where we lose points on T2/T3 tasks.

Usage:
  python bottleneck.py              # T2+T3 tasks only (default)
  python bottleneck.py --all        # All tiers
  python bottleneck.py --task X     # Single task type
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

TASK_TIERS = {
    "create_employee": 1, "update_employee": 1, "update_employee_role": 1,
    "create_customer": 1, "update_customer": 1, "create_product": 1,
    "create_department": 1, "create_project": 1, "create_travel_expense": 1,
    "delete_travel_expense": 1,
    "create_invoice": 2, "create_invoice_with_payment": 2, "create_credit_note": 2,
    "create_voucher": 2, "delete_voucher": 2, "log_timesheet_hours": 2,
    "reverse_invoice_payment": 2,
    "create_dimension_voucher": 3,
}
MAX_SCORE = {1: 2.0, 2: 4.0, 3: 6.0}


def load_runs():
    log_dir = Path("run_logs")
    runs = []
    for f in sorted(log_dir.glob("run_*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            d["_file"] = f.name
            runs.append(d)
        except Exception:
            pass
    return runs


def analyze_call_waste(api_calls):
    """Categorize each API call as necessary or wasteful."""
    categories = defaultdict(list)
    for call in api_calls:
        method = call.get("method", "?")
        url = str(call.get("url", ""))
        endpoint = url.split("/v2/")[-1] if "/v2/" in url else url
        status = call.get("status", 0)
        body = call.get("request_body") or {}
        resp = call.get("response_body") or {}

        key = f"{method} /{endpoint.split('?')[0]}"

        if status >= 400:
            err_msgs = resp.get("validationMessages", [])
            err_detail = err_msgs[0].get("message", "") if err_msgs else resp.get("message", "")
            categories["ERRORS"].append(f"{key} → {status}: {err_detail[:80]}")
        elif method == "GET" and resp.get("fullResultSize", 1) == 0:
            categories["EMPTY_SEARCH"].append(f"{key} (no results)")
        elif method == "GET":
            categories["LOOKUPS"].append(key)
        elif method in ("POST", "PUT"):
            categories["MUTATIONS"].append(key)
        elif method == "DELETE":
            categories["MUTATIONS"].append(key)

    return dict(categories)


def extract_422_patterns(api_calls):
    """Extract specific 422 error patterns."""
    patterns = []
    for call in api_calls:
        if call.get("status") == 422:
            resp = call.get("response_body") or {}
            msgs = resp.get("validationMessages") or []
            for msg in msgs:
                field = msg.get("field", "")
                message = msg.get("message", "")
                endpoint = str(call.get("url", "")).split("/v2/")[-1].split("?")[0]
                method = call.get("method", "?")
                patterns.append(f"{method} /{endpoint}: [{field}] {message}")
    return patterns


def analyze_task_type(runs, task_type):
    """Deep analysis of a single task type across all runs."""
    task_runs = [r for r in runs if r.get("parsed_task", {}).get("task_type") == task_type]
    if not task_runs:
        return None

    tier = TASK_TIERS.get(task_type, "?")
    max_score = MAX_SCORE.get(tier, 0)

    # Separate by path
    deterministic = [r for r in task_runs if r.get("path_taken") == "deterministic"]
    repaired = [r for r in task_runs if r.get("path_taken") == "deterministic_repaired"]
    agent_loop = [r for r in task_runs if r.get("path_taken") == "agent_loop"]

    # Handler crash analysis
    crashes = [r for r in task_runs if r.get("deterministic_error")]
    crash_reasons = defaultdict(int)
    for r in crashes:
        err = r["deterministic_error"]
        # Normalize error messages
        if "'list' object" in err:
            crash_reasons["Parser returned list instead of dict"] += 1
        elif "422" in err and "Validering" in err:
            crash_reasons["Voucher 422 validation error"] += 1
        elif "422" in err and "mapping" in err:
            crash_reasons["422 Request mapping (bad field)"] += 1
        elif "not found" in err.lower():
            crash_reasons[f"Entity not found: {err[:60]}"] += 1
        else:
            crash_reasons[err[:80]] += 1

    # 422 error pattern analysis across ALL runs
    all_422_patterns = defaultdict(int)
    for r in task_runs:
        for pat in extract_422_patterns(r.get("api_calls", [])):
            all_422_patterns[pat] += 1

    # Call waste analysis (on deterministic runs only, or best run)
    best_run = None
    best_score = -1
    for r in task_runs:
        calls = r.get("total_api_calls", 999)
        errors = r.get("errors_4xx", 999)
        score = -calls - errors * 10
        if r.get("path_taken") == "deterministic":
            score += 100  # Prefer deterministic
        if score > best_score:
            best_score = score
            best_run = r

    call_breakdown = analyze_call_waste(best_run.get("api_calls", [])) if best_run else {}

    # Timing analysis
    parse_times = [r.get("phase_times", {}).get("parse", 0) for r in task_runs]
    handler_times = [r.get("phase_times", {}).get("handler", 0) for r in task_runs]
    loop_times = [r.get("phase_times", {}).get("agent_loop", 0) for r in agent_loop]

    # Entity extraction issues
    entity_issues = []
    for r in task_runs:
        ents = r.get("parsed_task", {}).get("entities")
        if isinstance(ents, list):
            entity_issues.append("entities returned as LIST (should be dict)")
        elif isinstance(ents, dict):
            empty_keys = [k for k, v in ents.items() if v is None or v == "" or v == []]
            if empty_keys:
                entity_issues.append(f"Empty fields: {', '.join(empty_keys[:5])}")

    return {
        "task_type": task_type,
        "tier": tier,
        "max_score": max_score,
        "total_runs": len(task_runs),
        "deterministic_runs": len(deterministic),
        "repaired_runs": len(repaired),
        "agent_loop_runs": len(agent_loop),
        "crash_count": len(crashes),
        "crash_reasons": dict(crash_reasons),
        "all_422_patterns": dict(all_422_patterns),
        "avg_calls": round(sum(r.get("total_api_calls", 0) for r in task_runs) / len(task_runs), 1),
        "min_calls": min(r.get("total_api_calls", 999) for r in task_runs),
        "max_calls": max(r.get("total_api_calls", 0) for r in task_runs),
        "avg_errors": round(sum(r.get("errors_4xx", 0) for r in task_runs) / len(task_runs), 1),
        "best_run_calls": call_breakdown,
        "avg_parse_time": round(sum(parse_times) / len(parse_times), 2) if parse_times else 0,
        "avg_handler_time": round(sum(handler_times) / len(handler_times), 2) if handler_times else 0,
        "avg_loop_time": round(sum(loop_times) / len(loop_times), 1) if loop_times else 0,
        "entity_issues": list(set(entity_issues))[:5],
        "sample_prompts": [r.get("prompt", "")[:120] for r in task_runs[:3]],
    }


def print_task_analysis(analysis):
    tier = analysis["tier"]
    max_s = analysis["max_score"]
    tt = analysis["task_type"]
    total = analysis["total_runs"]
    det = analysis["deterministic_runs"]
    rep = analysis["repaired_runs"]
    loop = analysis["agent_loop_runs"]

    det_pct = round(det / total * 100) if total else 0
    loop_pct = round(loop / total * 100) if total else 0

    print(f"\n{'='*70}")
    print(f"  {tt}  (T{tier}, max {max_s}pts, {total} runs)")
    print(f"{'='*70}")

    # Path distribution
    print(f"\n  Path: deterministic={det} ({det_pct}%) | repaired={rep} | agent_loop={loop} ({loop_pct}%)")
    print(f"  Calls: avg={analysis['avg_calls']} min={analysis['min_calls']} max={analysis['max_calls']} | Errors avg={analysis['avg_errors']}")
    print(f"  Timing: parse={analysis['avg_parse_time']}s handler={analysis['avg_handler_time']}s loop={analysis['avg_loop_time']}s")

    # Crash reasons (the money shot)
    if analysis["crash_reasons"]:
        print(f"\n  HANDLER CRASHES ({analysis['crash_count']}x):")
        for reason, count in sorted(analysis["crash_reasons"].items(), key=lambda x: -x[1]):
            print(f"    [{count}x] {reason}")

    # 422 patterns
    if analysis["all_422_patterns"]:
        print(f"\n  422 VALIDATION ERRORS:")
        for pat, count in sorted(analysis["all_422_patterns"].items(), key=lambda x: -x[1])[:5]:
            print(f"    [{count}x] {pat}")

    # Call breakdown for best run
    if analysis["best_run_calls"]:
        print(f"\n  BEST RUN CALL BREAKDOWN:")
        for category, calls in analysis["best_run_calls"].items():
            print(f"    {category} ({len(calls)}):")
            # Show unique calls with counts
            call_counts = defaultdict(int)
            for c in calls:
                call_counts[c] += 1
            for c, n in sorted(call_counts.items(), key=lambda x: -x[1])[:5]:
                prefix = f"      {n}x " if n > 1 else "      "
                print(f"{prefix}{c}")

    # Entity issues
    if analysis["entity_issues"]:
        print(f"\n  PARSER ISSUES:")
        for issue in analysis["entity_issues"]:
            print(f"    - {issue}")

    # Actionable recommendations
    print(f"\n  RECOMMENDATIONS:")
    if analysis["crash_reasons"]:
        print(f"    1. FIX CRASHES: {list(analysis['crash_reasons'].keys())[0]}")
    if loop_pct > 30:
        print(f"    2. {loop_pct}% of runs fall to agent_loop — fix handler to stay deterministic")
    if analysis["avg_errors"] > 1:
        print(f"    3. Avg {analysis['avg_errors']} errors/run — reduce 4xx errors for efficiency bonus")
    if analysis["avg_calls"] > analysis["min_calls"] * 1.5:
        print(f"    4. Avg {analysis['avg_calls']} calls but best is {analysis['min_calls']} — optimize call count")


def main():
    args = sys.argv[1:]
    show_all_tiers = "--all" in args or "-a" in args
    task_filter = None
    for i, a in enumerate(args):
        if a == "--task" and i + 1 < len(args):
            task_filter = args[i + 1]

    runs = load_runs()
    if not runs:
        print("No run_*.json files found in run_logs/")
        return

    # Get all task types seen
    task_types = set()
    for r in runs:
        tt = r.get("parsed_task", {}).get("task_type", "unknown")
        task_types.add(tt)

    # Filter
    if task_filter:
        task_types = {t for t in task_types if task_filter in t}
    elif not show_all_tiers:
        task_types = {t for t in task_types if TASK_TIERS.get(t, 0) in (2, 3)}

    print(f"Analyzing {len(runs)} runs across {len(task_types)} task types...")

    # Sort by tier (highest first), then by name
    sorted_types = sorted(task_types, key=lambda t: (-TASK_TIERS.get(t, 0), t))

    # Summary table first
    print(f"\n{'─'*70}")
    print(f"  BOTTLENECK SUMMARY (T2+T3 = score multiplied tasks)")
    print(f"{'─'*70}")
    print(f"  {'Task':<30} {'Tier':>4} {'Runs':>5} {'Det%':>5} {'Crash':>6} {'AvgErr':>7} {'AvgCall':>8} {'MinCall':>8}")
    print(f"  {'─'*30} {'─'*4} {'─'*5} {'─'*5} {'─'*6} {'─'*7} {'─'*8} {'─'*8}")

    analyses = {}
    for tt in sorted_types:
        a = analyze_task_type(runs, tt)
        if a:
            analyses[tt] = a
            det_pct = round(a["deterministic_runs"] / a["total_runs"] * 100) if a["total_runs"] else 0
            flag = " ←FIX" if a["crash_count"] > 0 or a["agent_loop_runs"] > a["total_runs"] * 0.3 else ""
            print(f"  {tt:<30} T{a['tier']:>3} {a['total_runs']:>5} {det_pct:>4}% {a['crash_count']:>6} {a['avg_errors']:>7} {a['avg_calls']:>8} {a['min_calls']:>8}{flag}")

    # Detailed analysis for each
    for tt in sorted_types:
        if tt in analyses:
            print_task_analysis(analyses[tt])


if __name__ == "__main__":
    main()
