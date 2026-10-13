"""The 8 tools the scheduling agent can use. Spec: docs/tools.md, guardrails: docs/guardrails.md

Every tool returns a dict. Errors always look like:
    {"error": "<code>", "message": "<what went wrong and what to do next>"}
Messages are fixed templates filled with values from the database, never model-generated.

Code guardrails enforced here:
    G1  verified first   - patient data / a patient's appointments can only be used after that
                           patient was verified or registered in this conversation
    G3  age rule         - patient's age must fit the doctor's age_range (pediatrician 0-17)
    G6  no time clash    - a patient cannot have two appointments at the same time
(G7, the 15-tool-calls-per-reply limit, lives in the agent loop.)
"""

import re
from datetime import date, datetime

from clinic.db import TIME_FMT, ClinicDB

MAX_TOOL_CALLS_PER_TURN = 15  # G7, enforced by the agent loop


def _err(code: str, message: str) -> dict:
    return {"error": code, "message": message}


def _norm_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _norm_phone(phone: str) -> str | None:
    """Accepts '98200 11223', '+91-9820011223', '09820011223'. Returns 10 digits or None."""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, TIME_FMT)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class ClinicTools:
    def __init__(self, db: ClinicDB):
        self.db = db
        self.verified: set[str] = set()  # patient ids verified/registered in THIS conversation (G1)

    # ---- dispatcher -------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None) if name in TOOL_NAMES else None
        if fn is None:
            return _err("unknown_tool", f"No tool named '{name}'. Available tools: {sorted(TOOL_NAMES)}.")
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return _err("bad_arguments", f"Bad arguments for {name}: {e}. Check the tool's input schema and retry.")

    # ---- patients ---------------------------------------------------------

    def verify_patient(self, name: str, phone: str) -> dict:
        p_phone = _norm_phone(phone)
        if p_phone is None:
            return _err("invalid_phone",
                        f"'{phone}' is not a valid 10-digit mobile number. Ask the caller to repeat the number.")
        for p in self.db.patients.values():
            if p["phone"] == p_phone and _norm_name(p["name"]) == _norm_name(name):
                self.verified.add(p["id"])
                return {"registered": True, "patient_id": p["id"], "name": p["name"], "age": p["age"]}
        return {"registered": False}

    def register_patient(self, name: str, phone: str, age: int) -> dict:
        if not name or not name.strip():
            return _err("invalid_name", "Name is required. Ask the caller for the patient's full name.")
        p_phone = _norm_phone(phone)
        if p_phone is None:
            return _err("invalid_phone",
                        f"'{phone}' is not a valid 10-digit mobile number. Ask the caller to repeat the number.")
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 120:
            return _err("invalid_age",
                        f"Age must be a whole number of years between 0 and 120 (got {age!r}). "
                        "Ask the caller for the patient's age.")
        if self.verify_patient(name, p_phone).get("registered"):
            return _err("already_registered",
                        "A patient with this name and phone is already registered and is now verified. "
                        "Call verify_patient with the same name and phone to get their patient_id.")
        pid = self.db.next_id(self.db.patients, "P")
        patient = {"id": pid, "name": " ".join(name.split()), "phone": p_phone, "age": age}
        self.db.patients[pid] = patient
        self.verified.add(pid)
        return {"patient_id": pid, "name": patient["name"], "phone": p_phone, "age": age}

    def list_appointments_for_patient(self, patient_id: str) -> dict:
        problem = self._require_verified(patient_id)
        if problem:
            return problem
        appts = sorted(
            (a for a in self.db.appointments.values()
             if a["patient_id"] == patient_id and a["status"] == "booked"),
            key=lambda a: a["start"],
        )
        return {"patient_id": patient_id, "appointments": [self._public_appt(a) for a in appts]}

    # ---- doctors ----------------------------------------------------------

    def list_appointments_for_doctor(self, doctor_id: str, date: str | None = None) -> dict:
        if doctor_id not in self.db.doctors:
            return _err("unknown_doctor", f"No doctor with id '{doctor_id}'. {self._doctor_ids()}")
        if date is None:
            days = [self.db.week_start.fromordinal(o) for o in
                    range(self.db.week_start.toordinal(), self.db.week_end.toordinal() + 1)]
        else:
            d = _parse_date(date)
            if d is None:
                return _err("invalid_date", f"'{date}' is not a valid date. Use YYYY-MM-DD, e.g. 2026-10-14.")
            if not self.db.in_schedule_week(d):
                return _err("date_out_of_range", self._out_of_range_msg(date))
            days = [d]

        doc = self.db.doctors[doctor_id]
        out = []
        for d in days:
            if self.db.works_on(doctor_id, d):
                out.append({"date": d.isoformat(), "working": True, "hours": doc["hours"],
                            "booked": self.db.booked_starts(doctor_id, d),
                            "free": self.db.free_slots(doctor_id, d)})
            else:
                out.append({"date": d.isoformat(), "working": False, "reason": "day off",
                            "booked": [], "free": []})
        return {"doctor_id": doctor_id, "doctor_name": doc["name"], "days": out}

    # ---- appointments -----------------------------------------------------

    def book(self, patient_id: str, doctor_id: str, start: str, visit_type: str) -> dict:
        problem = self._require_verified(patient_id)
        if problem:
            return problem
        if doctor_id not in self.db.doctors:
            return _err("unknown_doctor", f"No doctor with id '{doctor_id}'. {self._doctor_ids()}")
        if visit_type not in self.db.visit_types:
            return _err("invalid_visit_type",
                        f"'{visit_type}' is not a visit type. Use one of: {', '.join(self.db.visit_types)}.")
        problem = (self._check_age(patient_id, doctor_id)
                   or self._check_slot(doctor_id, start)
                   or self._check_clash(patient_id, start))
        if problem:
            return problem
        aid = self.db.next_id(self.db.appointments, "A")
        appt = {"id": aid, "patient_id": patient_id, "doctor_id": doctor_id,
                "start": start, "visit_type": visit_type, "status": "booked"}
        self.db.appointments[aid] = appt
        return self._public_appt(appt)

    def reschedule(self, appointment_id: str, new_start: str) -> dict:
        appt, problem = self._own_active_appt(appointment_id)
        if problem:
            return problem
        if new_start == appt["start"]:
            return _err("same_time", f"Appointment {appointment_id} is already at {new_start}. "
                                     "Ask the patient which new time they want.")
        problem = (self._check_slot(appt["doctor_id"], new_start, exclude_id=appointment_id)
                   or self._check_clash(appt["patient_id"], new_start, exclude_id=appointment_id))
        if problem:
            return problem
        appt["start"] = new_start
        return self._public_appt(appt)

    def cancel(self, appointment_id: str) -> dict:
        appt, problem = self._own_active_appt(appointment_id)
        if problem:
            return problem
        appt["status"] = "cancelled"  # row kept for the record; the slot is free again
        return {"appointment_id": appointment_id, "status": "cancelled"}

    # ---- emergency --------------------------------------------------------

    def handoff_to_human(self) -> dict:
        em = self.db.clinic["emergency"]
        ticket = f"H{len(self.db.handoffs) + 1:03d}"
        self.db.handoffs.append(ticket)
        return {
            "handed_off": True,
            "ticket_id": ticket,
            "ambulance": em["ambulance"],
            "nearest_hospital": em["nearest_hospital"],
            "message": (f"This clinic has no emergency services. Call {em['ambulance']} now "
                        f"or go to {em['nearest_hospital'].split(',')[0]}."),
        }

    # ---- guardrail checks -------------------------------------------------

    def _require_verified(self, patient_id: str) -> dict | None:
        """G1. Checked before existence, so unverified callers learn nothing about which ids exist."""
        if patient_id in self.verified:
            return None
        return _err("not_verified",
                    f"Patient '{patient_id}' has not been verified in this conversation. "
                    "Ask the caller for the patient's full name and 10-digit phone number, call verify_patient "
                    "(or register_patient if they are new), then retry with the patient_id it returns.")

    def _own_active_appt(self, appointment_id: str) -> tuple[dict | None, dict | None]:
        """G1 for appointment-id tools: the appointment's owner must be verified in this conversation."""
        appt = self.db.appointments.get(appointment_id)
        if appt is None or appt["status"] != "booked" or appt["patient_id"] not in self.verified:
            return None, _err(
                "unknown_appointment",
                f"No active appointment '{appointment_id}' for a patient verified in this conversation. "
                "Verify the patient first, then call list_appointments_for_patient and use an appointment_id "
                "from that list.")
        return appt, None

    def _check_age(self, patient_id: str, doctor_id: str) -> dict | None:
        """G3."""
        p, doc = self.db.patients[patient_id], self.db.doctors[doctor_id]
        lo, hi = doc["age_range"]
        if lo <= p["age"] <= hi:
            return None
        alternatives = [d for d in self.db.doctors.values()
                        if d["specialty"] == "General Physician" and d["age_range"][0] <= p["age"] <= d["age_range"][1]]
        alt = " or ".join(f"{d['name']} ({d['id']})" for d in alternatives)
        return _err("age_not_allowed",
                    f"{doc['name']} ({doc['specialty']}) only sees patients aged {lo}-{hi}. "
                    f"This patient is {p['age']}. Offer a General Physician instead: {alt}.")

    def _check_clash(self, patient_id: str, start: str, exclude_id: str | None = None) -> dict | None:
        """G6. All slots are 30 min on :00/:30, so a clash means the same start time."""
        for a in self.db.appointments.values():
            if (a["patient_id"] == patient_id and a["status"] == "booked"
                    and a["start"] == start and a["id"] != exclude_id):
                doc = self.db.doctors[a["doctor_id"]]
                return _err("time_clash",
                            f"This patient already has appointment {a['id']} with {doc['name']} at {start}. "
                            "A patient cannot have two appointments at the same time. Offer a different time.")
        return None

    def _check_slot(self, doctor_id: str, start: str, exclude_id: str | None = None) -> dict | None:
        dt = _parse_dt(start)
        if dt is None:
            return _err("invalid_datetime",
                        f"'{start}' is not a valid time. Use YYYY-MM-DDTHH:MM, e.g. 2026-10-16T15:00.")
        if not self.db.in_schedule_week(dt.date()):
            return _err("date_out_of_range", self._out_of_range_msg(start))
        doc = self.db.doctors[doctor_id]
        if start not in self.db.doctor_slots(doctor_id, dt.date()):
            return _err("slot_not_in_schedule",
                        f"{doc['name']} has no slot at {start}. They work {doc['hours'][0]}-{doc['hours'][1]}, "
                        f"are off on {doc['day_off']}, and slots are {self.db.clinic['slot_minutes']} minutes "
                        f"starting on :00 or :30. Call list_appointments_for_doctor to see the free slots.")
        if start not in self.db.free_slots(doctor_id, dt.date(), exclude_id=exclude_id):
            return _err("slot_not_available",
                        f"{start} is already booked for {doc['name']}. "
                        "Call list_appointments_for_doctor and offer a time from the 'free' list.")
        return None

    def _out_of_range_msg(self, value: str) -> str:
        return (f"{value} is outside the bookable week. Appointments can only be made from "
                f"{self.db.week_start} to {self.db.week_end} (Mon-Fri). Tell the patient and offer a day in that week.")

    def _doctor_ids(self) -> str:
        return "Valid doctors: " + ", ".join(f"{d['id']} {d['name']} ({d['specialty']})"
                                              for d in self.db.doctors.values()) + "."

    def _public_appt(self, a: dict) -> dict:
        return {"appointment_id": a["id"], "patient_id": a["patient_id"],
                "doctor_id": a["doctor_id"], "doctor_name": self.db.doctors[a["doctor_id"]]["name"],
                "start": a["start"], "visit_type": a["visit_type"], "status": a["status"]}


