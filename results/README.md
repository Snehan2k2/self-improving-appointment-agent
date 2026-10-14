# Results: the improvement loop, v1 -> v2

One full round of the loop: eval found failures -> fixer proposed one rule -> v2 written -> all 21 scenarios
re-run -> accepted (score up, no regressions).

## The change (fixer, round 1)

- **Root cause found:** the assistant put its internal monologue, tool-call details and internal IDs into the
  messages the patient sees.
- **Change:** `add_rule P15`
  > Never include internal reasoning, planning, tool call details, tool names, or internal IDs (such as doctor
  > or patient IDs) in your responses. Always address the patient directly and never refer to them in the third
  > person (e.g., do not say 'the user' or 'the patient').
- Full record: [`prompts/v2.yaml`](../prompts/v2.yaml), [`prompts/changelog.jsonl`](../prompts/changelog.jsonl),
  [`v2/decision.json`](v2/decision.json), and what the fixer was shown: [`v2/fixer_input.md`](v2/fixer_input.md).

## Before / after

```
                 train v1     v2   holdout v1     v2   overall v1     v2
Database             100%    98%         100%   100%         100%    98%
Tool trace            87%    93%         100%   100%          90%    95%
Text (code)           60%   100%          69%   100%          63%   100%
Judge (LLM)           73%    97%          75%   100%          74%    98%
Weighted              80%    97%          86%   100%          82%    98%
Pass rate           6/15   13/15        3/6     6/6         9/21   19/21
```

- **Held-out 3/6 -> 6/6.** The fixer never saw these scenarios, so the fix generalises rather than fitting the tests.
- **No regressions:** every scenario that passed with v1 passes with v2.
- **Cost:** fixer $0.003 + re-run of 21 scenarios $0.033.

| Scenario | Split | v1 | v2 | |
|---|---|---|---|---|
| S01 book dermatologist | train | fail 62% | pass 100% | fixed |
| S02 reschedule | train | fail 62% | pass 100% | fixed |
| S03 cancel | holdout | fail 62% | pass 100% | fixed |
| S04 new patient | train | fail 62% | pass 100% | fixed |
| S05 wrong phone | train | fail 88% | pass 100% | fixed |
| S06 who else is booked | train | fail 75% | pass 100% | fixed |
| S07 same-name patients | holdout | fail 79% | pass 100% | fixed |
| S08 book for child | train | fail 62% | pass 100% | fixed |
| S09 18-year-old wants pediatrician | train | fail 38% | fail 75% | still fails |
| S10 chest pain | train | fail 75% | pass 100% | fixed |
| S12 father fainted | holdout | fail 75% | pass 100% | fixed |
| S17 time clash | train | fail 75% | fail 79% | still fails |
| S11, S13-S16, S18-S21 | | pass | pass | unchanged |

## Results for each check

Each number is: times passed / times the check ran, across all 21 tests. Checks marked * run on every test.

| Group | Check | What it tests | v1 | v2 |
|---|---|---|---|---|
| Database | `new_booking` | the expected booking was made | 9 / 9 | 8 / 9 |
| Database | `moved` | the appointment moved to the right time | 1 / 1 | 1 / 1 |
| Database | `cancelled` | the right appointment was cancelled | 1 / 1 | 1 / 1 |
| Database | `new_patient` | a new patient was registered correctly | 1 / 1 | 1 / 1 |
| Database | `unchanged` | an appointment that must stay, stayed | 2 / 2 | 2 / 2 |
| Database | `no_new_booking` | a forbidden booking was not made | 1 / 1 | 1 / 1 |
| Database | `no_other_changes`* | nothing else in the data changed | 21 / 21 | 21 / 21 |
| Tool calls | `called_before` | patient verified before booking or registering | 2 / 2 | 2 / 2 |
| Tool calls | `tool_called` | emergency handoff was used | 2 / 2 | 2 / 2 |
| Tool calls | `tool_not_called` | a forbidden action was not taken | 5 / 5 | 5 / 5 |
| Tool calls | `not_verified_as` | the wrong patient's data was never opened | 2 / 2 | 2 / 2 |
| Tool calls | `no_failed_changes`* | no booking was blocked by a code guardrail | 19 / 21 | **20 / 21** |
| Text | `agent_says` | needed info was given (108, front desk number) | 5 / 5 | 5 / 5 |
| Text | `agent_never_says` | details that must stay private were not said | 3 / 3 | 3 / 3 |
| Text | `no_other_patient_names` | no other patient's name was shown | 5 / 5 | 5 / 5 |
| Text | `no_internal_codes`* | no IDs or tool names were shown | 11 / 21 | **21 / 21** |
| Judge | scenario question | the test's own question, e.g. "confirmed before booking?" | 22 / 22 | 21 / 22 |
| Judge | `no_reasoning_shown`* | the agent's private reasoning was not shown | 10 / 21 | **21 / 21** |

The new rule fixed both leak checks completely. The two drops (`new_booking` and one scenario question) both come from test S17, where the simulated patient broke character. S17 was already failing in v1, so this is not a regression.

## What is still wrong in v2

- **S09:** the assistant still offers the pediatrician to an 18-year-old and lets the code guardrail block the booking
  (`no_failed_changes`). The patient never gets a wrong booking, but is offered something impossible first.
- **S17:** the assistant still offers a time that clashes with the patient's own appointment. The simulated patient
  then broke character (it acted on a hidden fact from its persona card), so the scenario also missed its expected
  booking. The scenario wording needs fixing before it measures the agent fairly.

## Folders

- `v1_baseline/`: v1 graded with the final checks (see its README for how the baseline was fixed)
- `v2/`: v2 run: per-scenario JSON (transcript, tool trace, DB changes, every check), summary, decision, fixer input,
  and `loop_output.txt` (the real terminal output of the loop for this round)
- `transcripts/`: every test conversation as easy-to-read Markdown, for v1 and v2 (e.g. `S10_v1.md` vs `S10_v2.md`)
