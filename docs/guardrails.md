# Guardrails

Rule of thumb: if something must **never** happen and code can check it with a yes/no, it is enforced
in the tools (the model cannot talk its way around it). Judgment calls go in the prompt. Whatever
neither can guarantee is measured by the eval.

## Code guardrails (enforced in `clinic/tools.py`)

| # | Guardrail | Tools | Error code |
|---|---|---|---|
| G1 | **Verified first.** A patient's data or appointments can only be used after that patient was verified (`verify_patient`) or registered (`register_patient`) in **this** conversation. For appointment-id tools this means the appointment's owner must be verified, so it also covers "only your own appointments". Family members must each be verified. | `list_appointments_for_patient`, `book`, `reschedule`, `cancel` | `not_verified`, `unknown_appointment` |
| G3 | **Age rule.** The patient's age must fit the doctor's `age_range`. Only the pediatrician is restricted (0-17). | `book` | `age_not_allowed` |
| G6 | **No time clash.** A patient cannot have two appointments at the same time. Two appointments with the same doctor on different times are allowed. | `book`, `reschedule` | `time_clash` |
| G7 | **Tool-call limit.** At most 15 tool calls per agent reply, so the agent cannot loop forever. | agent loop | (agent stops and apologises) |

Details:
- G1 is checked **before** checking whether an id exists, and an unverified appointment looks the same as a
  non-existent one. An unverified caller learns nothing about which patients or appointments exist.
- Verification lives in the conversation, not the database. A new chat always starts with nobody verified.
- Doctor schedules (`list_appointments_for_doctor`) are public and never show patient names or ids.

### Error messages
Every error is `{"error": <code>, "message": <text>}`. The message says **what went wrong and what to do next**,
so the agent can correct its own tool call. Messages are fixed templates filled with real values from the
database (names, ids, hours, ages), never written by the model. Example:

```json
{"error": "age_not_allowed",
 "message": "Dr. Sunita Iyer (Pediatrician) only sees patients aged 0-17. This patient is 18. Offer a General Physician instead: Dr. Anil Mehta (D1) or Dr. Kavita Rao (D2)."}
```

All error codes: `invalid_phone`, `invalid_name`, `invalid_age`, `already_registered`, `not_verified`,
`unknown_doctor`, `invalid_date`, `date_out_of_range`, `invalid_visit_type`, `invalid_datetime`,
`slot_not_in_schedule`, `slot_not_available`, `age_not_allowed`, `time_clash`, `unknown_appointment`,
`same_time`, `unknown_tool`, `bad_arguments`.

## Considered and dropped

| # | Idea | Why dropped | What covers the risk instead |
|---|---|---|---|
| G2 | Only change your own appointments | Already covered by G1 (owner must be verified) | G1 |
| G4 | Two-step confirm in code (hold, then confirm after the patient's next message) | Kept the tools simple. Code could only check that the patient *replied*, not that they said yes. | Prompt rule + eval checks the agent confirmed before book/reschedule/cancel |
| G5 | Lock verification after 3 non-matching name+phone attempts | Kept simple | **Known risk:** someone could try many name+phone pairs to find out who is a patient. Noted in the design note. |
| - | Minimum notice before a slot | Not needed for a one-week schedule starting before opening time | - |

## Prompt guardrails

To be decided next.

## Privacy assumption

Family booking is allowed without proof of relationship: anyone who gives a patient's correct name and phone
can view and change that patient's appointments. Acceptable for a small clinic phone line; noted in the design note.
