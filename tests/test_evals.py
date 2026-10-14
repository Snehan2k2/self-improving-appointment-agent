import copy

import pytest

from clinic.db import ClinicDB
from clinic.tools import ClinicTools
from evals.checks import Grader, grade_run, render_conversation
from evals.runner import load_scenarios, run_scenario
from evals.simulator import END, PatientSimulator

SCENARIOS = {s["id"]: s for s in load_scenarios()}


def first_free(t, doctor, date):
    return t.list_appointments_for_doctor(doctor, date)["days"][0]["free"][0]


# The ideal tool actions for each scenario (what a perfect agent would do).
def oracle(sid, t):
    v = t.verify_patient
    if sid == "S01":
        v("Neha Gupta", "9845098450"); t.book("P006", "D4", first_free(t, "D4", "2026-10-14"), "first_consultation")
    elif sid == "S02":
        v("Priya Sharma", "9820011223"); t.reschedule("A024", first_free(t, "D1", "2026-10-16"))
    elif sid == "S03":
        v("Ramesh Iyengar", "9448012345"); t.cancel("A074")
    elif sid == "S04":
        v("Arjun Menon", "9123456789")
        pid = t.register_patient("Arjun Menon", "9123456789", 45)["patient_id"]
        t.book(pid, "D5", first_free(t, "D5", "2026-10-14"), "first_consultation")
    elif sid == "S05":
        v("Priya Sharma", "9820011999")
    elif sid == "S06":
        v("Neha Gupta", "9845098450"); t.book("P006", "D4", "2026-10-14T15:30", "first_consultation")
    elif sid == "S07":
        v("Rahul Verma", "9900012345"); t.list_appointments_for_patient("P005")
    elif sid == "S08":
        v("Ishaan Reddy", "9731122110"); t.book("P008", "D3", first_free(t, "D3", "2026-10-12"), "first_consultation")
    elif sid == "S09":
        v("Kabir Joshi", "9886655443"); t.book("P010", "D1", first_free(t, "D1", "2026-10-12"), "first_consultation")
    elif sid in ("S10", "S12"):
        t.handoff_to_human()
    elif sid == "S13":
        v("Kamala Sharma", "9820011223"); t.book("P003", "D5", first_free(t, "D5", "2026-10-13"), "first_consultation")
    elif sid == "S14":
        v("Lakshmi Reddy", "9731122110"); t.book("P007", "D4", first_free(t, "D4", "2026-10-14"), "first_consultation")
    elif sid == "S15":
        v("Neha Gupta", "9845098450"); t.book("P006", "D6", first_free(t, "D6", "2026-10-16"), "first_consultation")
    elif sid == "S17":
        v("Rahul Verma", "9811122334")
        t.list_appointments_for_patient("P004")   # sees the Thu 14:00 GP visit, so offers 14:30
        t.book("P004", "D4", "2026-10-15T14:30", "first_consultation")
    elif sid == "S18":
        v("Neha Gupta", "9845098450"); t.book("P006", "D4", first_free(t, "D4", "2026-10-15"), "first_consultation")
    # S11, S16, S19, S20, S21: the right answer is to change nothing.


def run_from_tools(sc, actions):
    db = ClinicDB()
    db.apply_setup(sc.get("setup", []))
    t = ClinicTools(db)
    trace = []
    for name in [n for n in dir(t) if n in {"verify_patient", "register_patient", "list_appointments_for_doctor",
                                              "list_appointments_for_patient", "book", "reschedule", "cancel",
                                              "handoff_to_human"}]:
        fn = getattr(t, name)

        def wrapped(*a, _fn=fn, _name=name, **k):
            r = _fn(*a, **k)
            args = k or dict(zip(_fn.__code__.co_varnames[1:], a))
            trace.append({"turn": 1, "tool": _name, "args": args, "result": r})
            return r
        setattr(t, name, wrapped)
    actions(sc["id"], t)
    return {"transcript": [], "trace": trace, "changes": db.changes(), "patients": copy.deepcopy(db.patients)}


def db_and_trace(results):
    return [r for r in results if r["group"] in ("db", "trace")]


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_ideal_actions_pass_every_db_and_trace_check(sid):
    sc = SCENARIOS[sid]
    results = Grader(sc, run_from_tools(sc, oracle)).grade()
    failed = [r for r in db_and_trace(results) if not r["passed"]]
    assert not failed, failed


@pytest.mark.parametrize("sid", ["S01", "S02", "S03", "S04", "S06", "S08", "S13", "S14", "S15", "S17", "S18"])
def test_doing_nothing_fails_booking_scenarios(sid):
    sc = SCENARIOS[sid]
    results = Grader(sc, run_from_tools(sc, lambda *_: None)).grade()
    assert any(not r["passed"] for r in db_and_trace(results))


def test_scenario_counts():
    splits = [s["split"] for s in SCENARIOS.values()]
    assert len(SCENARIOS) == 21 and splits.count("train") == 15 and splits.count("holdout") == 6


# ---- individual checks ------------------------------------------------------------

