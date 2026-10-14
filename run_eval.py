"""Run the eval scenarios against the agent and report scores.

    uv run python run_eval.py                    # all 21 scenarios, active prompt version
    uv run python run_eval.py --split train      # only the 15 training scenarios
    uv run python run_eval.py --only S01,S10     # specific scenarios
    uv run python run_eval.py --version 2        # a specific prompt version
    uv run python run_eval.py --rescore runs/eval-...   # re-grade saved runs with the current checks
                                                        # (no conversations replayed; judge is re-asked)

Each scenario runs once, with temperature 0 and a fixed seed for agent, patient and judge.
Results go to runs/<name>/ : one JSON per scenario + summary.json.

Reported per split (train / holdout / overall):
    db, trace, text, judge   group scores (fraction of checks passed, averaged over scenarios)
    weighted                 average of the 4 groups (25% each)
    pass rate                scenarios where every critical check passed
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from clinic.prompt import active_version
from evals.harness import make_llm, print_report, run_parallel, run_version, summarize
from evals.runner import load_scenarios, regrade


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--split", choices=["train", "holdout"], default=None)
    ap.add_argument("--only", default=None, help="comma-separated scenario ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rescore", default=None, help="folder of a previous eval run to re-grade")
    args = ap.parse_args()

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    only = args.only.split(",") if args.only else None

    if args.rescore:
        src = Path(args.rescore)
        scenarios = {s["id"]: s for s in load_scenarios(args.split, only)}
        saved = [json.loads(p.read_text()) for p in sorted(src.glob("S*.json"))]
        saved = [r for r in saved if r["id"] in scenarios]
        version = saved[0].get("prompt_version") if saved else None
        out_dir = Path("runs") / f"rescore-{stamp}-v{version}"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"re-grading {len(saved)} saved runs from {src} with the current checks ...")

        def one(r):
            new = regrade(r, scenarios[r["id"]], make_llm("judge"))
            (out_dir / f"{r['id']}.json").write_text(json.dumps(new, indent=2))
            return new

        results = run_parallel(one, saved, args.workers)
        title = f"Re-scored prompt v{version} (conversations from {src.name})"
    else:
        version = args.version if args.version is not None else active_version()
        scenarios = load_scenarios(args.split, only)
        out_dir = Path("runs") / f"eval-{stamp}-v{version}"
        print(f"running {len(scenarios)} scenarios with prompt v{version} ...")
        results = run_version(version, scenarios, out_dir, args.workers, log=lambda m: print(m, flush=True))
        title = f"Eval report: prompt v{version}"

    summary = summarize(results, roles=("rescore_judge",) if args.rescore else None)
    summary["prompt_version"] = version
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print_report(summary, title, out_dir)


if __name__ == "__main__":
    main()
