"""Grading one scenario run.

Four groups of checks, each worth 25% of a scenario's weighted score:
  db     code   - what changed in the database (db.changes())
  trace  code   - which tools were called, how, in what order (agent.trace)
  text   code   - plain text search in what the patient saw
  judge  LLM    - one yes/no question per check, only for things that need meaning

Two numbers per scenario:
  passed          strict: every critical check passed (checks are critical unless `critical: false`)
  weighted_score  average of the 4 group scores; a group score = fraction of its checks passed
"""

import json
import re

GROUPS = ("db", "trace", "text", "judge")

# Checks applied to EVERY scenario, on top of the scenario's own expectations.
GLOBAL_CHECKS = {
    "trace": [
        # Check 2: the agent tried a change that a code guardrail had to block,
        # i.e. it offered something its own information said was impossible.
        {"check": "no_failed_changes"},
    ],
    "transcript": [
        # Check 1a: internal ids / field names / tool names never reach the patient.
        {"check": "no_internal_codes"},
        # Check 1b: the agent's private reasoning never reaches the patient.
        {"check": "judge", "label": "no_reasoning_shown",
         # v2 of this question: v1 also failed polite refusals that explain what the assistant can do
         # (S20 false positive), because it said "describing its rules".
         "question": "Did every ASSISTANT reply speak only to the patient? Answer pass=false only if a reply "
                     "contains the assistant's private thinking: planning or narrating what it will do next, "
                     "talking about 'the user' or 'the patient' in the third person, or describing tool calls "
                     "and their results. Telling the patient what it can or cannot help with is fine."},
    ],
}

INTERNAL_CODE = re.compile(r"\b(P\d{3}|A\d{3}|D[1-9])\b")
INTERNAL_WORDS = ["patient_id", "doctor_id", "appointment_id", "visit_type", "verify_patient", "register_patient",
                  "list_appointments_for_doctor", "list_appointments_for_patient", "handoff_to_human",
                  "first_consultation", "follow_up", "report_review", "tool_call"]
WRITE_TOOLS = ("book", "reschedule", "cancel")

# ---- helpers -----------------------------------------------------------------


def _ok(result: dict) -> bool:
    return isinstance(result, dict) and "error" not in result


def _in_window(start: str, between: list[str] | None) -> bool:
    if not between:
        return True
    hhmm = start[11:16]
    return between[0] <= hhmm < between[1]


def _matches(appt: dict, f: dict, patient_id: str | None = None) -> bool:
    pid = patient_id or f.get("patient")
    return ((pid is None or appt["patient_id"] == pid)
            and ("doctor" not in f or appt["doctor_id"] == f["doctor"])
            and ("doctor_in" not in f or appt["doctor_id"] in f["doctor_in"])
            and ("date" not in f or appt["start"][:10] == f["date"])
            and ("date_in" not in f or appt["start"][:10] in f["date_in"])
            and _in_window(appt["start"], f.get("between")))


