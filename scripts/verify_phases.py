"""Per-phase build verification: after every build, prove each completed phase still works.

    uv run python scripts/verify_phases.py            # fast: deterministic checks + golden set with MockLLM
    uv run python scripts/verify_phases.py --live     # also runs the golden set against the real model
    uv run python scripts/verify_phases.py --only P2  # a single phase

A failure is reported against the phase that owns it, so a regression points straight at its cause.
Phases that are not built yet are reported as SKIP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")


def pytest(*paths: str) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    last = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or ["no output"]
    return r.returncode == 0, last[0]


def p0() -> tuple[bool, str]:
    from scripts.seed import seed

    with tempfile.TemporaryDirectory() as d:
        c = seed(Path(d) / "p0.db", None)
    ok = c["patients"] == 30 and c["doctors"] == 8 and c["appointments"] == 20 and c["prescriptions"] == 40 and c["care_leaflets"] == 15 and c["policy_docs"] == 8 and c["medlineplus_docs"] >= 20
    ok2, msg = pytest("tests/test_seed.py")
    return ok and ok2, f"seed counts {'OK' if ok else c} | {msg}"


def p1() -> tuple[bool, str]:
    return pytest("tests/test_tools.py")


def p2() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "scripts/rag_smoke.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok, msg = pytest("tests/test_rag.py")
    tail = [ln for ln in r.stdout.splitlines() if "queries returned" in ln]
    return r.returncode == 0 and ok, f"{tail[0] if tail else 'smoke failed'} | {msg}"


def golden(live: bool) -> tuple[bool, str, str]:
    from careharbor.evals.golden import load_cases, run_cases, summarise

    factory = None
    if not live:
        from careharbor.llm import MockLLM

        mock = MockLLM({"router": lambda s, u: json.dumps({"intent": "out_of_scope"}), "safety": lambda s, u: '{"emergency": false}'})
        factory = lambda m=None: mock  # noqa: E731
    results = asyncio.run(run_cases(load_cases(), llm_factory=factory))
    passed = sum(c.ok for c in results)
    return passed == len(results), f"golden {passed}/{len(results)} ({'live LLM' if live else 'MockLLM'})", summarise(results)


def p3(live: bool) -> tuple[bool, str]:
    ok, msg = pytest("tests/test_graph.py")
    gok, gmsg, detail = golden(live)
    if not gok:
        print(detail)
    return ok and gok, f"{msg} | {gmsg}"


def p4() -> tuple[bool, str]:
    return pytest("tests/test_redteam.py")


def http_ok(url: str) -> bool:
    r = subprocess.run(["curl.exe", "-s", "-o", os.devnull, "-w", "%{http_code}", "--max-time", "5", url], capture_output=True, text=True)
    return r.stdout.strip() == "200"


def p5() -> tuple[bool, str] | None:
    if not (ROOT / "frontend" / "package.json").exists():
        return None
    ok, msg = pytest("tests/test_api.py")
    api, ui = http_ok("http://127.0.0.1:8010/api/health"), http_ok("http://127.0.0.1:5174/")
    live = f"API 8010 {'up' if api else 'down'}, UI 5174 {'up' if ui else 'down'}"
    return ok, f"{msg} | {live}"


def p6() -> tuple[bool, str] | None:
    report = ROOT / "evals" / "reports" / "timing_report.md"
    if not report.exists():
        return None
    ok, msg = pytest("tests/test_cache.py")
    return ok, f"{msg} | timing report present"


def p7() -> tuple[bool, str] | None:
    if not (ROOT / "scripts" / "run_evals.py").exists():
        return None
    r = subprocess.run([sys.executable, "scripts/run_evals.py", "--mock", "--quiet"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in r.stdout.splitlines() if ln.strip()][-1:] or ["no output"]
    return r.returncode == 0, tail[0]


def p8() -> tuple[bool, str] | None:
    needed = ["setup.ps1", "run.ps1", "scripts/make_portable_zip.ps1", "Dockerfile", "docs/PERSONAL_LAPTOP_SETUP.md"]
    if not (ROOT / "setup.ps1").exists():
        return None
    missing = [n for n in needed if not (ROOT / n).exists()]
    return not missing, "all portability files present" if not missing else f"missing {missing}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    phases = {"P0": p0, "P1": p1, "P2": p2, "P3": lambda: p3(args.live), "P4": p4, "P5": p5, "P6": p6, "P7": p7, "P8": p8}
    rows, all_ok = [], True
    for name, fn in phases.items():
        if args.only and name != args.only:
            continue
        t0 = time.perf_counter()
        res = fn()
        secs = time.perf_counter() - t0
        if res is None:
            rows.append((name, "SKIP", "not built yet", secs))
            continue
        ok, msg = res
        all_ok &= ok
        rows.append((name, "PASS" if ok else "FAIL", msg, secs))
        print(f"{name} {'PASS' if ok else 'FAIL'} ({secs:.1f}s) {msg}", flush=True)
    print("\nPhase  Status  Time    Details")
    for name, status, msg, secs in rows:
        print(f"{name:<6} {status:<7} {secs:5.1f}s  {msg}")
    print("\nALL BUILT PHASES PASS" if all_ok else "\nSOME PHASES FAILED - see above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
