"""The improvement loop: eval -> fixer proposes one rule change -> new prompt version -> re-eval -> keep or reject.

    uv run python run_improve.py --rounds 3
    uv run python run_improve.py --rounds 3 --baseline results/v1_baseline

Each round:
  1. The fixer reads the current version's TRAIN failures (never the held-out ones) and proposes ONE change.
  2. Code validates it (allowed op, locked rules untouched) and writes prompts/v<N>.yaml.
  3. All 21 scenarios are re-run with the new version.
  4. Any scenario that passed before and fails now is re-run once, to separate real regressions from noise.
  5. ACCEPT only if there is no confirmed regression (train or held-out) and the TRAIN weighted score goes up.
     Accepted -> prompts/active.txt points to it and the next round builds on it.
     Rejected -> marked rejected; the next round builds on the last accepted version, with the feedback.
Every decision is appended to prompts/changelog.jsonl and results go to results/v<N>/.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from clinic.prompt import PROMPTS_DIR, load_prompt_spec
from evals import fixer
from evals.harness import make_llm, print_comparison, print_report, run_version, summarize
from evals.runner import load_scenarios

RESULTS = Path("results")
CHANGELOG = PROMPTS_DIR / "changelog.jsonl"


def log(msg=""):
    print(msg, flush=True)


def load_results(folder: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(folder.glob("S*.json"))]


def history_from_changelog() -> list[dict]:
    """Rebuild what the fixer was told in earlier rounds, so a resumed run keeps that context."""
    if not CHANGELOG.exists():
        return []
    out = []
    for line in CHANGELOG.read_text().splitlines():
        e = json.loads(line)
        prop = e.get("proposal") or {}
        change = prop.get("change") or e.get("change") or {"op": "none"}
        out.append({"version": e["version"], "change": change, "accepted": e["accepted"],
                    "feedback": e.get("feedback") or e.get("reason", "")})
    return out


def next_version() -> int:
    return max(int(p.stem[1:]) for p in PROMPTS_DIR.glob("v*.yaml")) + 1


def decide(old: list[dict], new: list[dict], confirmed_regressions: list[str]) -> tuple[bool, str]:
    """The acceptance rule. Returns (accepted, reason)."""
    old_s, new_s = summarize(old)["scores"], summarize(new)["scores"]
    if confirmed_regressions:
        return False, f"regressions in {len(confirmed_regressions)} scenario(s) that passed before"
    if new_s["train"]["weighted"] <= old_s["train"]["weighted"]:
        return False, (f"train weighted score did not improve "
                       f"({old_s['train']['weighted']:.1%} -> {new_s['train']['weighted']:.1%})")
    return True, (f"train weighted {old_s['train']['weighted']:.1%} -> {new_s['train']['weighted']:.1%}, "
                  f"no regressions")


def fixer_feedback(old: list[dict], new: list[dict], accepted: bool, reason: str) -> str:
    """What the fixer is told about the result. Train details only; held-out is reported only as a count."""
    old_by = {r["id"]: r for r in old}
    fixed = [r["id"] for r in new if r["split"] == "train" and r["passed"] and not old_by[r["id"]]["passed"]]
    broke = [r["id"] for r in new if r["split"] == "train" and not r["passed"] and old_by[r["id"]]["passed"]]
    hidden_broke = sum(1 for r in new if r["split"] == "holdout" and not r["passed"] and old_by[r["id"]]["passed"])
    s = f"{reason}. Train scenarios newly passing: {fixed or 'none'}; newly failing: {broke or 'none'}."
    if hidden_broke:
        s += f" {hidden_broke} hidden scenario(s) also got worse."
    return s


def round_once(rnd: int, cur_v: int, cur_results: list[dict], history: list[dict], scenarios: list[dict],
               workers: int) -> tuple[int, list[dict]]:
    cand_v = next_version()
    log(f"\n{'=' * 70}\nROUND {rnd}: fixer reads v{cur_v} train failures, proposes v{cand_v}\n{'=' * 70}")

    llm = make_llm("fixer")
    proposal, problem, fixer_prompt = fixer.propose(llm, cur_v, cur_results, history)
    out_dir = RESULTS / f"v{cand_v}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fixer_input.md").write_text(fixer_prompt)
    fixer_cost = sum((c.get("usage") or {}).get("cost", 0) or 0 for c in llm.calls)

    if proposal is None:
        log(f"fixer gave no valid proposal: {problem}")
        entry = {"version": cand_v, "parent": cur_v, "accepted": False, "change": {"op": "none"},
                 "feedback": f"no valid proposal: {problem}"}
        history.append(entry)
        _append_changelog({**entry, "time": datetime.now().isoformat(timespec="seconds")})
        return cur_v, cur_results

    ch = proposal["change"]
    log(f"root cause : {proposal.get('root_cause')}")
    log(f"change     : {ch['op']} {ch.get('rule_id')}")
    log(f"text       : {ch.get('text')}")
    log(f"targets    : {proposal.get('target_checks')}  expected to fix: {proposal.get('expected_to_fix')}")
    log(f"risk       : {proposal.get('risk')}")

    spec = fixer.apply(load_prompt_spec(cur_v), proposal, cand_v)
    fixer.write_spec(spec)
    log(f"wrote prompts/v{cand_v}.yaml  (fixer cost ${fixer_cost:.4f})")

    log(f"\nre-running all {len(scenarios)} scenarios with v{cand_v} ...")
    run_dir = Path("runs") / f"eval-{datetime.now():%Y%m%d-%H%M%S}-v{cand_v}"
    new_results = run_version(cand_v, scenarios, run_dir, workers, log=log)

    # A scenario that could not run (API refused, out of credit, network) says nothing about the agent.
    # Never score such a round: stop, leave the candidate undecided, and say why.
    errored = [r for r in new_results if r["status"] == "error"]
    if errored:
        fixer.set_status(cand_v, "incomplete")
        log(f"\nROUND ABORTED: {len(errored)} scenario(s) could not run, so v{cand_v} is NOT scored or decided.")
        log(f"first error: {errored[0].get('error', '')[:300]}")
        raise SystemExit(2)

    # Re-run scenarios that newly fail once: a single run per scenario can flip on noise alone.
    old_by = {r["id"]: r for r in cur_results}
    suspects = [r["id"] for r in new_results if old_by[r["id"]]["passed"] and not r["passed"]]
    confirmed = []
    if suspects:
        log(f"\nnewly failing {suspects}: re-running once to confirm ...")
        rerun = run_version(cand_v, [s for s in scenarios if s["id"] in suspects], run_dir / "rerun", workers, log=log)
        rerun_by = {r["id"]: r for r in rerun}
        if any(r["status"] == "error" for r in rerun):
            fixer.set_status(cand_v, "incomplete")
            log(f"\nROUND ABORTED: re-run could not complete, so v{cand_v} is NOT decided.")
            raise SystemExit(2)
        confirmed = [sid for sid in suspects if not rerun_by[sid]["passed"]]
        flaky = [sid for sid in suspects if sid not in confirmed]
        if flaky:
            log(f"passed on re-run (noise, counted as pass): {flaky}")
            new_results = [rerun_by[r["id"]] if r["id"] in flaky else r for r in new_results]
        if confirmed:
            log(f"confirmed regressions: {confirmed}")

    accepted, reason = decide(cur_results, new_results, confirmed)
    old_sum, new_sum = summarize(cur_results), summarize(new_results)
    print_report(new_sum, f"v{cand_v} results", run_dir, log=log)
    log(f"\n--- v{cur_v} vs v{cand_v} ---")
    print_comparison(old_sum, new_sum, cur_v, cand_v, log=log)
    log(f"\nDECISION v{cand_v}: {'ACCEPTED' if accepted else 'REJECTED'} - {reason}")

    for r in new_results:
        (out_dir / f"{r['id']}.json").write_text(json.dumps(r, indent=2))
    (out_dir / "summary.json").write_text(json.dumps({**new_sum, "prompt_version": cand_v}, indent=2))
    decision = {"version": cand_v, "parent": cur_v, "accepted": accepted, "reason": reason,
                "proposal": proposal, "confirmed_regressions": confirmed,
                "scores_before": old_sum["scores"], "scores_after": new_sum["scores"],
                "fixer_cost": fixer_cost, "eval_cost": new_sum["usage"]["cost"]}
    feedback = fixer_feedback(cur_results, new_results, accepted, reason)
    decision["feedback"] = feedback
    (out_dir / "decision.json").write_text(json.dumps(decision, indent=2))
    _append_changelog({**{k: v for k, v in decision.items() if k not in ("scores_before", "scores_after")},
                       "weighted_train": [old_sum["scores"]["train"]["weighted"], new_sum["scores"]["train"]["weighted"]],
                       "pass_overall": [old_sum["scores"]["overall"]["passed"], new_sum["scores"]["overall"]["passed"]],
                       "time": datetime.now().isoformat(timespec="seconds")})

    history.append({"version": cand_v, "change": spec["change"], "accepted": accepted, "feedback": feedback})
    fixer.set_status(cand_v, "accepted" if accepted else "rejected")
    if accepted:
        (PROMPTS_DIR / "active.txt").write_text(f"{cand_v}\n")
        return cand_v, new_results
    return cur_v, cur_results


def _append_changelog(entry: dict):
    with CHANGELOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--baseline", default="results/v1_baseline", help="graded results of the starting version")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    cur_results = load_results(Path(args.baseline))
    if not cur_results:
        sys.exit(f"no results in {args.baseline}")
    cur_v = cur_results[0]["prompt_version"]
    scenarios = load_scenarios()
    log(f"starting from v{cur_v} ({args.baseline}); {args.rounds} rounds")
    print_report(summarize(cur_results), f"starting point v{cur_v}", log=log)

    history = history_from_changelog()
    if history:
        log(f"resuming: {len(history)} earlier round(s) loaded from {CHANGELOG}")
    start_v, start_results = cur_v, cur_results
    for rnd in range(1, args.rounds + 1):
        cur_v, cur_results = round_once(rnd, cur_v, cur_results, history, scenarios, args.workers)

    log(f"\n{'=' * 70}\nDONE. Best version: v{cur_v} (started from v{start_v})\n{'=' * 70}")
    print_comparison(summarize(start_results), summarize(cur_results), start_v, cur_v, log=log)
    for h in history:
        log(f"  v{h['version']}: {'ACCEPTED' if h['accepted'] else 'REJECTED'}  {h['change'].get('op')} "
            f"{h['change'].get('rule_id', '')}  - {h['feedback'][:150]}")


if __name__ == "__main__":
    main()
