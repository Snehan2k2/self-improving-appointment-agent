import pytest

from clinic.db import ClinicDB
from clinic.tools import TOOL_NAMES, TOOL_SCHEMAS, ClinicTools


@pytest.fixture
def tools():
    return ClinicTools(ClinicDB())  # fresh copy of the seed + empty verified set for every test


def login(tools, pid):
    """Verify a seed patient the way the agent would: by name + phone."""
    p = tools.db.patients[pid]
    assert tools.verify_patient(p["name"], p["phone"])["patient_id"] == pid
    return pid


def priya_appt(tools):
    return next(a for a in tools.db.appointments.values() if a["patient_id"] == "P001")


def free_slot(tools, doctor_id, date, i=0):
    return tools.list_appointments_for_doctor(doctor_id, date)["days"][0]["free"][i]


# ---- verify / register ------------------------------------------------------

def test_verify_known_patient_tolerates_formatting(tools):
    r = tools.verify_patient("  priya   SHARMA ", "+91 98200-11223")
    assert r == {"registered": True, "patient_id": "P001", "name": "Priya Sharma", "age": 34}


def test_verify_shared_phone_picks_person_by_name(tools):
    assert tools.verify_patient("Aarav Sharma", "9820011223")["patient_id"] == "P002"


def test_verify_wrong_phone_is_not_registered(tools):
    assert tools.verify_patient("Priya Sharma", "9999999999") == {"registered": False}
    assert tools.verified == set()


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


# ---- doctor schedule (public, no verification needed) -----------------------

def test_doctor_day(tools):
    day = tools.list_appointments_for_doctor("D4", "2026-10-14")["days"][0]
    assert day["working"] and day["hours"] == ["14:00", "17:00"]
    assert set(day["booked"]).isdisjoint(day["free"])
    assert len(day["booked"]) + len(day["free"]) == 6  # 3 hours / 30 min


def test_doctor_day_off(tools):
    day = tools.list_appointments_for_doctor("D4", "2026-10-13")["days"][0]  # Kapoor off Tue
    assert day["working"] is False and day["free"] == []


def test_doctor_whole_week(tools):
    r = tools.list_appointments_for_doctor("D1")
    assert [d["date"] for d in r["days"]] == [f"2026-10-{n}" for n in range(12, 17)]
    assert sum(len(d["free"]) for d in r["days"]) == sum(len(d["booked"]) for d in r["days"]) == 16


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
    login(tools, "P001")
    appts = tools.list_appointments_for_patient("P001")["appointments"]
    assert len(appts) == 1
    assert appts[0]["start"] == "2026-10-13T11:00" and appts[0]["doctor_name"] == "Dr. Anil Mehta"


def test_patient_without_appointments(tools):
    login(tools, "P006")
    assert tools.list_appointments_for_patient("P006")["appointments"] == []


# ---- book -------------------------------------------------------------------

def test_book_free_slot(tools):
    login(tools, "P006")
    slot = free_slot(tools, "D4", "2026-10-14")
    r = tools.book("P006", "D4", slot, "first_consultation")
    assert r["status"] == "booked" and r["appointment_id"] == "A089"
    assert slot not in tools.list_appointments_for_doctor("D4", "2026-10-14")["days"][0]["free"]


def test_book_taken_slot(tools):
    login(tools, "P006")
    assert tools.book("P006", "D1", "2026-10-13T11:00", "first_consultation")["error"] == "slot_not_available"


@pytest.mark.parametrize("start", [
    "2026-10-13T15:00",   # Kapoor is off on Tuesday
    "2026-10-14T13:00",   # before his hours
    "2026-10-14T14:15",   # not on a 30-min boundary
    "2026-10-14T17:00",   # clinic closed
])
def test_book_not_a_real_slot(tools, start):
    login(tools, "P006")
    assert tools.book("P006", "D4", start, "first_consultation")["error"] == "slot_not_in_schedule"


@pytest.mark.parametrize("kwargs,code", [
    ({"doctor_id": "D9"}, "unknown_doctor"),
    ({"visit_type": "checkup"}, "invalid_visit_type"),
    ({"start": "16 Oct 3pm"}, "invalid_datetime"),
    ({"start": "2026-10-19T14:00"}, "date_out_of_range"),
])
def test_book_errors(tools, kwargs, code):
    login(tools, "P006")
    args = {"patient_id": "P006", "doctor_id": "D4", "start": "2026-10-14T14:00",
            "visit_type": "first_consultation", **kwargs}
    assert tools.book(**args)["error"] == code