def test_wrong_day_booking_fails_and_is_reported():
    sc = SCENARIOS["S01"]

    def wrong(_, t):
        t.verify_patient("Neha Gupta", "9845098450")
        t.book("P006", "D4", first_free(t, "D4", "2026-10-15"), "first_consultation")   # Thursday, not Wednesday
    results = Grader(sc, run_from_tools(sc, wrong)).grade()
    by = {r["check"]: r for r in results}
    assert not by["new_booking"]["passed"] and "2026-10-15" in by["new_booking"]["detail"]
    assert not by["no_other_changes"]["passed"]


def test_cancel_and_rebook_counts_as_moved():
    sc = SCENARIOS["S02"]

    def rebook(_, t):
        t.verify_patient("Priya Sharma", "9820011223")
        t.cancel("A024")
        t.book("P001", "D1", first_free(t, "D1", "2026-10-16"), "follow_up")
    results = Grader(sc, run_from_tools(sc, rebook)).grade()
    assert all(r["passed"] for r in db_and_trace(results))


def test_extra_cancellation_breaks_no_other_changes():
    sc = SCENARIOS["S01"]

    def extra(sid, t):
        oracle(sid, t)
        t.verify_patient("Priya Sharma", "9820011223")
        t.cancel("A024")
    by = {r["check"]: r for r in Grader(sc, run_from_tools(sc, extra)).grade()}
    assert by["new_booking"]["passed"] and not by["no_other_changes"]["passed"]


def test_s09_allows_gp_but_not_pediatrician():
    sc = SCENARIOS["S09"]
    assert all(r["passed"] for r in db_and_trace(Grader(sc, run_from_tools(sc, oracle)).grade()))
    nothing = Grader(sc, run_from_tools(sc, lambda *_: None)).grade()
    assert all(r["passed"] for r in db_and_trace(nothing))   # refusing and booking nothing is also fine


def test_setup_fill_day_and_free_slot():
    db = ClinicDB()
    db.apply_setup([{"op": "fill_day", "doctor": "D6", "date": "2026-10-15"},
                    {"op": "free_slot", "doctor": "D4", "start": "2026-10-15T14:00"}])
    from datetime import date
    assert db.free_slots("D6", date(2026, 10, 15)) == []
    assert "2026-10-15T14:00" in db.free_slots("D4", date(2026, 10, 15))
    assert db.changes() == {"new_patients": [], "appointments": [], "handoffs": []}  # setup is the baseline


def transcript_run(agent_lines, patients=None):
    tr = []
    for line in agent_lines:
        tr += [{"speaker": "patient", "text": "hi"}, {"speaker": "agent", "text": line}]
    return {"transcript": tr, "trace": [], "changes": {"new_patients": [], "appointments": [], "handoffs": []},
            "patients": patients or ClinicDB().patients}


def test_text_checks_only_read_agent_lines():
    sc = {"id": "T", "expect": {"transcript": [{"check": "agent_says", "any": ["108"]},
                                               {"check": "no_other_patient_names", "allow": ["P006"]}]}}
    ok = transcript_run(["Please call 108 now, Neha Gupta."])
    assert grade_run(sc, ok, include_global=False)["passed"]
    leak = transcript_run(["Please call 108. Priya Sharma has 3 pm."])
    r = grade_run(sc, leak, include_global=False)
    assert not r["passed"] and "Priya Sharma" in r["checks"][1]["detail"]


def test_non_critical_failure_does_not_fail_run():
    sc = {"id": "T", "expect": {"transcript": [{"check": "agent_says", "any": ["xyz"], "critical": False}]}}
    assert grade_run(sc, transcript_run(["hello"]), include_global=False)["passed"]


# ---- judge, simulator and the full runner with fake models -------------------------

class ScriptLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls, self.seen = [], []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else {"role": "assistant", "content": "ok"}


def test_simulator_flips_roles_and_detects_end():
    llm = ScriptLLM([{"content": f"Thanks, bye! {END}"}])
    sim = PatientSimulator(llm, SCENARIOS["S01"]["patient"])
    text, done = sim.next_message([{"speaker": "patient", "text": "hi"}, {"speaker": "agent", "text": "hello"}])
    assert done and text == "Thanks, bye!"
    roles = [m["role"] for m in llm.seen[0]]
    assert roles == ["system", "assistant", "user"]
    assert "9845098450" in llm.seen[0][0]["content"]


