"""The 8 tools the scheduling agent can use. Spec: docs/tools.md

Every tool returns a dict. Errors always look like:
    {"error": "<code>", "message": "<human readable>"}

Only data-correctness checks live here for now (doctor exists, slot is real
and free, date is in the schedule week). Policy guardrails (verified in this
chat, age limits, ownership, patient confirmation) are added in a later step.
"""

import re
from datetime import date, datetime

from clinic.db import TIME_FMT, ClinicDB


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


class ClinicTools:
    def __init__(self, db: ClinicDB):
        self.db = db

    # ---- dispatcher -------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None) if name in TOOL_NAMES else None
        if fn is None:
            return _err("unknown_tool", f"No tool named '{name}'.")
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return _err("bad_arguments", f"Bad arguments for {name}: {e}")

    # ---- patients ---------------------------------------------------------

    def verify_patient(self, name: str, phone: str) -> dict:
        p_phone = _norm_phone(phone)
        if p_phone is None:
            return _err("invalid_phone", "Phone must be a 10-digit mobile number.")
        for p in self.db.patients.values():
            if p["phone"] == p_phone and _norm_name(p["name"]) == _norm_name(name):
                return {"registered": True, "patient_id": p["id"], "name": p["name"], "age": p["age"]}
        return {"registered": False}

    def register_patient(self, name: str, phone: str, age: int) -> dict:
        if not name or not name.strip():
            return _err("invalid_name", "Name is required.")
        p_phone = _norm_phone(phone)
        if p_phone is None:
            return _err("invalid_phone", "Phone must be a 10-digit mobile number.")
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 120:
            return _err("invalid_age", "Age must be a whole number between 0 and 120.")
        if self.verify_patient(name, p_phone).get("registered"):
            return _err("already_registered", "A patient with this name and phone already exists.")
        pid = self.db.next_id(self.db.patients, "P")
        patient = {"id": pid, "name": " ".join(name.split()), "phone": p_phone, "age": age}
        self.db.patients[pid] = patient
        return {"patient_id": pid, "name": patient["name"], "phone": p_phone, "age": age}

    def list_appointments_for_patient(self, patient_id: str) -> dict:
        if patient_id not in self.db.patients:
            return _err("unknown_patient", f"No patient with id '{patient_id}'.")
        appts = sorted(
            (a for a in self.db.appointments.values()
             if a["patient_id"] == patient_id and a["status"] == "booked"),
            key=lambda a: a["start"],
        )
        return {"patient_id": patient_id, "appointments": [self._public_appt(a) for a in appts]}

    # ---- doctors ----------------------------------------------------------

    def list_appointments_for_doctor(self, doctor_id: str, date: str | None = None) -> dict:
        if doctor_id not in self.db.doctors:
            return _err("unknown_doctor", f"No doctor with id '{doctor_id}'.")
        if date is None:
            days = [self.db.week_start.fromordinal(o) for o in
                    range(self.db.week_start.toordinal(), self.db.week_end.toordinal() + 1)]
        else:
            d = _parse_date(date)
            if d is None:
                return _err("invalid_date", "Date must be YYYY-MM-DD.")
            if not self.db.in_schedule_week(d):
                return _err("date_out_of_range",
                            f"Bookings are open only from {self.db.week_start} to {self.db.week_end}.")
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
        if patient_id not in self.db.patients:
            return _err("unknown_patient", f"No patient with id '{patient_id}'.")
        if doctor_id not in self.db.doctors:
            return _err("unknown_doctor", f"No doctor with id '{doctor_id}'.")
        if visit_type not in self.db.visit_types:
            return _err("invalid_visit_type",
                        f"visit_type must be one of {sorted(self.db.visit_types)}.")
        problem = self._check_slot(doctor_id, start)
        if problem:
            return problem
        aid = self.db.next_id(self.db.appointments, "A")
        appt = {"id": aid, "patient_id": patient_id, "doctor_id": doctor_id,
                "start": start, "visit_type": visit_type, "status": "booked"}
        self.db.appointments[aid] = appt
        return self._public_appt(appt)

    def reschedule(self, appointment_id: str, new_start: str) -> dict:
        appt = self.db.appointments.get(appointment_id)
        if appt is None or appt["status"] != "booked":
            return _err("unknown_appointment", f"No active appointment with id '{appointment_id}'.")
        if new_start == appt["start"]:
            return _err("same_time", "The appointment is already at that time.")
        problem = self._check_slot(appt["doctor_id"], new_start, exclude_id=appointment_id)
        if problem:
            return problem
        appt["start"] = new_start
        return self._public_appt(appt)

    def cancel(self, appointment_id: str) -> dict:
        appt = self.db.appointments.get(appointment_id)
        if appt is None or appt["status"] != "booked":
            return _err("unknown_appointment", f"No active appointment with id '{appointment_id}'.")
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

    # ---- internals --------------------------------------------------------

    def _check_slot(self, doctor_id: str, start: str, exclude_id: str | None = None) -> dict | None:
        dt = _parse_dt(start)
        if dt is None:
            return _err("invalid_datetime", "Time must be YYYY-MM-DDTHH:MM, e.g. 2026-10-16T15:00.")
        d = dt.date()
        if not self.db.in_schedule_week(d):
            return _err("date_out_of_range",
                        f"Bookings are open only from {self.db.week_start} to {self.db.week_end}.")
        doc = self.db.doctors[doctor_id]
        if start not in self.db.doctor_slots(doctor_id, d):
            return _err("slot_not_in_schedule",
                        f"{doc['name']} has no slot at {start} (works {doc['hours'][0]}-{doc['hours'][1]}, "
                        f"off on {doc['day_off']}, {self.db.clinic['slot_minutes']}-minute slots).")
        if start not in self.db.free_slots(doctor_id, d, exclude_id=exclude_id):
            return _err("slot_not_available", f"{start} is already booked for {doc['name']}.")
        return None

    def _public_appt(self, a: dict) -> dict:
        return {"appointment_id": a["id"], "patient_id": a["patient_id"],
                "doctor_id": a["doctor_id"], "doctor_name": self.db.doctors[a["doctor_id"]]["name"],
                "start": a["start"], "visit_type": a["visit_type"], "status": a["status"]}


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---- tool definitions sent to the model (Anthropic tool format) -----------

_DT = "Date and time in clinic local time, format YYYY-MM-DDTHH:MM, e.g. 2026-10-16T15:00."

TOOL_SCHEMAS = [
    {
        "name": "verify_patient",
        "description": "Check whether a patient is registered, using their full name and 10-digit phone number. "
                       "Ask the patient for both before calling.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["name", "phone"],
                         "properties": {"name": {"type": "string"}, "phone": {"type": "string"}}},
    },
    {
        "name": "register_patient",
        "description": "Register a new patient. Only call after verify_patient returned registered=false.",
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
        "description": "List a patient's active appointments, with appointment ids.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id"],
                         "properties": {"patient_id": {"type": "string"}}},
    },
    {
        "name": "book",
        "description": "Book a free slot for a patient with a doctor.",
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
        "description": "Move an existing appointment to a new free slot with the same doctor.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["appointment_id", "new_start"],
                         "properties": {"appointment_id": {"type": "string"},
                                        "new_start": {"type": "string", "description": _DT}}},
    },
    {
        "name": "cancel",
        "description": "Cancel an existing appointment.",
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