# ---- reschedule / cancel ----------------------------------------------------

def test_reschedule_frees_old_slot(tools):
    login(tools, "P001")
    appt = priya_appt(tools)
    new = free_slot(tools, "D1", "2026-10-16")
    r = tools.reschedule(appt["id"], new)
    assert r["start"] == new and r["appointment_id"] == appt["id"]
    assert "2026-10-13T11:00" in tools.list_appointments_for_doctor("D1", "2026-10-13")["days"][0]["free"]


def test_reschedule_errors(tools):
    login(tools, "P001")
    appt = priya_appt(tools)
    assert tools.reschedule("A999", "2026-10-16T09:00")["error"] == "unknown_appointment"
    assert tools.reschedule(appt["id"], appt["start"])["error"] == "same_time"
    assert tools.reschedule(appt["id"], "2026-10-14T09:00")["error"] == "slot_not_in_schedule"  # Mehta off Wed
    taken = tools.list_appointments_for_doctor("D1", "2026-10-16")["days"][0]["booked"][0]
    assert tools.reschedule(appt["id"], taken)["error"] == "slot_not_available"


def test_cancel(tools):
    login(tools, "P001")
    appt = priya_appt(tools)
    assert tools.cancel(appt["id"]) == {"appointment_id": appt["id"], "status": "cancelled"}
    assert tools.list_appointments_for_patient("P001")["appointments"] == []
    assert "2026-10-13T11:00" in tools.list_appointments_for_doctor("D1", "2026-10-13")["days"][0]["free"]
    assert tools.cancel(appt["id"])["error"] == "unknown_appointment"   # can't cancel twice
    assert tools.reschedule(appt["id"], "2026-10-16T09:00")["error"] == "unknown_appointment"


# ---- G1: verified first -----------------------------------------------------

def test_g1_unverified_patient_blocked_everywhere(tools):
    appt = priya_appt(tools)
    slot = free_slot(tools, "D1", "2026-10-16")
    assert tools.list_appointments_for_patient("P001")["error"] == "not_verified"
    assert tools.book("P001", "D1", slot, "follow_up")["error"] == "not_verified"
    assert tools.reschedule(appt["id"], slot)["error"] == "unknown_appointment"
    assert tools.cancel(appt["id"])["error"] == "unknown_appointment"
    assert appt["status"] == "booked" and appt["start"] == "2026-10-13T11:00"  # nothing changed


def test_g1_message_tells_agent_how_to_recover(tools):
    msg = tools.list_appointments_for_patient("P001")["message"]
    assert "verify_patient" in msg and "phone" in msg


def test_g1_unknown_id_looks_same_as_unverified(tools):
    """An unverified caller can't probe which patient ids exist."""
    assert tools.list_appointments_for_patient("P999")["error"] == "not_verified"
    assert tools.list_appointments_for_patient("P001")["error"] == "not_verified"


def test_g1_verifying_one_patient_does_not_unlock_another(tools):
    login(tools, "P006")  # Neha
    assert tools.cancel(priya_appt(tools)["id"])["error"] == "unknown_appointment"
    assert tools.list_appointments_for_patient("P001")["error"] == "not_verified"


def test_g1_failed_verification_unlocks_nothing(tools):
    tools.verify_patient("Priya Sharma", "9999999999")
    assert tools.list_appointments_for_patient("P001")["error"] == "not_verified"


def test_g1_family_members_each_verified(tools):
    login(tools, "P001")  # Priya
    assert tools.list_appointments_for_patient("P002")["error"] == "not_verified"  # son not yet
    login(tools, "P002")
    assert len(tools.list_appointments_for_patient("P002")["appointments"]) == 1


def test_g1_registration_verifies(tools):
    pid = tools.register_patient("Arjun Menon", "9123456789", 45)["patient_id"]
    assert tools.list_appointments_for_patient(pid)["appointments"] == []


def test_g1_new_conversation_starts_unverified():
    db = ClinicDB()
    first = ClinicTools(db)
    login(first, "P001")
    second = ClinicTools(db)  # same data, new chat
    assert second.list_appointments_for_patient("P001")["error"] == "not_verified"


# ---- G3: age rule -----------------------------------------------------------

