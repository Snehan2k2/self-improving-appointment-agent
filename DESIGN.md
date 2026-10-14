# Design note

## The agent

The agent is a plain loop I wrote myself, with no framework, so every safety rule stays visible. It works on a made-up Bengaluru clinic through 8 small tools: verify or register a patient, see a doctor's slots, list a patient's appointments, book, reschedule, cancel, and hand off an emergency.

There are two kinds of guardrail:

- **Must-never-break rules live in code.** Patient data can only be used after their name and phone matched in this chat. The children's doctor only sees under-18s. A patient can't be double-booked. There are at most 15 tool calls per reply.
- **Judgment calls live in the prompt.** Emergencies go to 108, no medical advice, confirm before changing anything, only offer real slots. Four of these rules are locked, so the loop can never weaken them.

The chat history only grows; nothing is edited. "Who is verified" is kept in code, so the model can't fake it. Every chat and every test starts from a fresh copy of the clinic data.

## The eval

21 test conversations: 15 for training, 6 held out. Most are hard cases: emergencies, medical advice, a wrong phone number, questions about other patients, two patients with the same name, a day off, a full day, time clashes, prompt tricks, and a fake doctor. A second model plays the patient.

Each test is graded in four groups, worth 25% each:

1. **Database** (code): did the right booking happen, and nothing else?
2. **Tool trace** (code): right tools called? Any change blocked by a guardrail?
3. **Text** (code): did the patient see "108", or any IDs or other patients' names?
4. **Judge** (model): only things that need meaning, like "did it confirm first?"

I report a weighted score and a strict pass rate. A safety failure can hide in an average, but not in the pass rate. A transcript-only judge can't tell whether a booking really happened, which is why outcomes are checked against the database.

## The improvement loop

The fixer is one model call. It sees the rules, the tools and the failed training tests, never the held-out ones. It returns one change as JSON. Code checks the change, writes the next version, and re-runs all 21 tests. Newly failing tests are run once more to rule out noise. The change is kept only if the training score rises and nothing that passed before breaks.

**Result:** v2 added one rule: "never show internal reasoning, tool details or IDs". The score went from 82% to 98%, held-out tests from 3/6 to 6/6, with no regressions.

## What I learned

- **My first checks gave v1 a perfect 21/21.** The transcripts showed the agent leaking its reasoning and IDs. I added checks, and v1 fell to 9/21. Read transcripts, not just scores.
- **The judge erred too.** It failed a correct refusal. I tightened the question and retry empty replies.
- **The fake patient can break character.** In one test it acted on a fact it was told it had "forgotten".
- **API errors almost counted as agent failures.** Now a round with errors stops without a decision.
- **Two tests still fail.** The agent offers something it should know is impossible, and the code catches it.

**Assumptions:** today is fixed at Mon 12 Oct 2026, and only that week is bookable. Name plus phone identifies a patient, so anyone who knows both can act for them. English only. One run per test at temperature 0, so scores have some noise.

## AI help vs my judgment

Claude wrote most of the code, tests, scenarios and data, and found several bugs in its own harness. I made the design calls:

- a cheap open model
- one week of data with no family links
- an emergency-only handoff
- dropping the two-step confirm and the verification lockout
- a model judge for leaked reasoning
- the 25% weighting
- a 15/6 split with one run each
- stopping after one round
