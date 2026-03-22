#!/usr/bin/env python3
"""
check_critical_tasks.py — Analyze the 8 previously-zero-score tasks
from the latest 25 runs to see if they're actually working now.

Tasks checked:
  annual_closure, run_payroll, bank_reconciliation, error_correction,
  fx_correction, project_lifecycle, overdue_invoice, cost_analysis
"""

import json
import glob
import os
import sys
from pathlib import Path
from datetime import datetime

RUN_DIR = Path(__file__).parent / "run_logs"

TARGET_TASKS = [
    "annual_closure",
    "run_payroll",
    "bank_reconciliation",
    "error_correction",
    "fx_correction",
    "project_lifecycle",
    "overdue_invoice",
    "cost_analysis",
]

# ── Success heuristics per task ──────────────────────────────────────────────
# Each returns (ok: bool, detail: str) given the run dict.

def _check_annual_closure(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if "vouchers created" in r.lower() and d.get("errors_4xx", 0) == 0:
        # extract count
        for tok in r.split():
            if tok.isdigit():
                return True, f"{tok} vouchers created, 0 errors"
        return True, "vouchers created, 0 errors"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"result={r[:80]}"


def _check_run_payroll(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if "salary transaction" in r.lower() and d.get("errors_4xx", 0) == 0:
        return True, "payroll OK, 0 errors"
    if "max iterations" in r.lower():
        return False, f"MAX ITER, {d.get('errors_4xx',0)} 4xx errors"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"4xx={d.get('errors_4xx',0)}, result={r[:60]}"


def _check_bank_reconciliation(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if "payments matched" in r.lower() and d.get("errors_4xx", 0) == 0:
        # extract matched count
        for i, tok in enumerate(r.split()):
            if tok.isdigit() and i < 10:
                return True, f"{tok} payments matched, 0 errors"
        return True, "payments matched, 0 errors"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"result={r[:80]}"


def _check_error_correction(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if "corrections" in r.lower() and d.get("errors_4xx", 0) == 0:
        return True, "corrections posted, 0 errors"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"result={r[:80]}"


def _check_fx_correction(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if ("agiogevinst" in r.lower() or "agiotap" in r.lower()) and d.get("errors_4xx", 0) == 0:
        return True, "FX voucher posted, 0 errors"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"result={r[:80]}"


def _check_project_lifecycle(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    err = d.get("deterministic_error") or ""
    if "max iterations" in r.lower():
        return False, f"MAX ITER — det_err: {err[:60]}"
    if "int()" in err or "None" in err:
        return False, f"CRASH: {err[:80]}"
    if d.get("errors_4xx", 0) > 2:
        return False, f"{d['errors_4xx']} 4xx errors, path={d.get('path_taken','?')}"
    if d.get("path_taken") == "deterministic" and d.get("errors_4xx", 0) == 0:
        return True, f"deterministic OK, 0 errors"
    if "project" in r.lower() and ("created" in r.lower() or "completed" in r.lower()):
        return True, f"completed via {d.get('path_taken','?')}"
    return False, f"path={d.get('path_taken','?')}, 4xx={d.get('errors_4xx',0)}, result={r[:50]}"


def _check_overdue_invoice(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    if "late fee" in r.lower() and d.get("errors_4xx", 0) == 0:
        return True, "late fee + invoice created, 0 errors"
    if "422" in r:
        return False, f"422 error: {r[:80]}"
    if d.get("deterministic_error"):
        return False, f"CRASH: {d['deterministic_error'][:80]}"
    return False, f"result={r[:80]}"


def _check_cost_analysis(d: dict) -> tuple[bool, str]:
    r = d.get("result") or ""
    err = d.get("deterministic_error") or ""
    e4 = d.get("errors_4xx", 0)
    if "top 0 accounts" in r.lower():
        return False, "found 0 accounts — logic/auth failure"
    if "tilgang" in r.lower() or "access" in err.lower():
        return False, f"AUTH error: {err[:60] or r[:60]}"
    if e4 >= 3:
        return False, f"{e4} 4xx errors — likely auth/logic failure"
    if "cost" in r.lower() and ("account" in r.lower() or "top" in r.lower()):
        return True, f"analysis completed, {e4} errors"
    if d.get("path_taken") == "deterministic" and e4 == 0 and r:
        return True, f"deterministic OK: {r[:60]}"
    return False, f"4xx={e4}, result={r[:60]}"


CHECKERS = {
    "annual_closure": _check_annual_closure,
    "run_payroll": _check_run_payroll,
    "bank_reconciliation": _check_bank_reconciliation,
    "error_correction": _check_error_correction,
    "fx_correction": _check_fx_correction,
    "project_lifecycle": _check_project_lifecycle,
    "overdue_invoice": _check_overdue_invoice,
    "cost_analysis": _check_cost_analysis,
}


def load_runs(n: int = 25) -> list[dict]:
    """Load last n runs that match target tasks, scanning up to 200 files."""
    files = sorted(glob.glob(str(RUN_DIR / "run_*.json")), key=os.path.getmtime, reverse=True)
    runs = []
    for f in files[:200]:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        tt = d.get("parsed_task", {}).get("task_type", "")
        if tt in CHECKERS:
            d["_filename"] = os.path.basename(f)
            d["_task_type"] = tt
            runs.append(d)
        if len(runs) >= n:
            break
    return runs


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    runs = load_runs(n)

    if not runs:
        print("No matching runs found!")
        return

    # ── Group by task ────────────────────────────────────────────────────────
    by_task: dict[str, list[dict]] = {t: [] for t in TARGET_TASKS}
    for r in runs:
        by_task[r["_task_type"]].append(r)

    # ── Header ───────────────────────────────────────────────────────────────
    print()
    print("=" * 110)
    print(f"  CRITICAL TASK HEALTH CHECK — latest {n} target-task runs")
    print(f"  Scanned at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 110)

    # ── Per-task detail ──────────────────────────────────────────────────────
    summary_rows = []

    for task in TARGET_TASKS:
        task_runs = by_task[task]
        if not task_runs:
            summary_rows.append((task, 0, 0, 0, "NO RUNS FOUND"))
            continue

        print(f"\n{'─' * 110}")
        print(f"  {task.upper()}  ({len(task_runs)} runs)")
        print(f"{'─' * 110}")
        print(f"  {'File':<38s}  {'Path':<22s}  {'4xx':>3s}  {'OK?':<4s}  Detail")
        print(f"  {'─'*37:<38s}  {'─'*21:<22s}  {'─'*3:>3s}  {'─'*3:<4s}  {'─'*40}")

        ok_count = 0
        fail_count = 0
        for r in task_runs:
            checker = CHECKERS[task]
            ok, detail = checker(r)
            status = "YES" if ok else "NO"
            mark = "✓" if ok else "✗"
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            path = r.get("path_taken", "?")
            e4 = r.get("errors_4xx", 0)
            print(f"  {r['_filename']:<38s}  {path:<22s}  {e4:>3d}  {mark:<4s}  {detail}")

        pct = ok_count / len(task_runs) * 100 if task_runs else 0
        verdict = "WORKING" if pct >= 80 else ("FLAKY" if pct >= 40 else "BROKEN")
        summary_rows.append((task, len(task_runs), ok_count, fail_count, verdict))

    # ── Summary table ────────────────────────────────────────────────────────
    print(f"\n\n{'=' * 90}")
    print("  SUMMARY")
    print(f"{'=' * 90}")
    print(f"  {'Task':<25s}  {'Runs':>5s}  {'OK':>4s}  {'Fail':>4s}  {'Rate':>6s}  {'Verdict':<10s}")
    print(f"  {'─'*24:<25s}  {'─'*4:>5s}  {'─'*3:>4s}  {'─'*3:>4s}  {'─'*5:>6s}  {'─'*9:<10s}")

    all_ok = 0
    all_fail = 0
    for task, total, ok, fail, verdict in summary_rows:
        rate = f"{ok/total*100:.0f}%" if total > 0 else "N/A"
        color = "✓" if verdict == "WORKING" else ("~" if verdict == "FLAKY" else "✗")
        print(f"  {task:<25s}  {total:>5d}  {ok:>4d}  {fail:>4d}  {rate:>6s}  {color} {verdict}")
        all_ok += ok
        all_fail += fail

    total = all_ok + all_fail
    print(f"  {'─'*24:<25s}  {'─'*4:>5s}  {'─'*3:>4s}  {'─'*3:>4s}  {'─'*5:>6s}  {'─'*9:<10s}")
    rate = f"{all_ok/total*100:.0f}%" if total > 0 else "N/A"
    print(f"  {'TOTAL':<25s}  {total:>5d}  {all_ok:>4d}  {all_fail:>4d}  {rate:>6s}")
    print()

    # ── Actionable notes ─────────────────────────────────────────────────────
    broken = [t for t, _, _, _, v in summary_rows if v == "BROKEN"]
    flaky = [t for t, _, _, _, v in summary_rows if v == "FLAKY"]
    if broken:
        print(f"  ⚠  BROKEN tasks needing fixes: {', '.join(broken)}")
    if flaky:
        print(f"  ⚡ FLAKY tasks (intermittent): {', '.join(flaky)}")
    if not broken and not flaky:
        print(f"  ✓  All 8 critical tasks are WORKING!")
    print()


if __name__ == "__main__":
    main()
