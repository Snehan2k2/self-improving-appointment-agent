"""The fixer: one model call that reads eval failures and proposes ONE change to the rules.

It only ever sees TRAIN scenarios. Held-out scenarios are never shown, not even their ids.
Its output is a JSON proposal; code validates it and applies it to make the next prompt version.
"""

import copy
import json
import re

import yaml

from clinic.db import ClinicDB
from clinic.prompt import PROMPTS_DIR, build_system_prompt, load_prompt_spec
from clinic.tools import TOOL_SCHEMAS

ALLOWED_OPS = ("add_rule", "edit_rule", "delete_rule")
MAX_RULE_CHARS = 600
TOOL_RESULT_CHARS = 300

FIXER_PROMPT = """You improve the instructions of a clinic appointment-scheduling assistant.
The assistant chats with patients and uses tools. It was tested on scenarios with a simulated patient,
and each conversation was graded by automatic checks. Your job: find the single most important root cause
behind the failures and propose ONE change to the assistant's rules that fixes it.

## The assistant's full system prompt today (for context)
<system_prompt>
{system_prompt}
</system_prompt>

## The rules you may change (only this part of the prompt is editable)
{rules}

Locked rules ({locked}) are safety rules: they can never be edited or deleted.

## Tools the assistant can call
{tools}

## What the checks mean
- no_reasoning_shown: the patient must only see replies addressed to them, never the assistant's own
  planning or narration (talking about "the user"/"the patient", describing tool calls or results).
- no_internal_codes: the patient must never see internal ids (P006, D4, A024), field names (patient_id)
  or tool names (verify_patient).
- no_failed_changes: book/reschedule/cancel was refused by the system because the assistant offered
  something its own information already ruled out (e.g. age limits, the patient's existing appointments).
- Other checks are scenario-specific and their detail explains what was expected.

## Eval results on training scenarios (prompt v{version})
Passed {passed}/{total}.
How often each check failed: {failure_counts}

Scenarios that currently PASS (your change must not break them):
{passing}

## Failed scenarios
{failures}
{history}
## Your task
1. Identify the root cause that explains the most failures.
2. Propose exactly ONE change:
   - add_rule: a new rule (give rule_id "NEW")
   - edit_rule: replace the full text of an existing unlocked rule (give its rule_id)
   - delete_rule: remove an unlocked rule (give its rule_id, text "")
3. Write the rule as general behaviour for all patients. Never mention scenario ids or test patients.
   Keep the text under {max_chars} characters.

Reply with only a JSON object, no other text:
{{"target_checks": ["<check names this fixes>"],
  "root_cause": "<one or two sentences>",
  "change": {{"op": "add_rule|edit_rule|delete_rule", "rule_id": "<id or NEW>", "text": "<rule text>"}},
  "expected_to_fix": ["<scenario ids>"],
  "risk": "<what could get worse>"}}"""


# ---- building the input ------------------------------------------------------------


def _render(run: dict) -> str:
    lines, tools_by_turn, turn = [], {}, 0
    for t in run.get("trace", []):
        tools_by_turn.setdefault(t["turn"], []).append(t)
    for t in run.get("transcript", []):
        if t["speaker"] == "patient":
            turn += 1
            lines.append(f"PATIENT: {t['text']}")
            for call in tools_by_turn.get(turn, []):
                res = json.dumps(call["result"])
                if len(res) > TOOL_RESULT_CHARS:
                    res = res[:TOOL_RESULT_CHARS] + "...(cut)"
                lines.append(f"  [TOOL {call['tool']}({json.dumps(call['args'])}) -> {res}]")
        else:
            lines.append(f"ASSISTANT: {t['text']}")
    return "\n".join(lines)


def _rules_text(spec: dict) -> str:
    return "\n".join(f"- {r['id']}{' [locked]' if r.get('locked') else ''}: {r['text'].strip()}"
                     for r in spec["rules"])


def _tools_text() -> str:
    return "\n".join(f"- {t['name']}: {t['description']}" for t in TOOL_SCHEMAS)


def _history_text(history: list[dict]) -> str:
    if not history:
        return ""
    lines = ["\n## Earlier proposals and what happened (do not repeat a rejected idea unchanged)"]
    for h in history:
        lines.append(f"- v{h['version']}: {h['change']['op']} {h['change'].get('rule_id', '')}: "
                     f"\"{h['change'].get('text', '')[:200]}\" -> {'ACCEPTED' if h['accepted'] else 'REJECTED'}. "
                     f"{h['feedback']}")
    return "\n".join(lines) + "\n"


