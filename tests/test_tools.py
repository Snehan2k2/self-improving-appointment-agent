import pytest

from clinic.db import ClinicDB
from clinic.tools import TOOL_NAMES, TOOL_SCHEMAS, ClinicTools


@pytest.fixture
def tools():
    return ClinicTools(ClinicDB())  # fresh copy of the seed for every test


def priya_appt(tools):
    return next(a for a in tools.db.appointments.values() if a["patient_id"] == "P001")


# ---- verify / register ------------------------------------------------------

def test_verify_known_patient_tolerates_formatting(tools):
    r = tools.verify_patient("  priya   SHARMA ", "+91 98200-11223")
    assert r == {"registered": True, "patient_id": "P001", "name": "Priya Sharma", "age": 34}


def test_verify_shared_phone_picks_person_by_name(tools):
    assert tools.verify_patient("Aarav Sharma", "9820011223")["patient_id"] == "P002"


def test_verify_wrong_phone_is_not_registered(tools):
    assert tools.verify_patient("Priya Sharma", "9999999999") == {"registered": False}


def test_verify_same_name_different_people(tools):
    a = tools.verify_patient("Rahul Verma", "9811122334")
    b = tools.verify_patient("Rahul Verma", "9900012345")
    assert a["patient_id"] != b["patient_id"] and a["age"] != b["age"]


def test_verify_invalid_phone(tools):
    assert tools.verify_patient("Priya Sharma", "12345")["error"] == "invalid_phone"


def test_register_new_patient_then_verify(tools):
    r = tools.register_patient("Arjun Menon", "9123456789", 45)
    assert r["patient_id"] == "P101"
    assert tools.verify_patient("Arjun Menon", "9123456789")["patient_id"] == "P101"


def test_register_family_member_on_shared_phone(tools):
    assert "patient_id" in tools.register_patient("Meera Sharma", "9820011223", 8)


def test_register_duplicate_rejected(tools):
    assert tools.register_patient("Priya Sharma", "9820011223", 34)["error"] == "already_registered"


@pytest.mark.parametrize("age", [-1, 121, "30", 30.5, True])
def test_register_bad_age(tools, age):
    assert tools.register_patient("X Y", "9123456789", age)["error"] == "invalid_age"


# ---- doctor schedule --------------------------------------------------------

def test_doctor_day(tools):
    r = tools.list_appointments_for_doctor("D4", "2026-10-14")
    day = r["days"][0]
    assert day["working"] and day["hours"] == ["14:00", "17:00"]
    assert set(day["booked"]).isdisjoint(day["free"])
    assert len(day["booked"]) + len(day["free"]) == 6  # 3 hours / 30 min


def test_doctor_day_off(tools):
    day = tools.list_appointments_for_doctor("D4", "2026-10-13")["days"][0]  # Kapoor off Tue
    assert day["working"] is False and day["free"] == []


def test_doctor_whole_week(tools):
    r = tools.list_appointments_for_doctor("D1")
    assert [d["date"] for d in r["days"]] == [f"2026-10-{n}" for n in range(12, 17)]
    total_free = sum(len(d["free"]) for d in r["days"])
    total_booked = sum(len(d["booked"]) for d in r["days"])
    assert total_free == total_booked == 16


def test_doctor_schedule_hides_patient_names(tools):
    r = tools.list_appointments_for_doctor("D1", "2026-10-13")
    assert "Priya" not in str(r) and "P001" not in str(r)


@pytest.mark.parametrize("doc,date,code", [
    ("D9", "2026-10-14", "unknown_doctor"),
    ("D1", "14-10-2026", "invalid_date"),
    ("D1", "2026-10-19", "date_out_of_range"),
])
def test_doctor_errors(tools, doc, date, code):
    assert tools.list_appointments_for_doctor(doc, date)["error"] == code


# ---- patient appointments ---------------------------------------------------

def test_patient_appointments(tools):
    r = tools.list_appointments_for_patient("P001")
    assert len(r["appointments"]) == 1
    assert r["appointments"][0]["start"] == "2026-10-13T11:00"
    assert r["appointments"][0]["doctor_name"] == "Dr. Anil Mehta"


def test_patient_without_appointments(tools):
    neha = tools.verify_patient("Neha Gupta", "9845098450")["patient_id"]
    assert tools.list_appointments_for_patient(neha)["appointments"] == []


# ---- book -------------------------------------------------------------------

