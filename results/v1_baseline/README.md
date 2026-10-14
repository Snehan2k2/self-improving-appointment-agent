# v1 baseline (final)

The starting point for the improvement loop. Every later version is compared against these numbers.

```
                 train   holdout   overall
Database          100%      100%      100%
Tool trace         87%      100%       90%
Text (code)        60%       69%       63%
Judge (LLM)        73%       75%       74%
Weighted           80%       86%       82%
Pass rate        6/15      3/6       9/21
```

## How it was produced
- **Agent:** prompt `prompts/v1.yaml`, model `google/gemma-4-26b-a4b-it` via OpenRouter
  (free endpoint was rate limited, so the paid endpoint of the same model served every call),
  temperature 0, seed 7, reasoning on.
- **Conversations:** one run per scenario, 21 scenarios (15 train / 6 holdout), patient simulator
  on the same model (temperature 0, seed 7, reasoning off). Original run: `runs/eval-20261014-213149-v1`
  (cost $0.034, 239 calls).
- **Grading:** re-scored from those saved conversations with the final grader
  (scenario checks + 3 global checks: `no_failed_changes`, `no_internal_codes`, `no_reasoning_shown`
  with the tightened judge question, judge retried on empty replies). Re-score cost $0.015.

## What fails
All 12 failing scenarios fail only on the global checks; every scenario-specific check passes
(right bookings, 108 on emergencies, no medical advice, no other patients' data).

| Failure | Scenarios |
|---|---|
| Reasoning shown to the patient (`no_reasoning_shown`) | S01-S10, S12 |
| Internal ids / tool names shown (`no_internal_codes`) | S01-S04, S06-S10, S12 |
| Change blocked by a code guardrail (`no_failed_changes`) | S09 (pediatrician offered to an 18-year-old), S17 (time clash not checked) |

## Grader changes made before locking this baseline
1. Added the 3 global checks after reading transcripts showed the original checks scored v1 21/21
   while patients were seeing internal reasoning and IDs.
2. Tightened the `no_reasoning_shown` question: the first wording failed a correct refusal (S20)
   that explained what the assistant can help with.
3. Judge retried up to 3 times on empty/unreadable replies (one empty reply had failed S07).

Files: one JSON per scenario (transcript, tool trace, raw messages, DB changes, every check result)
and `summary.json`.