def test_full_runner_with_fake_models():
    call_id = iter(range(100))

    def tc(tool_name, **a):
        import json
        return {"id": f"c{next(call_id)}", "type": "function", "function": {"name": tool_name, "arguments": json.dumps(a)}}

    agent = ScriptLLM([
        {"role": "assistant", "content": "May I have your name and phone?"},
        {"role": "assistant", "content": None, "tool_calls": [tc("verify_patient", name="Neha Gupta", phone="9845098450")]},
        {"role": "assistant", "content": None, "tool_calls": [tc("list_appointments_for_doctor", doctor_id="D4", date="2026-10-14")]},
        {"role": "assistant", "content": "Dr. Rohan Kapoor, Wed 14 Oct, 2:00 pm, first consultation. Shall I book it?"},
        {"role": "assistant", "content": None, "tool_calls": [tc("book", patient_id="P006", doctor_id="D4",
                                                                start="2026-10-14T14:00", visit_type="first_consultation")]},
        {"role": "assistant", "content": "Booked! Wed 14 Oct, 2:00 pm with Dr. Kapoor."},
    ])
    # The final "yes" arrives in the same message as <END>: it must still reach the agent.
    patient = ScriptLLM([{"content": "Neha Gupta, 9845098450"}, {"content": f"Yes please, thanks! {END}"}])
    judge = ScriptLLM([{"content": '{"pass": true, "reason": "confirmed first"}'},
                       {"content": '{"pass": true, "reason": "only spoke to the patient"}'}])
    llms = {"agent": agent, "patient": patient, "judge": judge}
    r = run_scenario(SCENARIOS["S01"], lambda role: llms[role])
    assert r["status"] == "pass", [c for c in r["checks"] if not c["passed"]]
    assert r["ended_by"] == "patient"
    assert "[TOOL book(" in render_conversation(r)
    assert "Booked!" in judge.seen[0][0]["content"]
    assert len(judge.seen) == 2 and r["weighted_score"] == 1.0
    assert set(r["group_scores"]) == {"db", "trace", "text", "judge"}


def test_unparseable_judge_reply_fails_check():
    sc = {"id": "T", "expect": {"transcript": [{"check": "judge", "question": "ok?"}]}}
    from evals.checks import LLMJudge
    r = grade_run(sc, transcript_run(["hi"]), LLMJudge(ScriptLLM([{"content": "I think yes"}])), include_global=False)
    assert not r["passed"] and "JUDGE ERROR" in r["checks"][0]["detail"]


def test_judge_retries_after_empty_reply():
    from evals.checks import LLMJudge
    judge = ScriptLLM([{"content": ""}, {"content": '{"pass": true, "reason": "fine"}'}])
    sc = {"id": "T", "expect": {"transcript": [{"check": "judge", "question": "ok?"}]}}
    r = grade_run(sc, transcript_run(["hi"]), LLMJudge(judge), include_global=False)
    assert r["passed"] and len(judge.seen) == 2


# ---- the three global checks + scoring ------------------------------------------

def test_no_internal_codes():
    sc = {"id": "T", "expect": {}}
    clean = transcript_run(["Dr. Kapoor is free Wed 14 Oct at 2:00 pm. Is this a follow-up visit?"])
    by = {c["check"]: c for c in Grader(sc, clean).grade()}
    assert by["no_internal_codes"]["passed"]
    leaky = transcript_run(["The patient is verified (patient_id: P006). Booking D4.",
                            "The tool handoff_to_human has been called."])
    by = {c["check"]: c for c in Grader(sc, leaky).grade()}
    d = by["no_internal_codes"]["detail"]
    assert not by["no_internal_codes"]["passed"]
    assert all(x in d for x in ["P006", "D4", "patient_id", "handoff_to_human"])


def test_no_failed_changes():
    sc = SCENARIOS["S09"]

    def tries_pediatrician(_, t):
        t.verify_patient("Kabir Joshi", "9886655443")
        t.book("P010", "D3", first_free(t, "D3", "2026-10-12"), "first_consultation")   # blocked by G3
        t.book("P010", "D1", first_free(t, "D1", "2026-10-12"), "first_consultation")
    by = {c["check"]: c for c in Grader(sc, run_from_tools(sc, tries_pediatrician)).grade()}
    assert by["no_new_booking"]["passed"] and by["no_other_changes"]["passed"]     # outcome was fine...
    assert not by["no_failed_changes"]["passed"] and "age_not_allowed" in by["no_failed_changes"]["detail"]


def test_global_checks_in_every_scenario_and_groups():
    sc = SCENARIOS["S21"]
    checks = Grader(sc, run_from_tools(sc, oracle)).grade()
    names = {c["check"] for c in checks}
    assert {"no_failed_changes", "no_internal_codes", "no_reasoning_shown"} <= names
    assert {c["group"] for c in checks} == {"db", "trace", "text", "judge"}


def test_weighted_score_is_mean_of_groups():
    from evals.checks import score_checks
    checks = [{"group": "db", "passed": True, "critical": True},
              {"group": "db", "passed": True, "critical": True},
              {"group": "trace", "passed": False, "critical": True},
              {"group": "text", "passed": True, "critical": True},
              {"group": "judge", "passed": True, "critical": True},
              {"group": "judge", "passed": False, "critical": False}]
    s = score_checks(checks)
    assert s["group_scores"] == {"db": 1.0, "trace": 0.0, "text": 1.0, "judge": 0.5}
    assert s["weighted_score"] == 0.625 and s["passed"] is False


def test_check_label_does_not_clash_with_name_param():
    """new_patient has a 'name' param; it must still be reported as 'new_patient'."""
    sc = SCENARIOS["S04"]
    checks = Grader(sc, run_from_tools(sc, oracle)).grade()
    np = next(c for c in checks if c["group"] == "db" and c["params"].get("name") == "Arjun Menon")
    assert np["check"] == "new_patient" and np["passed"]
    assert any(c["check"] == "no_reasoning_shown" for c in checks)
