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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from clinic.llm import OpenRouterLLM
from clinic.prompt import active_version
from evals.checks import GROUPS
from evals.runner import load_scenarios, regrade, run_scenario

SEED = 7
SPLITS = ("train", "holdout", "overall")


def make_llm(role: str) -> OpenRouterLLM:
    # The agent and judge think before answering; the patient just chats.
    return OpenRouterLLM(temperature=0, seed=SEED, reasoning=role != "patient")


def _mean(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 3) if xs else None


def split_scores(rows: list[dict]) -> dict:
    out = {g: _mean(r["group_scores"][g] for r in rows if g in r.get("group_scores", {})) for g in GROUPS}
    out["weighted"] = _mean(r.get("weighted_score", 0.0) for r in rows)
    out["passed"] = sum(r["passed"] for r in rows)
    out["total"] = len(rows)
    out["pass_rate"] = round(out["passed"] / len(rows), 3) if rows else None
    return out


def summarize(results: list[dict], roles: tuple | None = None) -> dict:
    """roles: only count model usage for these roles (e.g. just the re-score judge)."""
    usage = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
    for r in results:
        for role, calls in r.get("llm_calls", {}).items():
            if roles and role not in roles:
                continue
            for c in calls:
                u = c.get("usage") or {}
                usage["calls"] += 1
                usage["prompt"] += u.get("prompt_tokens", 0)
                usage["completion"] += u.get("completion_tokens", 0)
                usage["cost"] += u.get("cost", 0) or 0
    by_split = {"train": [r for r in results if r["split"] == "train"],
                "holdout": [r for r in results if r["split"] == "holdout"],
                "overall": results}
    return {
        "scores": {s: split_scores(rows) for s, rows in by_split.items()},
        "scenarios": [{"id": r["id"], "split": r["split"], "status": r["status"],
                       "weighted_score": round(r.get("weighted_score", 0.0), 3),
                       "group_scores": r.get("group_scores", {}),
                       "failed_checks": [f"[{c['group']}] {c['check']}: {c['detail']}" for c in r["checks"]
                                         if not c["passed"]]} for r in results],
        "usage": usage,
    }


def print_report(summary: dict, title: str, out_dir: Path):
    print(f"\n=== {title} ===")
    for r in summary["scenarios"]:
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[r["status"]]
        print(f"{r['id']}  {r['split']:<7}  {mark}  weighted {r['weighted_score']:.0%}")
        for f in r["failed_checks"]:
            print(f"      - {f[:170]}")

    s = summary["scores"]
    pct = lambda v: "  -  " if v is None else f"{v:>5.0%}"
    print(f"\n{'':<12}" + "".join(f"{k:>10}" for k in SPLITS))
    for g, label in (("db", "Database"), ("trace", "Tool trace"), ("text", "Text (code)"),
                     ("judge", "Judge (LLM)"), ("weighted", "Weighted")):
        print(f"{label:<12}" + "".join(f"{pct(s[k][g]):>10}" for k in SPLITS))
    print(f"{'Pass rate':<12}" + "".join(f"{s[k]['passed']:>6}/{s[k]['total']:<3}" for k in SPLITS))
    u = summary["usage"]
    print(f"\nllm calls {u['calls']}, tokens {u['prompt']}+{u['completion']}, cost ${u['cost']:.4f}")
    print(f"saved {out_dir}")


def run_parallel(fn, items, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


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
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"running {len(scenarios)} scenarios with prompt v{version} ...")

        def one(sc):
            r = run_scenario(sc, make_llm, version)
            (out_dir / f"{sc['id']}.json").write_text(json.dumps(r, indent=2))
            print(f"  {sc['id']} {r['status']} ({r['seconds']}s)", flush=True)
            return r

        results = run_parallel(one, scenarios, args.workers)
        title = f"Eval report: prompt v{version}"

    summary = summarize(results, roles=("rescore_judge",) if args.rescore else None)
    summary["prompt_version"] = version
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print_report(summary, title, out_dir)


if __name__ == "__main__":
    main()
