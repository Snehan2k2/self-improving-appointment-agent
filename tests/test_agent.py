import json

import pytest

import clinic.llm as llm_mod
from clinic.agent import EMPTY_REPLY, Agent
from clinic.db import ClinicDB
from clinic.llm import OpenRouterLLM, _clean_message
from clinic.prompt import build_system_prompt, load_prompt_spec
from clinic.tools import MAX_TOOL_CALLS_PER_TURN


class FakeLLM:
    """Returns scripted assistant messages and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.script.pop(0) if self.script else {"role": "assistant", "content": "ok"}


_n = 0


def call(tool_name, **args):
    global _n
    _n += 1
    return {"id": f"call_{_n}", "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(args)}}


def tool_msg(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def say(text):
    return {"role": "assistant", "content": text}


def assert_history_valid(messages):
    """Every tool_call must be answered by a tool message with the same id before the next model turn."""
    pending = set()
    for m in messages:
        if m["role"] == "assistant":
            assert not pending, f"unanswered tool calls {pending}"
            pending = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            pending.discard(m["tool_call_id"])
        elif m["role"] == "user":
            assert not pending
    assert not pending


# ---- basic loop ---------------------------------------------------------------

def test_plain_reply():
    agent = Agent(FakeLLM([say("Hello! How can I help?")]))
    assert agent.respond("hi") == "Hello! How can I help?"
    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant"]


def test_tool_call_then_reply():
    fake = FakeLLM([tool_msg(call("verify_patient", name="Neha Gupta", phone="9845098450")),
                    say("Thanks Neha!")])
    agent = Agent(fake)
    assert agent.respond("Neha Gupta, 9845098450") == "Thanks Neha!"
    assert agent.trace[0]["tool"] == "verify_patient" and agent.trace[0]["result"]["registered"]
    # the second model call saw the tool result
    assert fake.seen[1][-1]["role"] == "tool" and "P006" in fake.seen[1][-1]["content"]
    assert_history_valid(agent.messages)


def test_parallel_calls_each_answered_in_order():
    a, b = call("list_appointments_for_doctor", doctor_id="D1", date="2026-10-16"), \
           call("list_appointments_for_doctor", doctor_id="D2", date="2026-10-15")
    agent = Agent(FakeLLM([tool_msg(a, b, content="Let me check."), say("Both checked.")]))
    assert agent.respond("any GP Thu or Fri?") == "Both checked."
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == [a["id"], b["id"]]
    assert [t["args"]["doctor_id"] for t in agent.trace] == ["D1", "D2"]
    assert_history_valid(agent.messages)


def test_bad_json_arguments_become_error_result():
    bad = {"id": "call_bad", "type": "function", "function": {"name": "verify_patient", "arguments": "{name:"}}
    agent = Agent(FakeLLM([tool_msg(bad), say("Sorry, let me retry.")]))
    agent.respond("hi")
    assert agent.trace[0]["result"]["error"] == "bad_arguments"
    assert_history_valid(agent.messages)


def test_unknown_tool_becomes_error_result():
    agent = Agent(FakeLLM([tool_msg(call("delete_everything")), say("ok")]))
    agent.respond("hi")
    assert agent.trace[0]["result"]["error"] == "unknown_tool"


def test_empty_reply_is_replaced():
    agent = Agent(FakeLLM([say("")]))
    assert agent.respond("hi") == EMPTY_REPLY


def test_reasoning_details_kept_in_history():
    msg = {"role": "assistant", "content": "hi", "reasoning_details": [{"type": "reasoning.text", "text": "x"}]}
    agent = Agent(FakeLLM([msg]))
    agent.respond("hi")
    assert agent.messages[-1]["reasoning_details"] == msg["reasoning_details"]


# ---- G7: tool-call limit --------------------------------------------------------

def test_g7_tool_limit_stops_runaway_loop():
    looping = [tool_msg(call("list_appointments_for_doctor", doctor_id="D1")) for _ in range(40)]
    fake = FakeLLM(looping)
    agent = Agent(fake)
    reply = agent.respond("hi")
    assert "front desk" in reply and "080-41234567" in reply
    real_calls = [t for t in agent.trace if t["result"].get("error") != "tool_limit_reached"]
    assert len(real_calls) == MAX_TOOL_CALLS_PER_TURN
    assert_history_valid(agent.messages)
    # conversation can continue after giving up
    fake.script = [say("I'm back.")]
    assert agent.respond("hello?") == "I'm back."
    assert_history_valid(agent.messages)


def test_g7_limit_with_parallel_batch_answers_every_call():
    batch = tool_msg(*[call("list_appointments_for_doctor", doctor_id="D1") for _ in range(20)])
    agent = Agent(FakeLLM([batch]))
    agent.respond("hi")
    assert sum(1 for t in agent.trace if t["result"].get("error") == "tool_limit_reached") == 5
    assert_history_valid(agent.messages)


def test_g7_limit_resets_each_patient_message():
    script = [tool_msg(call("list_appointments_for_doctor", doctor_id="D1")) for _ in range(10)] + [say("a")]
    script += [tool_msg(call("list_appointments_for_doctor", doctor_id="D1")) for _ in range(10)] + [say("b")]
    agent = Agent(FakeLLM(script))
    assert agent.respond("1") == "a"
    assert agent.respond("2") == "b"


# ---- guardrails still apply through the loop ------------------------------------

def test_model_cannot_book_without_verifying():
    agent = Agent(FakeLLM([tool_msg(call("book", patient_id="P001", doctor_id="D1",
                                         start="2026-10-16T09:00", visit_type="follow_up")),
                           say("hmm")]))
    agent.respond("book me")
    assert agent.trace[0]["result"]["error"] == "not_verified"
    assert agent.tools.db.changes()["appointments"] == []


def test_full_booking_flow_shows_in_db_changes():
    script = [
        tool_msg(call("verify_patient", name="Neha Gupta", phone="9845098450")), say("Which day?"),
        tool_msg(call("list_appointments_for_doctor", doctor_id="D4", date="2026-10-14")), say("4:00 pm ok?"),
        tool_msg(call("book", patient_id="P006", doctor_id="D4", start="2026-10-14T16:00",
                      visit_type="first_consultation")), say("Booked!"),
    ]
    agent = Agent(FakeLLM(script))
    for text in ["Neha Gupta 9845098450", "Wednesday", "yes"]:
        agent.respond(text)
    ch = agent.tools.db.changes()["appointments"]
    assert len(ch) == 1 and ch[0]["before"] is None and ch[0]["after"]["start"] == "2026-10-14T16:00"
    assert_history_valid(agent.messages)


def test_each_agent_has_its_own_db():
    a = Agent(FakeLLM([tool_msg(call("verify_patient", name="Neha Gupta", phone="9845098450")), say("x")]))
    b = Agent(FakeLLM([]))
    a.respond("hi")
    assert a.tools.verified == {"P006"} and b.tools.verified == set()


# ---- prompt ------------------------------------------------------------------------

def test_prompt_has_facts_and_rules():
    p = build_system_prompt(ClinicDB(), version=1)
    assert "Monday 12 October 2026, 8:30 am" in p
    assert "Dr. Sunita Iyer, Pediatrician" in p and "under 18 only" in p
    assert "080-41234567" in p and "108" in p
    assert "{" not in p  # every placeholder filled


def test_safety_rules_are_locked():
    rules = {r["id"]: r for r in load_prompt_spec(1)["rules"]}
    for rid in ["P1", "P2", "P6", "P7"]:
        assert rules[rid]["locked"] is True


# ---- LLM client (no network) ----------------------------------------------------

def test_clean_message_keeps_only_needed_fields():
    raw = {"role": "assistant", "content": "hi", "refusal": None, "reasoning": "...",
           "reasoning_details": [{"t": 1}], "tool_calls": [{"id": "c1"}]}
    assert _clean_message(raw) == {"role": "assistant", "content": "hi",
                                   "tool_calls": [{"id": "c1"}], "reasoning_details": [{"t": 1}]}


class FakeResp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.text = status, data or {}, json.dumps(data or {})

    def json(self):
        return self._data


def test_free_model_rate_limit_falls_back_to_paid(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    sent = []

    def fake_post(url, json, timeout, headers):
        sent.append(json["model"])
        if json["model"].endswith(":free"):
            return FakeResp(429, {"error": {"message": "rate limited"}})
        return FakeResp(200, {"model": json["model"], "choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    llm = OpenRouterLLM(model="google/gemma-4-26b-a4b-it:free", fallback_model="google/gemma-4-26b-a4b-it")
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "hi"
    assert sent == ["google/gemma-4-26b-a4b-it:free", "google/gemma-4-26b-a4b-it"]
    llm.chat([{"role": "user", "content": "y"}])
    assert sent[-1] == "google/gemma-4-26b-a4b-it"  # stays on paid after the free quota ran out


def test_missing_key_gives_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod, "load_env", lambda *a, **k: None)
    with pytest.raises(llm_mod.LLMError, match="OPENROUTER_API_KEY"):
        OpenRouterLLM()