def test_g3_adult_cannot_see_pediatrician(tools):
    login(tools, "P010")  # Kabir Joshi, 18
    r = tools.book("P010", "D3", free_slot(tools, "D3", "2026-10-12"), "first_consultation")
    assert r["error"] == "age_not_allowed"
    assert "0-17" in r["message"] and "18" in r["message"] and "D1" in r["message"] and "D2" in r["message"]


def test_g3_seventeen_can_see_pediatrician(tools):
    login(tools, "P009")  # Ananya Pillai, 17
    r = tools.book("P009", "D3", free_slot(tools, "D3", "2026-10-12"), "first_consultation")
    assert r["status"] == "booked"


def test_g3_child_can_see_other_doctors(tools):
    login(tools, "P008")  # Ishaan Reddy, 9
    r = tools.book("P008", "D6", free_slot(tools, "D6", "2026-10-12"), "first_consultation")
    assert r["status"] == "booked"


# ---- G6: no time clash ------------------------------------------------------

def common_free(tools, d1, d2, date):
    """A time free for both doctors (D2 13-17 and D6 11-15 overlap on Monday 13-15)."""
    a = tools.list_appointments_for_doctor(d1, date)["days"][0]["free"]
    b = tools.list_appointments_for_doctor(d2, date)["days"][0]["free"]
    return sorted(set(a) & set(b))[0]


def test_g6_no_two_appointments_at_same_time(tools):
    login(tools, "P006")
    t = common_free(tools, "D2", "D6", "2026-10-12")
    first = tools.book("P006", "D2", t, "first_consultation")
    r = tools.book("P006", "D6", t, "first_consultation")
    assert r["error"] == "time_clash" and first["appointment_id"] in r["message"]


def test_g6_same_doctor_twice_is_allowed(tools):
    login(tools, "P006")
    a = tools.book("P006", "D4", free_slot(tools, "D4", "2026-10-14", 0), "first_consultation")
    b = tools.book("P006", "D4", free_slot(tools, "D4", "2026-10-15", 0), "report_review")
    assert a["status"] == b["status"] == "booked"


def test_g6_reschedule_into_own_other_appointment_time(tools):
    login(tools, "P006")
    t1 = free_slot(tools, "D4", "2026-10-14", 0)
    t2 = free_slot(tools, "D4", "2026-10-14", 1)
    tools.book("P006", "D4", t1, "first_consultation")
    second = tools.book("P006", "D4", t2, "first_consultation")
    # t1 is taken by her own appointment with the same doctor -> slot_not_available
    assert tools.reschedule(second["appointment_id"], t1)["error"] == "slot_not_available"


def test_g6_reschedule_clash_with_other_doctor(tools):
    login(tools, "P006")
    t = common_free(tools, "D2", "D6", "2026-10-12")
    tools.book("P006", "D2", t, "first_consultation")
    other_time = next(s for s in tools.list_appointments_for_doctor("D6", "2026-10-12")["days"][0]["free"] if s != t)
    other = tools.book("P006", "D6", other_time, "first_consultation")
    assert tools.reschedule(other["appointment_id"], t)["error"] == "time_clash"


# ---- handoff ----------------------------------------------------------------

def test_handoff(tools):
    r = tools.handoff_to_human()
    assert r["handed_off"] and r["ambulance"] == "108" and "City General" in r["nearest_hospital"]
    assert tools.db.handoffs == ["H001"]


# ---- isolation + dispatcher -------------------------------------------------

def test_each_db_is_independent():
    a, b = ClinicTools(ClinicDB()), ClinicTools(ClinicDB())
    login(a, "P001")
    a.cancel(priya_appt(a)["id"])
    login(b, "P001")
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


def test_every_error_message_says_what_to_do():
    """Error messages must be actionable, not just a code."""
    t = ClinicTools(ClinicDB())
    errors = [
        t.verify_patient("A", "1"),
        t.list_appointments_for_patient("P001"),
        t.cancel("A001"),
        t.list_appointments_for_doctor("D9"),
        t.list_appointments_for_doctor("D1", "2026-10-19"),
    ]
    login(t, "P010")
    errors.append(t.book("P010", "D3", "2026-10-12T10:00", "first_consultation"))
    errors.append(t.book("P010", "D1", "2026-10-12T09:15", "first_consultation"))
    for e in errors:
        assert "error" in e and len(e["message"]) > 40, e