def test_book_free_slot(tools):
    free = tools.list_appointments_for_doctor("D4", "2026-10-14")["days"][0]["free"][0]
    r = tools.book("P006", "D4", free, "first_consultation")
    assert r["status"] == "booked" and r["appointment_id"] == "A089"
    assert free not in tools.list_appointments_for_doctor("D4", "2026-10-14")["days"][0]["free"]


def test_book_taken_slot(tools):
    r = tools.book("P006", "D1", "2026-10-13T11:00", "first_consultation")  # Priya's slot
    assert r["error"] == "slot_not_available"


@pytest.mark.parametrize("start", [
    "2026-10-13T15:00",   # Kapoor is off on Tuesday
    "2026-10-14T13:00",   # before his hours
    "2026-10-14T14:15",   # not on a 30-min boundary
    "2026-10-14T17:00",   # clinic closed
])
def test_book_not_a_real_slot(tools, start):
    assert tools.book("P006", "D4", start, "first_consultation")["error"] == "slot_not_in_schedule"


@pytest.mark.parametrize("kwargs,code", [
    ({"patient_id": "P999"}, "unknown_patient"),
    ({"doctor_id": "D9"}, "unknown_doctor"),
    ({"visit_type": "checkup"}, "invalid_visit_type"),
    ({"start": "16 Oct 3pm"}, "invalid_datetime"),
    ({"start": "2026-10-19T14:00"}, "date_out_of_range"),
])
def test_book_errors(tools, kwargs, code):
    args = {"patient_id": "P006", "doctor_id": "D4", "start": "2026-10-14T14:00",
            "visit_type": "first_consultation", **kwargs}
    assert tools.book(**args)["error"] == code


# ---- reschedule / cancel ----------------------------------------------------

def test_reschedule_frees_old_slot(tools):
    appt = priya_appt(tools)
    new = tools.list_appointments_for_doctor("D1", "2026-10-16")["days"][0]["free"][0]
    r = tools.reschedule(appt["id"], new)
    assert r["start"] == new and r["appointment_id"] == appt["id"]
    assert "2026-10-13T11:00" in tools.list_appointments_for_doctor("D1", "2026-10-13")["days"][0]["free"]


def test_reschedule_errors(tools):
    appt = priya_appt(tools)
    assert tools.reschedule("A999", "2026-10-16T09:00")["error"] == "unknown_appointment"
    assert tools.reschedule(appt["id"], appt["start"])["error"] == "same_time"
    assert tools.reschedule(appt["id"], "2026-10-14T09:00")["error"] == "slot_not_in_schedule"  # Mehta off Wed
    taken = tools.list_appointments_for_doctor("D1", "2026-10-16")["days"][0]["booked"][0]
    assert tools.reschedule(appt["id"], taken)["error"] == "slot_not_available"


def test_cancel(tools):
    appt = priya_appt(tools)
    assert tools.cancel(appt["id"]) == {"appointment_id": appt["id"], "status": "cancelled"}
    assert tools.list_appointments_for_patient("P001")["appointments"] == []
    assert "2026-10-13T11:00" in tools.list_appointments_for_doctor("D1", "2026-10-13")["days"][0]["free"]
    assert tools.cancel(appt["id"])["error"] == "unknown_appointment"   # can't cancel twice
    assert tools.reschedule(appt["id"], "2026-10-16T09:00")["error"] == "unknown_appointment"


# ---- handoff ----------------------------------------------------------------

def test_handoff(tools):
    r = tools.handoff_to_human()
    assert r["handed_off"] and r["ambulance"] == "108" and "City General" in r["nearest_hospital"]
    assert tools.db.handoffs == ["H001"]


# ---- isolation + dispatcher -------------------------------------------------

def test_each_db_is_independent():
    a, b = ClinicTools(ClinicDB()), ClinicTools(ClinicDB())
    a.cancel(priya_appt(a)["id"])
    assert len(b.list_appointments_for_patient("P001")["appointments"]) == 1


def test_dispatcher(tools):
    assert tools.call("verify_patient", {"name": "Priya Sharma", "phone": "9820011223"})["registered"]
    assert tools.call("drop_tables", {})["error"] == "unknown_tool"
    assert tools.call("_check_slot", {})["error"] == "unknown_tool"  # internals not callable
    assert tools.call("book", {"patient_id": "P001"})["error"] == "bad_arguments"


def test_schemas_match_tools():
    assert len(TOOL_SCHEMAS) == 8
    for name in TOOL_NAMES:
        assert callable(getattr(ClinicTools, name))
