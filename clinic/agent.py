"""The scheduling agent: a hand-written tool-calling loop.

One Agent = one conversation. It owns:
  - messages : the full chat history sent to the model (append-only, never edited)
  - tools    : this conversation's ClinicTools (own database copy + verified set)
  - trace    : every tool call and result, for the eval harness
"""

import json

from clinic.db import ClinicDB
from clinic.prompt import active_version, build_system_prompt
from clinic.tools import MAX_TOOL_CALLS_PER_TURN, TOOL_SCHEMAS, ClinicTools

# Our tool schemas are stored in Anthropic format; OpenRouter uses the OpenAI format.
OPENAI_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOL_SCHEMAS
]

GIVE_UP_REPLY = ("Sorry, I'm having trouble completing that right now. "
                 "Please call our front desk at {front_desk}.")
EMPTY_REPLY = "Sorry, could you say that again?"


class Agent:
    def __init__(self, llm, db: ClinicDB | None = None, prompt_version: int | None = None):
        self.llm = llm
        self.tools = ClinicTools(db or ClinicDB())
        self.prompt_version = prompt_version if prompt_version is not None else active_version()
        self.messages = [{"role": "system", "content": build_system_prompt(self.tools.db, self.prompt_version)}]
        self.trace = []   # {"turn", "tool", "args", "result"} per tool call
        self.turn = 0

    def respond(self, patient_text: str) -> str:
        """Handle one patient message; return the reply shown to the patient."""
        self.turn += 1
        self.messages.append({"role": "user", "content": patient_text})
        used = 0

        while True:
            msg = self.llm.chat(self.messages, tools=OPENAI_TOOLS)
            self.messages.append(msg)                      # kept as returned, incl. reasoning_details
            calls = msg.get("tool_calls") or []

            if not calls:
                reply = (msg.get("content") or "").strip()
                if not reply:
                    reply = EMPTY_REPLY
                    self.messages.append({"role": "assistant", "content": reply})
                return reply

            for call in calls:                             # run in the order the model listed them
                used += 1
                if used > MAX_TOOL_CALLS_PER_TURN:         # G7
                    result = {"error": "tool_limit_reached",
                              "message": "Too many tool calls for one reply. Stop and apologise to the patient."}
                    self._record(call, None, result)
                    continue
                result, args = self._run_tool(call)
                self._record(call, args, result)

            if used > MAX_TOOL_CALLS_PER_TURN:
                # Every tool_call above already has a tool result, so the history stays valid.
                reply = GIVE_UP_REPLY.format(front_desk=self.tools.db.clinic["phone"])
                self.messages.append({"role": "assistant", "content": reply})
                return reply

    # ---- internals --------------------------------------------------------

    def _run_tool(self, call: dict) -> tuple[dict, dict | None]:
        name = call.get("function", {}).get("name", "")
        raw = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError):
            return {"error": "bad_arguments",
                    "message": f"Arguments for {name} were not a valid JSON object: {raw[:200]!r}. Retry."}, None
        return self.tools.call(name, args), args

    def _record(self, call: dict, args: dict | None, result: dict) -> None:
        name = call.get("function", {}).get("name", "")
        self.trace.append({"turn": self.turn, "tool": name, "args": args, "result": result})
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                              "content": json.dumps(result)})