def build_input(version: int, results: list[dict], history: list[dict]) -> str:
    """Only train results are used. Holdout results are filtered out here, before anything is shown."""
    train = [r for r in results if r["split"] == "train"]
    spec = load_prompt_spec(version)
    failed = [r for r in train if not r["passed"]]
    counts = {}
    for r in failed:
        for c in r["checks"]:
            if not c["passed"]:
                counts[c["check"]] = counts.get(c["check"], 0) + 1
    failures = []
    for r in failed:
        checks = "\n".join(f"- [{c['group']}] {c['check']}: {c['detail'][:300]}" for c in r["checks"] if not c["passed"])
        failures.append(f"### {r['id']}: {r['title']}\nFailed checks:\n{checks}\nConversation:\n{_render(r)}\n")
    passing = "\n".join(f"- {r['id']}: {r['title']}" for r in train if r["passed"]) or "- none"
    return FIXER_PROMPT.format(
        system_prompt=build_system_prompt(ClinicDB(), version),
        rules=_rules_text(spec),
        locked=", ".join(r["id"] for r in spec["rules"] if r.get("locked")),
        tools=_tools_text(), version=version, passed=len(train) - len(failed), total=len(train),
        failure_counts=json.dumps(dict(sorted(counts.items(), key=lambda kv: -kv[1]))),
        passing=passing, failures="\n".join(failures), history=_history_text(history),
        max_chars=MAX_RULE_CHARS)


# ---- validating + applying the proposal ---------------------------------------------------


def parse_proposal(reply: str) -> dict | None:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def validate(proposal: dict | None, spec: dict) -> str | None:
    """Returns a problem description, or None if the proposal may be applied."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get("change"), dict):
        return "reply was not a JSON object with a 'change' object"
    ch = proposal["change"]
    op, rid, text = ch.get("op"), str(ch.get("rule_id", "")), ch.get("text") or ""
    rules = {r["id"]: r for r in spec["rules"]}
    if op not in ALLOWED_OPS:
        return f"op must be one of {ALLOWED_OPS}, got {op!r}"
    if op in ("edit_rule", "delete_rule"):
        if rid not in rules:
            return f"rule {rid!r} does not exist"
        if rules[rid].get("locked"):
            return f"rule {rid} is locked and cannot be {op.split('_')[0]}ed"
    if op in ("add_rule", "edit_rule"):
        if not 20 <= len(text) <= MAX_RULE_CHARS:
            return f"rule text must be 20-{MAX_RULE_CHARS} characters (got {len(text)})"
        if re.search(r"\bS\d{2}\b", text):
            return "rule text must not mention scenario ids"
        if op == "edit_rule" and text.strip() == rules[rid]["text"].strip():
            return "edit does not change the rule"
    return None


def apply(spec: dict, proposal: dict, new_version: int) -> dict:
    """Return the next prompt spec with the one change applied (input spec is not modified)."""
    new = copy.deepcopy(spec)
    ch = proposal["change"]
    op, rid, text = ch["op"], str(ch.get("rule_id", "")), (ch.get("text") or "").strip()
    if op == "add_rule":
        n = max(int(r["id"][1:]) for r in new["rules"] if re.fullmatch(r"P\d+", r["id"])) + 1
        rid = f"P{n}"
        new["rules"].append({"id": rid, "locked": False, "text": text})
    elif op == "edit_rule":
        next(r for r in new["rules"] if r["id"] == rid)["text"] = text
    elif op == "delete_rule":
        new["rules"] = [r for r in new["rules"] if r["id"] != rid]
    new.update({"version": new_version, "parent": spec["version"], "status": "candidate",
                "note": f"{op} {rid}: {proposal.get('root_cause', '')}".strip()})
    new["change"] = {"op": op, "rule_id": rid, "text": text}
    return new


def write_spec(spec: dict) -> None:
    order = ["version", "parent", "status", "note", "change", "role", "rules"]
    ordered = {k: spec[k] for k in order if k in spec}
    path = PROMPTS_DIR / f"v{spec['version']}.yaml"
    path.write_text(yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=110))


def set_status(version: int, status: str) -> None:
    spec = load_prompt_spec(version)
    spec["status"] = status
    write_spec(spec)


# ---- the call ------------------------------------------------------------------------


def propose(llm, version: int, results: list[dict], history: list[dict], attempts: int = 3):
    """Ask for a proposal; if it is invalid, say why and ask again. Returns (proposal, problem, prompt)."""
    spec = load_prompt_spec(version)
    prompt = build_input(version, results, history)
    messages = [{"role": "user", "content": prompt}]
    problem = "no reply"
    for _ in range(attempts):
        msg = llm.chat(messages)
        proposal = parse_proposal(msg.get("content"))
        problem = validate(proposal, spec)
        if problem is None:
            return proposal, None, prompt
        messages += [{"role": "assistant", "content": msg.get("content") or ""},
                     {"role": "user", "content": f"That proposal is invalid: {problem}. "
                                                 "Reply again with only the corrected JSON object."}]
    return None, problem, prompt
