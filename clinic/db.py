"""Working database for one chat session or one eval run.

data/clinic.json is the seed and is never modified. Each ClinicDB is an
independent in-memory copy, so runs cannot affect each other.
"""

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic.json"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_FMT = "%Y-%m-%dT%H:%M"


def _load_seed(path=SEED_PATH):
    return json.loads(Path(path).read_text())


class ClinicDB:
    def __init__(self, seed=None):
        data = copy.deepcopy(seed if seed is not None else _load_seed())
        self.clinic = data["clinic"]
        self.visit_types = {v["id"]: v for v in data["visit_types"]}
        self.doctors = {d["id"]: d for d in data["doctors"]}
        self.patients = {p["id"]: p for p in data["patients"]}
        self.appointments = {a["id"]: a for a in data["appointments"]}
        self.handoffs = []
        self.now = datetime.strptime(self.clinic["today"], TIME_FMT)
        week = self.clinic["schedule_week"]
        self.week_start = date.fromisoformat(week[0])
        self.week_end = date.fromisoformat(week[1])
        self._snapshot()

    def _snapshot(self) -> None:
        """Remember the current state; changes() reports differences from it."""
        self._initial_appts = copy.deepcopy(self.appointments)
        self._initial_patient_ids = set(self.patients)

    # ---- per-scenario setup (eval only) -----------------------------------

    RESERVED_PATIENT = {"id": "P900", "name": "Reserved Slot", "phone": "0000000000", "age": 40}

    def apply_setup(self, ops: list[dict]) -> None:
        """Apply scenario setup on top of the seed, then treat the result as the starting state.

        ops:
          {op: fill_day, doctor: D6, date: 2026-10-15}            book every free slot that day
          {op: free_slot, doctor: D4, start: 2026-10-15T14:00}    remove whatever is booked there
          {op: add_appointment, patient: P006, doctor: D4, start: ..., visit_type: ...}
        """
        for op in ops or []:
            kind = op["op"]
            if kind == "fill_day":
                self.patients.setdefault("P900", dict(self.RESERVED_PATIENT))
                for start in self.free_slots(op["doctor"], date.fromisoformat(op["date"])):
                    self._add("P900", op["doctor"], start, "first_consultation")
            elif kind == "free_slot":
                for aid in [a["id"] for a in self.appointments.values()
                            if a["doctor_id"] == op["doctor"] and a["start"] == op["start"]]:
                    del self.appointments[aid]
            elif kind == "add_appointment":
                self._add(op["patient"], op["doctor"], op["start"], op.get("visit_type", "first_consultation"))
            else:
                raise ValueError(f"unknown setup op: {kind}")
        self._snapshot()

    def _add(self, patient_id: str, doctor_id: str, start: str, visit_type: str) -> str:
        aid = self.next_id(self.appointments, "A")
        self.appointments[aid] = {"id": aid, "patient_id": patient_id, "doctor_id": doctor_id,
                                  "start": start, "visit_type": visit_type, "status": "booked"}
        return aid

    # ---- schedule helpers -------------------------------------------------

    def in_schedule_week(self, d: date) -> bool:
        return self.week_start <= d <= self.week_end

    def works_on(self, doctor_id: str, d: date) -> bool:
        doc = self.doctors[doctor_id]
        return self.in_schedule_week(d) and DAY_NAMES[d.weekday()] != doc["day_off"]

    def doctor_slots(self, doctor_id: str, d: date) -> list[str]:
        """All slot start times for a doctor on a day (booked or not)."""
        if not self.works_on(doctor_id, d):
            return []
        doc = self.doctors[doctor_id]
        step = timedelta(minutes=self.clinic["slot_minutes"])
        t = datetime.combine(d, datetime.strptime(doc["hours"][0], "%H:%M").time())
        end = datetime.combine(d, datetime.strptime(doc["hours"][1], "%H:%M").time())
        out = []
        while t + step <= end:
            out.append(t.strftime(TIME_FMT))
            t += step
        return out

    def booked_starts(self, doctor_id: str, d: date, exclude_id: str | None = None) -> list[str]:
        prefix = d.isoformat()
        return sorted(
            a["start"] for a in self.appointments.values()
            if a["doctor_id"] == doctor_id and a["status"] == "booked"
            and a["start"].startswith(prefix) and a["id"] != exclude_id
        )

    def free_slots(self, doctor_id: str, d: date, exclude_id: str | None = None) -> list[str]:
        taken = set(self.booked_starts(doctor_id, d, exclude_id))
        return [s for s in self.doctor_slots(doctor_id, d) if s not in taken]

    # ---- what changed in this run -----------------------------------------

    def changes(self) -> dict:
        """Everything that differs from the state this DB started with."""
        appts = []
        for aid, a in self.appointments.items():
            before = self._initial_appts.get(aid)
            if before != a:
                appts.append({"appointment_id": aid, "before": before, "after": copy.deepcopy(a)})
        return {
            "new_patients": [self.patients[p] for p in self.patients if p not in self._initial_patient_ids],
            "appointments": appts,
            "handoffs": list(self.handoffs),
        }

    # ---- id helpers -------------------------------------------------------

    def next_id(self, table: dict, prefix: str) -> str:
        n = max((int(k[len(prefix):]) for k in table), default=0) + 1
        return f"{prefix}{n:03d}"