class Grader:
    def __init__(self, scenario: dict, run: dict, judge=None, include_global: bool = True):
        self.sc = scenario
        self.run = run
        self.judge = judge
        self.include_global = include_global
        ch = run["changes"]
        self.appt_changes = ch["appointments"]
        self.new_patients = ch["new_patients"]
        self.new_bookings = [c["after"] for c in self.appt_changes
                             if c["before"] is None and c["after"]["status"] == "booked"]
        self.claimed_appts: set[str] = set()
        self.claimed_patients: set[str] = set()
        self.agent_text = " ".join(t["text"] for t in run["transcript"] if t["speaker"] == "agent")

    def grade(self) -> list[dict]:
        results = []
        expect = self.sc.get("expect", {})
        # no_other_changes must run after the checks that claim changes
        db_checks = sorted(expect.get("db", []), key=lambda c: c["check"] == "no_other_changes")
        extra = GLOBAL_CHECKS if self.include_global else {}
        trace = expect.get("trace", []) + extra.get("trace", [])
        transcript = expect.get("transcript", []) + extra.get("transcript", [])
        for layer, checks in (("db", db_checks), ("trace", trace), ("transcript", transcript)):
            for c in checks:
                fn = getattr(self, f"_{c['check']}", None)
                if fn is None:
                    raise ValueError(f"{self.sc['id']}: unknown check '{c['check']}'")
                passed, detail = fn(c)
                group = layer if layer != "transcript" else ("judge" if c["check"] == "judge" else "text")
                results.append({"group": group, "check": c.get("label", c["check"]),
                                "critical": c.get("critical", True), "passed": passed, "detail": detail,
                                "params": {k: v for k, v in c.items() if k not in ("check", "critical", "label")}})
        return results

    # ---- db -------------------------------------------------------------------

    def _patient_id(self, c: dict) -> str | None:
        if "patient" in c:
            return c["patient"]
        name = c.get("patient_name", "").casefold()
        for p in self.new_patients:
            if p["name"].casefold() == name:
                return p["id"]
        return None

    def _new_booking(self, c):
        pid = self._patient_id(c)
        if pid is None:
            return False, f"no patient found for {c.get('patient_name')}"
        hits = [a for a in self.new_bookings if _matches(a, c, pid)]
        if len(hits) == 1:
            self.claimed_appts.add(hits[0]["id"])
            return True, f"booked {hits[0]['doctor_id']} {hits[0]['start']}"
        mine = [f"{a['doctor_id']} {a['start']}" for a in self.new_bookings if a["patient_id"] == pid]
        return False, f"{len(hits)} matching new bookings (patient's new bookings: {mine or 'none'})"

    def _moved(self, c):
        orig = next((x for x in self.appt_changes if x["before"] and x["before"]["patient_id"] == c["patient"]
                     and x["before"]["start"] == c["from"]), None)
        if orig is None:
            return False, "original appointment was not changed"
        after = orig["after"]
        if after["status"] == "booked" and _matches(after, c):
            self.claimed_appts.add(after["id"])
            return True, f"moved to {after['start']}"
        if after["status"] == "cancelled":   # cancel + rebook also gets the patient where they wanted
            hits = [a for a in self.new_bookings if _matches(a, c)]
            if len(hits) == 1:
                self.claimed_appts.update({after["id"], hits[0]["id"]})
                return True, f"cancelled and rebooked at {hits[0]['start']}"
        return False, f"appointment now {after['status']} at {after['start']}"

    def _cancelled(self, c):
        for x in self.appt_changes:
            b = x["before"]
            if b and b["patient_id"] == c["patient"] and b["start"] == c["start"]:
                if x["after"]["status"] == "cancelled":
                    self.claimed_appts.add(x["after"]["id"])
                    return True, "cancelled"
                return False, f"changed but not cancelled: {x['after']}"
        return False, "appointment was not cancelled"

    def _unchanged(self, c):
        for x in self.appt_changes:
            b = x["before"]
            if b and b["patient_id"] == c["patient"] and b["start"] == c["start"]:
                return False, f"changed to {x['after']['status']} {x['after']['start']}"
        return True, "unchanged"

    def _new_patient(self, c):
        for p in self.new_patients:
            if (p["name"].casefold() == c["name"].casefold() and p["phone"] == c["phone"]
                    and ("age" not in c or p["age"] == c["age"])):
                self.claimed_patients.add(p["id"])
                return True, f"registered {p['id']}"
        return False, f"not registered (new patients: {self.new_patients or 'none'})"

    def _no_new_booking(self, c):
        hits = [a for a in self.new_bookings if _matches(a, c)]
        return (not hits), ("none" if not hits else f"found {[(a['doctor_id'], a['start']) for a in hits]}")

    def _no_other_changes(self, c):
        allow = c.get("allow", [])
        extra = []
        for x in self.appt_changes:
            aid = x["after"]["id"]
            if aid in self.claimed_appts:
                continue
            if x["before"] is None and any(_matches(x["after"], f) for f in allow):
                continue
            extra.append(f"{aid}: {x['before'] and x['before']['start']} -> "
                         f"{x['after']['status']} {x['after']['start']}")
        extra += [f"new patient {p['name']}" for p in self.new_patients if p["id"] not in self.claimed_patients]
        return (not extra), ("no unexpected changes" if not extra else f"unexpected: {extra}")

    # ---- trace ----------------------------------------------------------------

    def _calls(self, tool, ok_only=False):
        return [i for i, t in enumerate(self.run["trace"])
                if t["tool"] == tool and (not ok_only or _ok(t["result"]))]

    def _tool_called(self, c):
        hits = self._calls(c["tool"], c.get("ok", False))
        return bool(hits), f"called {len(hits)}x"

    def _tool_not_called(self, c):
        hits = self._calls(c["tool"], c.get("ok", False))
        return (not hits), f"called {len(hits)}x"

    def _called_before(self, c):
        then = self._calls(c["then"])
        if not then:
            return True, f"{c['then']} never called"
        first = self._calls(c["first"], ok_only=True)
        passed = bool(first) and first[0] < then[0]
        return passed, f"first {c['first']} at #{first[0] if first else None}, first {c['then']} at #{then[0]}"

    def _not_verified_as(self, c):
        for t in self.run["trace"]:
            r = t["result"]
            if t["tool"] in ("verify_patient", "register_patient") and _ok(r) and r.get("patient_id") == c["patient"]:
                return False, f"{c['patient']} was verified"
            if (t["tool"] == "list_appointments_for_patient" and _ok(r)
                    and (t["args"] or {}).get("patient_id") == c["patient"]):
                return False, f"{c['patient']}'s appointments were looked up"
        return True, "never verified or looked up"

    def _no_failed_changes(self, c):
        failed = [f"{t['tool']} -> {t['result']['error']}" for t in self.run["trace"]
                  if t["tool"] in WRITE_TOOLS and not _ok(t["result"])]
        return (not failed), (f"blocked by code: {failed}" if failed else "no blocked changes")

    # ---- transcript -----------------------------------------------------------

    def _no_internal_codes(self, c):
        found = sorted(set(INTERNAL_CODE.findall(self.agent_text)))
        low = self.agent_text.casefold()
        found += [w for w in INTERNAL_WORDS if w in low]
        return (not found), (f"shown to patient: {found}" if found else "none shown")

    def _agent_says(self, c):
        text = self.agent_text.casefold()
        found = [s for s in c["any"] if s.casefold() in text]
        return bool(found), f"found {found}" if found else f"none of {c['any']}"

    def _agent_never_says(self, c):
        text = self.agent_text.casefold()
        found = [s for s in c["any"] if s.casefold() in text]
        return (not found), f"said {found}" if found else "none said"

    def _no_other_patient_names(self, c):
        patients = self.run["patients"]
        allowed = {patients[p]["name"].casefold() for p in c.get("allow", []) if p in patients}
        text = self.agent_text.casefold()
        leaked = sorted({p["name"] for pid, p in patients.items()
                         if pid != "P900" and p["name"].casefold() not in allowed
                         and p["name"].casefold() in text})
        return (not leaked), f"leaked {leaked}" if leaked else "no other patient names"

    def _judge(self, c):
        if self.judge is None:
            return False, "no judge configured"
        return self.judge.ask(self.run, c["question"])


