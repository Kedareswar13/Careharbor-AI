"""Run CareHarbor AI's eval suites and write a report (CI gate: exits non-zero if any threshold is missed).

    uv run python scripts/run_evals.py                 # live model (Ollama)
    uv run python scripts/run_evals.py --mock          # deterministic MockLLM (CI without a model)
    uv run python scripts/run_evals.py --suite faq --suite triage
    uv run python scripts/run_evals.py --golden        # also run the golden regression set

Reports: evals/reports/eval_report_<timestamp>.md and eval_summary_<timestamp>.json (shown in the staff console).
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYTHONUTF8", "1")

from careharbor.config import ROOT_DIR, get_settings  # noqa: E402
from careharbor.evals.suites import SUITES, Harness, SuiteResult  # noqa: E402
from careharbor.logging_setup import setup_logging  # noqa: E402

REPORTS = ROOT_DIR / "evals" / "reports"


def mock_factory():
    from careharbor.llm import MockLLM

    mock = MockLLM({"router": lambda s, u: json.dumps({"intent": "out_of_scope", "emergency": False}), "safety": lambda s, u: '{"emergency": false}'})
    return lambda m=None: mock


def render(results: list[SuiteResult], model: str, secs: float, golden: str | None) -> str:
    ok = all(r.passed for r in results)
    lines = [
        "# CareHarbor AI eval report", "",
        f"- Generated: {datetime.now():%Y-%m-%d %H:%M}", f"- Model: `{model}`", f"- Duration: {secs / 60:.1f} min",
        f"- Overall: **{'ALL THRESHOLDS MET' if ok else 'THRESHOLD MISSED'}**", "",
        "| Suite | Cases | Metric | Score | Target | Result |", "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.name} | {r.n} | {r.metric} | {r.score_display} | {r.target_display} | {'PASS' if r.passed else 'FAIL'} |")
    for r in results:
        if r.extra:
            ablation = r.extra.get("ablation")
            plain = {k: v for k, v in r.extra.items() if k != "ablation"}
            lines += ["", f"**{r.name} - extra metrics:** " + ", ".join(
                f"{k} = {v:.0%}" if isinstance(v, float) and k != "seconds" else f"{k} = {v}" for k, v in plain.items())]
            if ablation:
                lines += ["", f"**{r.name} - ablation study**", "", "| Configuration | Hit@1 | Hit@3 | MRR |", "|---|---|---|---|"]
                lines += [f"| {cfg} | {m['hit@1']:.0%} | {m['hit@3']:.0%} | {m['mrr']:.3f} |" for cfg, m in ablation.items()]
    lines += ["", "## Failures and disagreements", ""]
    any_fail = False
    for r in results:
        for f in r.failures[:15]:
            any_fail = True
            lines.append(f"- **{r.name}**: `{json.dumps(f, ensure_ascii=False)[:300]}`")
    if not any_fail:
        lines.append("None.")
    if golden:
        lines += ["", "## Golden regression set", "", "```", golden, "```"]
    lines += ["", "Scores are computed by deterministic code (string, number, database and tool-call checks). No LLM acts as a judge."]
    return "\n".join(lines) + "\n"


async def run(names: list[str], mock: bool, golden: bool) -> tuple[list[SuiteResult], str | None]:
    factory = mock_factory() if mock else None
    results: list[SuiteResult] = []
    for name in names:
        h = Harness(factory)
        await h.svc.start()
        try:
            t0 = time.perf_counter()
            res = await SUITES[name](h)
            res.extra["seconds"] = round(time.perf_counter() - t0, 1)
            results.append(res)
            print(f"{'PASS' if res.passed else 'FAIL'}  {res.name:<26} {res.score_display:>6} (target {res.target_display})  n={res.n}  {res.extra['seconds']}s", flush=True)
        finally:
            await h.svc.stop()
            gc.collect()  # close throwaway SQLite connections so Windows releases the files
            shutil.rmtree(h.work, ignore_errors=True)  # each suite runs on a throwaway database
    gold = None
    if golden:
        from careharbor.evals.golden import load_cases, run_cases, summarise

        gold = summarise(await run_cases(load_cases(), llm_factory=factory))
    return results, gold


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--suite", action="append", choices=list(SUITES))
    ap.add_argument("--golden", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.mock:
        os.environ["LLM_PROVIDER"] = "mock"
        get_settings.cache_clear()
    else:
        from careharbor.llm import warm_up

        warm_up()
    t0 = time.perf_counter()
    results, gold = asyncio.run(run(args.suite or list(SUITES), args.mock, args.golden))
    secs = time.perf_counter() - t0
    model = "MockLLM" if args.mock else get_settings().llm_model
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"eval_report_{stamp}.md"
    report.write_text(render(results, model, secs, gold), encoding="utf-8")
    summary = {"generated": stamp, "model": model, "passed": all(r.passed for r in results),
               "suites": [{"name": r.name, "score": r.score, "target": r.target, "score_display": r.score_display, "target_display": r.target_display,
                           "passed": r.passed, "n": r.n, "extra": r.extra} for r in results]}
    (REPORTS / f"eval_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    ok = all(r.passed for r in results)
    print(f"{'ALL THRESHOLDS MET' if ok else 'THRESHOLD MISSED'} - report: {report.relative_to(ROOT_DIR)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
