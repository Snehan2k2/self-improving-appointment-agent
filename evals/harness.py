"""Shared eval plumbing: model setup, running a prompt version on scenarios, scoring, reporting.
Used by run_eval.py and run_improve.py."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clinic.llm import OpenRouterLLM
from evals.checks import GROUPS
from evals.runner import run_scenario

SEED = 7
SPLITS = ("train", "holdout", "overall")
ROW_LABELS = (("db", "Database"), ("trace", "Tool trace"), ("text", "Text (code)"),
              ("judge", "Judge (LLM)"), ("weighted", "Weighted"))


def make_llm(role: str) -> OpenRouterLLM:
    # The agent, judge and fixer think before answering; the patient just chats.
    # The fixer reads a long input and thinks for a while, so it gets a longer per-request timeout.
    timeout = 600 if role == "fixer" else 120
    return OpenRouterLLM(temperature=0, seed=SEED, reasoning=role != "patient", timeout=timeout)


def run_parallel(fn, items, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def run_version(version: int, scenarios: list[dict], out_dir: Path, workers: int = 4, log=print) -> list[dict]:
    """Play + grade every scenario with one prompt version; save one JSON per scenario."""
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(sc):
        r = run_scenario(sc, make_llm, version)
        (out_dir / f"{sc['id']}.json").write_text(json.dumps(r, indent=2))
        log(f"  {sc['id']} {r['status']} ({r['seconds']}s)")
        return r

    return run_parallel(one, scenarios, workers)


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


def _pct(v):
    return "  -  " if v is None else f"{v:>5.0%}"


def print_report(summary: dict, title: str, out_dir: Path | None = None, log=print):
    log(f"\n=== {title} ===")
    for r in summary["scenarios"]:
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[r["status"]]
        log(f"{r['id']}  {r['split']:<7}  {mark}  weighted {r['weighted_score']:.0%}")
        for f in r["failed_checks"]:
            log(f"      - {f[:170]}")
    s = summary["scores"]
    log(f"\n{'':<12}" + "".join(f"{k:>10}" for k in SPLITS))
    for g, label in ROW_LABELS:
        log(f"{label:<12}" + "".join(f"{_pct(s[k][g]):>10}" for k in SPLITS))
    log(f"{'Pass rate':<12}" + "".join(f"{s[k]['passed']:>6}/{s[k]['total']:<3}" for k in SPLITS))
    u = summary["usage"]
    log(f"\nllm calls {u['calls']}, tokens {u['prompt']}+{u['completion']}, cost ${u['cost']:.4f}")
    if out_dir:
        log(f"saved {out_dir}")


def print_comparison(old: dict, new: dict, old_v: int, new_v: int, log=print):
    """Side-by-side six numbers for two versions, per split."""
    so, sn = old["scores"], new["scores"]
    log(f"\n{'':<12}" + "".join(f"{f'{k} v{old_v}':>13}{f'v{new_v}':>7}" for k in SPLITS))
    for g, label in ROW_LABELS:
        log(f"{label:<12}" + "".join(f"{_pct(so[k][g]):>13}{_pct(sn[k][g]):>7}" for k in SPLITS))
    log(f"{'Pass rate':<12}" + "".join(
        f"{so[k]['passed']:>9}/{so[k]['total']:<3}{sn[k]['passed']:>4}/{sn[k]['total']:<2}" for k in SPLITS))
