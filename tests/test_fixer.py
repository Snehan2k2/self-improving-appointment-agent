import json
from pathlib import Path

import pytest

from clinic.prompt import load_prompt_spec
from evals import fixer
from run_improve import decide, fixer_feedback

BASELINE = sorted(Path("results/v1_baseline").glob("S*.json"))
RESULTS = [json.loads(p.read_text()) for p in BASELINE]
HOLDOUT_IDS = [r["id"] for r in RESULTS if r["split"] == "holdout"]
SPEC = load_prompt_spec(1)


def prop(op, rule_id="NEW", text="Never show internal ids, tool names or your own reasoning to the patient."):
    return {"root_cause": "x", "change": {"op": op, "rule_id": rule_id, "text": text}}


# ---- the fixer only ever sees training scenarios ---------------------------------------

def test_fixer_input_has_no_holdout_scenarios():
    text = fixer.build_input(1, RESULTS, [])
    for sid in HOLDOUT_IDS:
        assert f"{sid}:" not in text and f"### {sid}" not in text
    assert "### S01" in text                 # failing train scenario shown with evidence
    assert "S11:" in text                    # passing train scenario listed as must-not-break
    assert "P1 [locked]" in text and "P3:" in text


def test_fixer_input_counts_train_only():
    text = fixer.build_input(1, RESULTS, [])
    assert "Passed 6/15" in text


def test_feedback_hides_holdout_ids():
    old = [{"id": "S01", "split": "train", "passed": False}, {"id": "S03", "split": "holdout", "passed": True}]
    new = [{"id": "S01", "split": "train", "passed": True}, {"id": "S03", "split": "holdout", "passed": False}]
    fb = fixer_feedback(old, new, False, "rejected")
    assert "S01" in fb and "S03" not in fb and "1 hidden scenario" in fb


# ---- validation -----------------------------------------------------------------------------

def test_valid_add():
    assert fixer.validate(prop("add_rule"), SPEC) is None


@pytest.mark.parametrize("proposal,problem", [
    (prop("edit_rule", "P1"), "locked"),
    (prop("delete_rule", "P6", ""), "locked"),
    (prop("edit_rule", "P99"), "does not exist"),
    (prop("rewrite_all"), "op must be"),
    (prop("add_rule", text="too short"), "20-600"),
    (prop("add_rule", text="x" * 601), "20-600"),
    (prop("add_rule", text="When the patient is like in S09, check their age first before offering."), "scenario ids"),
    (None, "JSON object"),
    ({"change": "add a rule"}, "JSON object"),
])
def test_invalid_proposals(proposal, problem):
    assert problem in fixer.validate(proposal, SPEC)


def test_edit_must_change_text():
    p3 = next(r for r in SPEC["rules"] if r["id"] == "P3")
    assert "does not change" in fixer.validate(prop("edit_rule", "P3", p3["text"]), SPEC)


def test_parse_proposal_tolerates_code_fences():
    raw = 'Here you go:\n```json\n{"change": {"op": "add_rule", "rule_id": "NEW", "text": "abc"}}\n```'
    assert fixer.parse_proposal(raw)["change"]["op"] == "add_rule"
    assert fixer.parse_proposal("no json here") is None


# ---- applying -------------------------------------------------------------------------------

def test_apply_add_assigns_next_id_and_keeps_locked():
    new = fixer.apply(SPEC, prop("add_rule"), 2)
    assert new["rules"][-1]["id"] == "P15" and new["rules"][-1]["locked"] is False
    assert new["version"] == 2 and new["parent"] == 1 and new["status"] == "candidate"
    assert [r for r in new["rules"] if r.get("locked")] == [r for r in SPEC["rules"] if r.get("locked")]
    assert len(SPEC["rules"]) == 14          # input spec untouched


def test_apply_edit_and_delete():
    edited = fixer.apply(SPEC, prop("edit_rule", "P10", "Short replies, one question at a time, plain words."), 2)
    assert next(r for r in edited["rules"] if r["id"] == "P10")["text"].startswith("Short replies")
    deleted = fixer.apply(SPEC, prop("delete_rule", "P14", ""), 2)
    assert "P14" not in {r["id"] for r in deleted["rules"]} and len(deleted["rules"]) == 13


def test_applied_spec_renders_into_a_prompt(tmp_path, monkeypatch):
    import clinic.prompt as cp
    from clinic.db import ClinicDB
    new = fixer.apply(SPEC, prop("add_rule"), 2)
    monkeypatch.setattr(cp, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(fixer, "PROMPTS_DIR", tmp_path)
    fixer.write_spec(new)
    text = cp.build_system_prompt(ClinicDB(), 2)
    assert "15. Never show internal ids" in text


# ---- acceptance rule --------------------------------------------------------------------------

def row(sid, split, passed, w):
    return {"id": sid, "split": split, "passed": passed, "weighted_score": w, "status": "pass" if passed else "fail",
            "group_scores": {"db": w, "trace": w, "text": w, "judge": w}, "checks": [], "llm_calls": {}}


def test_accept_when_train_improves_without_regressions():
    old = [row("S01", "train", False, 0.5), row("S03", "holdout", True, 1.0)]
    new = [row("S01", "train", True, 1.0), row("S03", "holdout", True, 1.0)]
    assert decide(old, new, [])[0] is True


def test_reject_on_confirmed_regression_even_if_score_up():
    old = [row("S01", "train", False, 0.5), row("S03", "holdout", True, 1.0)]
    new = [row("S01", "train", True, 1.0), row("S03", "holdout", False, 0.75)]
    ok, reason = decide(old, new, ["S03"])
    assert ok is False and "regression" in reason


def test_reject_when_train_score_flat():
    old = [row("S01", "train", False, 0.5)]
    assert decide(old, [row("S01", "train", False, 0.5)], [])[0] is False


def test_round_with_errored_scenarios_is_aborted_not_decided(tmp_path, monkeypatch):
    """If the API refuses calls (e.g. out of credit), the round must stop undecided - never 'reject'."""
    import shutil
    import clinic.prompt as cp
    import run_improve as ri
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    shutil.copy("prompts/v1.yaml", prompts / "v1.yaml")
    for mod in (cp, fixer, ri):
        monkeypatch.setattr(mod, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(ri, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(ri, "CHANGELOG", prompts / "changelog.jsonl")
    monkeypatch.setattr(ri, "make_llm", lambda role: type("L", (), {"calls": []})())
    monkeypatch.setattr(fixer, "propose", lambda *a, **k: (prop("add_rule"), None, "input"))
    errored = [{**r, "status": "error", "passed": False, "error": "OpenRouter 402: out of credit"} for r in RESULTS]
    monkeypatch.setattr(ri, "run_version", lambda *a, **k: errored)

    with pytest.raises(SystemExit):
        ri.round_once(1, 1, RESULTS, [], [], workers=1)
    assert fixer.load_prompt_spec(2)["status"] == "incomplete"
    assert not (prompts / "changelog.jsonl").exists()        # no accept/reject recorded
    assert not (prompts / "active.txt").exists()             # active version untouched