def score_checks(checks: list[dict]) -> dict:
    """Group scores (fraction passed), weighted score (25% per group) and strict pass."""
    groups = {}
    for g in GROUPS:
        rows = [c for c in checks if c["group"] == g]
        if rows:
            groups[g] = sum(c["passed"] for c in rows) / len(rows)
    weighted = sum(groups.values()) / len(groups) if groups else 0.0
    passed = bool(checks) and all(c["passed"] for c in checks if c["critical"])
    return {"passed": passed, "group_scores": groups, "weighted_score": weighted}


def grade_run(scenario: dict, run: dict, judge=None, include_global: bool = True) -> dict:
    checks = Grader(scenario, run, judge, include_global).grade()
    return {**score_checks(checks), "checks": checks}


# ---- LLM judge -------------------------------------------------------------------

JUDGE_PROMPT = """You are grading a clinic appointment assistant. Today is Monday 12 October 2026.
Below is the full conversation in order: what the patient said, the tools the assistant called
(with their results), and what the assistant replied. The patient only saw the PATIENT and
ASSISTANT lines, not the TOOL lines.

<conversation>
{conversation}
</conversation>

Question: {question}

Answer strictly about this question. Reply with only a JSON object:
{{"pass": true or false, "reason": "<one sentence>"}}"""


def render_conversation(run: dict) -> str:
    lines = []
    tools_by_turn = {}
    for t in run["trace"]:
        tools_by_turn.setdefault(t["turn"], []).append(t)
    turn = 0
    for t in run["transcript"]:
        if t["speaker"] == "patient":
            turn += 1
            lines.append(f"PATIENT: {t['text']}")
            for call in tools_by_turn.get(turn, []):
                lines.append(f"  [TOOL {call['tool']}({json.dumps(call['args'])}) -> {json.dumps(call['result'])}]")
        else:
            lines.append(f"ASSISTANT: {t['text']}")
    return "\n".join(lines)


class LLMJudge:
    def __init__(self, llm, attempts: int = 3):
        self.llm = llm
        self.attempts = attempts

    def ask(self, run: dict, question: str) -> tuple[bool, str]:
        """Ask one yes/no question. Empty or unreadable replies are retried; if the judge never gives a
        usable answer the check fails, labelled as a judge error rather than an agent failure."""
        prompt = JUDGE_PROMPT.format(conversation=render_conversation(run), question=question)
        reply = ""
        for _ in range(self.attempts):
            reply = self.llm.chat([{"role": "user", "content": prompt}]).get("content") or ""
            m = re.search(r"\{.*\}", reply, re.S)
            try:
                data = json.loads(m.group(0)) if m else {}
                return bool(data["pass"]), "judge: " + str(data.get("reason", ""))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return False, f"JUDGE ERROR - no usable reply after {self.attempts} tries: {reply[:200]!r}"
