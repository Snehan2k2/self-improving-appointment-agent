"""Patient simulator: an LLM that plays the patient from a scenario's persona card.

It never sees the agent's prompt, tools or tool results - only what a real patient
would see: the agent's replies.
"""

END = "<END>"

SIM_PROMPT = """You are role-playing a person chatting with {clinic} to sort out a doctor's appointment.
Stay in character. You are the PATIENT (or the person calling for them), never the clinic assistant.

About you:
- Name: {name}
- Phone: {phone}
- Age: {age}
{facts}
Your goal: {goal}

How to behave:
{behaviour}

Rules:
- Write only your next chat message: 1-2 short, natural sentences, like texting.
- Share your details only when asked. Give exactly the details above; never invent new ones.
  If asked something not covered above, say you're not sure.
- Today is Monday 12 October 2026, morning.
- When your goal is done, or the assistant clearly can't help, or you've been told to call someone else,
  send a short goodbye and then write {end} on its own.
- Never write {end} before the assistant has replied to your latest message."""


class PatientSimulator:
    def __init__(self, llm, patient: dict, clinic_name: str = "Sanjeevani Clinic"):
        self.llm = llm
        self.patient = patient
        facts = "".join(f"- {f}\n" for f in patient.get("facts", []))
        behaviour = "\n".join(f"- {b}" for b in patient.get("behaviour", []))
        self.system = SIM_PROMPT.format(
            clinic=clinic_name, name=patient["name"], phone=patient["phone"], age=patient["age"],
            facts=facts, goal=patient["goal"], behaviour=behaviour, end=END)

    def opening(self) -> str:
        return self.patient["opening"]

    def next_message(self, transcript: list[dict]) -> tuple[str, bool]:
        """transcript: [{"speaker": "patient"|"agent", "text": ...}]. Returns (message, done)."""
        # From the simulator's point of view the agent is the "user" and it is the "assistant".
        messages = [{"role": "system", "content": self.system}]
        for t in transcript:
            role = "assistant" if t["speaker"] == "patient" else "user"
            messages.append({"role": role, "content": t["text"]})
        reply = (self.llm.chat(messages).get("content") or "").strip()
        done = END in reply
        text = reply.replace(END, "").strip()
        return text, done