# ---- tool definitions sent to the model (Anthropic tool format) -----------

_DT = "Date and time in clinic local time, format YYYY-MM-DDTHH:MM, e.g. 2026-10-16T15:00."

TOOL_SCHEMAS = [
    {
        "name": "verify_patient",
        "description": "Check whether a patient is registered, using their full name and 10-digit phone number. "
                       "Ask the caller for both before calling. A successful match verifies the patient for the "
                       "rest of this conversation.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["name", "phone"],
                         "properties": {"name": {"type": "string"}, "phone": {"type": "string"}}},
    },
    {
        "name": "register_patient",
        "description": "Register a new patient. Only call after verify_patient returned registered=false. "
                       "The new patient is verified for the rest of this conversation.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["name", "phone", "age"],
                         "properties": {"name": {"type": "string"}, "phone": {"type": "string"},
                                        "age": {"type": "integer"}}},
    },
    {
        "name": "list_appointments_for_doctor",
        "description": "Show a doctor's booked and free 30-minute slots for one date, or the whole week if date "
                       "is omitted. Only offer patients times from the 'free' list.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["doctor_id"],
                         "properties": {"doctor_id": {"type": "string"},
                                        "date": {"type": "string", "description": "YYYY-MM-DD (optional)"}}},
    },
    {
        "name": "list_appointments_for_patient",
        "description": "List a verified patient's active appointments, with appointment ids.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id"],
                         "properties": {"patient_id": {"type": "string"}}},
    },
    {
        "name": "book",
        "description": "Book a free slot for a verified patient with a doctor.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["patient_id", "doctor_id", "start", "visit_type"],
                         "properties": {"patient_id": {"type": "string"}, "doctor_id": {"type": "string"},
                                        "start": {"type": "string", "description": _DT},
                                        "visit_type": {"type": "string",
                                                       "enum": ["first_consultation", "follow_up",
                                                                "report_review"]}}},
    },
    {
        "name": "reschedule",
        "description": "Move a verified patient's appointment to a new free slot with the same doctor.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["appointment_id", "new_start"],
                         "properties": {"appointment_id": {"type": "string"},
                                        "new_start": {"type": "string", "description": _DT}}},
    },
    {
        "name": "cancel",
        "description": "Cancel a verified patient's appointment.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["appointment_id"],
                         "properties": {"appointment_id": {"type": "string"}}},
    },
    {
        "name": "handoff_to_human",
        "description": "Use only for a possible medical emergency. Alerts clinic staff and returns the ambulance "
                       "number and nearest emergency hospital to pass on to the patient.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}
