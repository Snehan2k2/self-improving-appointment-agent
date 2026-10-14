# Clinic Scheduler Agent

A chat agent that books doctor appointments for a small clinic. It comes with a test harness that finds the agent's mistakes and improves the agent's instructions on its own.

## What's inside

- **The agent.** It chats with patients and books, reschedules or cancels appointments. It works on a made-up clinic in Bengaluru: 6 doctors, 100 patients, one week of slots. It can only act through 8 tools.
- **The eval.** 21 test conversations. Another model plays the patient. Each conversation is then graded by code checks and by a model acting as judge.
- **The improvement loop.** A "fixer" reads the failed conversations and suggests one change to the agent's rules. The harness re-runs all 21 tests. It keeps the change only if the score goes up and nothing that passed before now fails.

## Result

One round of the loop, from v1 to v2:

| | v1 | v2 |
|---|---|---|
| Overall score | 82% | 98% |
| Tests passed (train) | 6 / 15 | 13 / 15 |
| Tests passed (held out, never shown to the fixer) | 3 / 6 | 6 / 6 |
| Tests that broke | | 0 |

Details are in [results/README.md](results/README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) and an [OpenRouter](https://openrouter.ai) API key.

```bash
uv sync
```

```bash
cp .env.example .env
```

Open `.env` and paste your key after `OPENROUTER_API_KEY=`.

**Your OpenRouter account needs some credit.** The free model is often rate limited, so the code switches to the paid version of the same model (Gemma 4 26B). It is cheap: one full eval run costs about $0.04.

## Run the agent

```bash
uv run python run_agent.py
```

Chat as a patient. Try: *"Hi, I'm Neha Gupta, 9845098450. Can I see a skin doctor on Wednesday afternoon?"* Tool calls are shown in grey. Type `quit` to exit.

## Run the eval loop

```bash
uv run python run_improve.py --rounds 1
```

This starts from the current best version. It asks the fixer for one change, re-runs all 21 tests, and prints the before and after scores with the decision.

## Other commands

Run the 21 tests once, without changing anything:

```bash
uv run python run_eval.py
```

Run the unit tests. These need no key and cost nothing:

```bash
uv run pytest
```

See the full prompt the agent gets:

```bash
uv run python run_agent.py --show-prompt
```

## Where things are

| Path | What it holds |
|---|---|
| `clinic/` | the agent: tools, guardrails, prompt builder, chat loop |
| `prompts/` | rule files `v1.yaml` and `v2.yaml`, the active version, and a log of every change |
| `evals/` | the 21 test scenarios, patient simulator, grader, fixer |
| `data/clinic.json` | the clinic data (made by `scripts/generate_clinic_data.py`) |
| `results/` | saved results for v1 and v2 |
| `docs/` | tool and guardrail specs |
| `DESIGN.md` | the one-page design note |
