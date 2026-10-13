# Agent tools

The scheduling agent can only act on the clinic through these tools.
Guardrails (who may call what, and when) are in [guardrails.md](guardrails.md).

## Data the tools work on

- `data/clinic.json` is the **seed**. It is never modified.
- Every chat session (and every eval run) gets its **own fresh copy** of the seed as its working database.
  Bookings made in one test cannot leak into another, and the seed always stays clean.
- Every appointment has an `id` (e.g. `A014`). Reschedule and cancel refer to it.
- Times are clinic local time, format `YYYY-MM-DDTHH:MM` (date + time, e.g. `2026-10-16T15:00`).
  Dates are `YYYY-MM-DD`.
- **Static clinic info is in the system prompt, not a tool**: clinic hours, the 6 doctors
  (id, name, specialty, day off, hours, age range), visit types, today's date, front-desk phone.
  It is small and never changes, so the agent always has it without a tool call.

## Typical flow

```
patient: "I have a skin rash. I'm Neha Gupta, 9845098450. I'm free Wed 2-4pm."
  -> agent asks for name + phone if not given
  -> verify_patient(name, phone)                  registered? if not -> register_patient
  -> agent picks the doctor from the system prompt (dermatologist = D4)
  -> list_appointments_for_doctor("D4", "2026-10-14")
  -> agent offers free slots inside 2-4pm, patient picks one
  -> book(patient_id, "D4", "2026-10-14T14:30", "first_consultation")
```

## Tools

### 1. `verify_patient`
Checks whether a patient is already registered.

| Input | Type | Notes |
|---|---|---|
| `name` | string | Full name as the patient gives it |
| `phone` | string | 10-digit mobile number |

Output:
- found: `{"registered": true, "patient_id": "P001", "name": "Priya Sharma", "age": 34}`
- not found: `{"registered": false}`

The agent must ask for name and phone **before** calling this tool.
Name + phone together identify one patient (a phone can be shared by a family).

### 2. `register_patient`
Adds a new patient. Use only after `verify_patient` returned `registered: false`.

| Input | Type | Notes |
|---|---|---|
| `name` | string | Full name |
| `phone` | string | 10-digit mobile number |
| `age` | integer | Years |

Output: `{"patient_id": "P101", "name": "...", "phone": "...", "age": 29}`

### 3. `list_appointments_for_doctor`
Shows when a doctor is busy and free.

| Input | Type | Notes |
|---|---|---|
| `doctor_id` | string | e.g. `D4` |
| `date` | string, **optional** | `YYYY-MM-DD`. If omitted: the whole week. |

Output (one entry per day):
```json
{"doctor_id": "D4", "days": [
  {"date": "2026-10-14", "working": true, "hours": ["14:00", "17:00"],
   "booked": ["2026-10-14T14:00", "2026-10-14T15:00"],
   "free":   ["2026-10-14T14:30", "2026-10-14T15:30", "2026-10-14T16:00", "2026-10-14T16:30"]}
]}
```
- `booked` shows **times only, never patient names** (privacy).
- On the doctor's day off: `"working": false`, empty lists.
- `free` = working hours minus booked, computed by code (never stored).

### 4. `list_appointments_for_patient`
All active appointments of one patient.

| Input | Type | Notes |
|---|---|---|
| `patient_id` | string | From `verify_patient` / `register_patient` |

Output: list of `{"appointment_id", "doctor_id", "doctor_name", "start", "visit_type", "status"}`.

### 5. `book`
Books one slot.

| Input | Type | Notes |
|---|---|---|
| `patient_id` | string | |
| `doctor_id` | string | |
| `start` | string | Date + time, must be a free slot of that doctor |
| `visit_type` | string | `first_consultation` / `follow_up` / `report_review` |

Output: `{"appointment_id": "A089", "doctor_name": "...", "start": "...", "visit_type": "...", "status": "booked"}`

### 6. `reschedule`
Moves an existing appointment to a new date + time with the **same doctor**.
The old time is simply overwritten (same appointment id), so the old slot becomes free.

| Input | Type | Notes |
|---|---|---|
| `appointment_id` | string | e.g. `A014` |
| `new_start` | string | New date + time, e.g. `2026-10-16T15:00`. Must be a free slot of that doctor. |

Output: the updated appointment.

### 7. `cancel`
Cancels an appointment.

| Input | Type | Notes |
|---|---|---|
| `appointment_id` | string | |

Output: `{"appointment_id": "A014", "status": "cancelled"}`. The slot becomes free again.
The row is kept with status `cancelled` (not deleted), so there is a record of what happened.

### 8. `handoff_to_human`
Used **only for possible emergencies**. No input.

Output:
```json
{"handed_off": true,
 "ticket_id": "H001",
 "ambulance": "108",
 "nearest_hospital": "City General Hospital, 100 Feet Road, Indiranagar (2 km) - 24x7 emergency",
 "message": "This clinic has no emergency services. Call 108 now or go to City General Hospital."}
```
The agent stops scheduling and passes this on to the patient.

Non-emergency cases (angry patient, billing, prescriptions, anything outside scheduling) do not use a tool:
the agent gives the clinic front-desk number from the system prompt.

## Errors

Every tool returns errors in one shape so the agent can recover:

```json
{"error": "slot_not_available", "message": "2026-10-14T15:00 is already booked for Dr. Rohan Kapoor"}
```

The message says what went wrong and what to do next. All error codes and the guardrails
behind them: [guardrails.md](guardrails.md).
