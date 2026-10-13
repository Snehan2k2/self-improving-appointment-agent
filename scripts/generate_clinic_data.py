"""Generate the base clinic dataset (data/clinic.json).

Deterministic: fixed seed, so every run produces the same file.
Run:  python3 scripts/generate_clinic_data.py
"""

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 42
OUT = Path(__file__).resolve().parent.parent / "data" / "clinic.json"

WEEK_START = date(2026, 10, 12)  # Monday
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
SLOT_MINUTES = 30
BOOKED_FRACTION = 0.5

CLINIC = {
    "name": "Sanjeevani Clinic",
    "city": "Bengaluru",
    "address": "14, 5th Cross, Indiranagar, Bengaluru 560038",
    "phone": "080-41234567",
    "timezone": "Asia/Kolkata",
    "today": "2026-10-12T08:30",
    "open_days": DAYS,
    "open_hours": ["09:00", "17:00"],
    "schedule_week": ["2026-10-12", "2026-10-16"],
    "slot_minutes": SLOT_MINUTES,
    "emergency": {
        "has_emergency_services": False,
        "ambulance": "108",
        "nearest_hospital": "City General Hospital, 100 Feet Road, Indiranagar (2 km) - 24x7 emergency",
    },
}

VISIT_TYPES = [
    {"id": "first_consultation", "name": "First consultation",
     "description": "First visit to this doctor for a new problem."},
    {"id": "follow_up", "name": "Follow-up",
     "description": "Return visit to the same doctor about the same problem."},
    {"id": "report_review", "name": "Report review",
     "description": "Show the doctor test results they asked for."},
]

# age_range is inclusive. Only the pediatrician is restricted.
DOCTORS = [
    {"id": "D1", "name": "Dr. Anil Mehta", "specialty": "General Physician",
     "day_off": "Wed", "hours": ["09:00", "13:00"], "age_range": [0, 120]},
    {"id": "D2", "name": "Dr. Kavita Rao", "specialty": "General Physician",
     "day_off": "Fri", "hours": ["13:00", "17:00"], "age_range": [0, 120]},
    {"id": "D3", "name": "Dr. Sunita Iyer", "specialty": "Pediatrician",
     "day_off": "Thu", "hours": ["10:00", "14:00"], "age_range": [0, 17]},
    {"id": "D4", "name": "Dr. Rohan Kapoor", "specialty": "Dermatologist",
     "day_off": "Tue", "hours": ["14:00", "17:00"], "age_range": [0, 120]},
    {"id": "D5", "name": "Dr. Farhan Siddiqui", "specialty": "Orthopedics",
     "day_off": "Mon", "hours": ["09:00", "12:00"], "age_range": [0, 120]},
    {"id": "D6", "name": "Dr. Meera Nair", "specialty": "ENT",
     "day_off": "Wed", "hours": ["11:00", "15:00"], "age_range": [0, 120]},
]

# Hand-placed patients that the eval scenarios can rely on.
# (name, age, phone, booking or None) ; booking = (doctor_id, "YYYY-MM-DD HH:MM", visit_type)
FEATURED = [
    # Family sharing one phone: mother (GP) + son (pediatrician) + grandmother (no booking)
    ("Priya Sharma", 34, "9820011223", ("D1", "2026-10-13 11:00", "follow_up")),
    ("Aarav Sharma", 6, "9820011223", ("D3", "2026-10-14 10:30", "first_consultation")),
    ("Kamala Sharma", 63, "9820011223", None),
    # Same name, different people
    ("Rahul Verma", 41, "9811122334", ("D2", "2026-10-15 14:00", "report_review")),
    ("Rahul Verma", 27, "9900012345", ("D6", "2026-10-12 12:00", "first_consultation")),
    # Unbooked adult and unbooked parent + child on one phone
    ("Neha Gupta", 29, "9845098450", None),
    ("Lakshmi Reddy", 38, "9731122110", None),
    ("Ishaan Reddy", 9, "9731122110", None),
    # Teen (17) - edge of pediatric age range, booked with dermatologist
    ("Ananya Pillai", 17, "9008877665", ("D4", "2026-10-12 15:00", "first_consultation")),
    # Just turned 18 - may NOT see pediatrician
    ("Kabir Joshi", 18, "9886655443", None),
    # Elderly, orthopedics
    ("Ramesh Iyengar", 72, "9448012345", ("D5", "2026-10-16 09:30", "follow_up")),
]

FIRST_F = ["Aishwarya", "Divya", "Pooja", "Sneha", "Kavya", "Meghana", "Shruti", "Deepa",
           "Anjali", "Ritu", "Swati", "Nandini", "Bhavana", "Harini", "Lavanya", "Pallavi",
           "Sangeeta", "Usha", "Revathi", "Farah", "Zoya", "Simran", "Gurpreet", "Tanvi"]
FIRST_M = ["Arjun", "Vikram", "Suresh", "Karthik", "Manoj", "Rohit", "Sanjay", "Naveen",
           "Imran", "Rajesh", "Venkat", "Harsh", "Siddharth", "Aditya", "Pranav", "Gaurav",
           "Mohan", "Ravi", "Abdul", "Joseph", "Harpreet", "Tushar", "Nikhil", "Deepak"]
FIRST_KID = ["Vihaan", "Aditi", "Riya", "Krish", "Saanvi", "Advik", "Myra", "Reyansh",
             "Anika", "Dhruv", "Kiara", "Arnav", "Ira", "Shaurya", "Navya", "Ayaan"]
SURNAMES = ["Patel", "Nair", "Menon", "Kulkarni", "Deshpande", "Chatterjee", "Banerjee",
            "Rao", "Shetty", "Hegde", "Gowda", "Khan", "Qureshi", "Singh", "Gill", "Agarwal",
            "Bansal", "Mishra", "Tiwari", "Das", "Bose", "Fernandes", "D'Souza", "Krishnan",
            "Subramanian", "Naidu", "Jain", "Saxena", "Malhotra", "Bhat"]


def slots_for(doc):
    """All 30-min slot start times for a doctor over the week."""
    out = []
    for i, day in enumerate(DAYS):
        if day == doc["day_off"]:
            continue
        d = WEEK_START + timedelta(days=i)
        t = datetime.combine(d, datetime.strptime(doc["hours"][0], "%H:%M").time())
        end = datetime.combine(d, datetime.strptime(doc["hours"][1], "%H:%M").time())
        while t + timedelta(minutes=SLOT_MINUTES) <= end:
            out.append(t)
            t += timedelta(minutes=SLOT_MINUTES)
    return out


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def main():
    rng = random.Random(SEED)
    patients, appointments = [], []
    used_phones, used_names = set(), set()

    def add_patient(name, age, phone):
        pid = f"P{len(patients) + 1:03d}"
        patients.append({"id": pid, "name": name, "phone": phone, "age": age})
        used_phones.add(phone)
        used_names.add(name)
        return pid

    def add_appt(pid, doc_id, start, vtype):
        appointments.append({"id": None, "patient_id": pid, "doctor_id": doc_id,
                             "start": start, "visit_type": vtype, "status": "booked"})

    def new_phone():
        while True:
            p = str(rng.choice([6, 7, 8, 9])) + "".join(str(rng.randint(0, 9)) for _ in range(9))
            if p not in used_phones:
                return p

    def new_name(firsts):
        while True:
            n = f"{rng.choice(firsts)} {rng.choice(SURNAMES)}"
            if n not in used_names:
                return n

    # 1. Featured patients
    for name, age, phone, booking in FEATURED:
        pid = add_patient(name, age, phone)
        if booking:
            doc_id, start, vtype = booking
            add_appt(pid, doc_id, start.replace(" ", "T"), vtype)

    # 2. Fill each doctor to 50% booked, one new patient per booking
    vtypes = [v["id"] for v in VISIT_TYPES]
    vweights = [0.5, 0.3, 0.2]
    for doc in DOCTORS:
        all_slots = [fmt(s) for s in slots_for(doc)]
        target = int(len(all_slots) * BOOKED_FRACTION)
        taken = {a["start"] for a in appointments if a["doctor_id"] == doc["id"]}
        free = [s for s in all_slots if s not in taken]
        for start in sorted(rng.sample(free, target - len(taken))):
            if doc["specialty"] == "Pediatrician":
                age = rng.randint(0, 17)
                name = new_name(FIRST_KID)
                # Most kids share a phone with a parent who is also registered (no booking)
                if rng.random() < 0.5:
                    phone = new_phone()
                    parent_first = rng.choice(FIRST_F + FIRST_M)
                    add_patient(f"{parent_first} {name.split(' ', 1)[1]}", age + rng.randint(22, 40), phone)
                else:
                    phone = new_phone()
            else:
                age = rng.randint(19, 80)
                name = new_name(FIRST_F if rng.random() < 0.5 else FIRST_M)
                phone = new_phone()
            pid = add_patient(name, age, phone)
            add_appt(pid, doc["id"], start, rng.choices(vtypes, vweights)[0])

    # Stable appointment ids in time order
    appointments.sort(key=lambda a: (a["start"], a["doctor_id"]))
    for i, a in enumerate(appointments, 1):
        a["id"] = f"A{i:03d}"

    data = {"clinic": CLINIC, "visit_types": VISIT_TYPES, "doctors": DOCTORS,
            "patients": patients, "appointments": appointments}
    validate(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    summary(data)


def validate(data):
    docs = {d["id"]: d for d in data["doctors"]}
    pats = {p["id"]: p for p in data["patients"]}
    valid_slots = {d["id"]: {fmt(s) for s in slots_for(d)} for d in data["doctors"]}
    seen = set()
    per_patient = {}
    for a in data["appointments"]:
        doc, pat = docs[a["doctor_id"]], pats[a["patient_id"]]
        assert a["start"] in valid_slots[doc["id"]], f"{a} outside doctor hours/day off"
        assert (doc["id"], a["start"]) not in seen, f"double booking {a}"
        seen.add((doc["id"], a["start"]))
        lo, hi = doc["age_range"]
        assert lo <= pat["age"] <= hi, f"age rule broken: {pat} with {doc['name']}"
        per_patient.setdefault(pat["id"], []).append(a)
    assert all(len(v) == 1 for v in per_patient.values()), "a patient has >1 booking"
    for p in data["patients"]:
        assert len(p["phone"]) == 10 and p["phone"].isdigit(), p
    keys = [(p["name"].casefold(), p["phone"]) for p in data["patients"]]
    assert len(keys) == len(set(keys)), "name + phone must identify exactly one patient"
    for d in data["doctors"]:
        n = sum(1 for a in data["appointments"] if a["doctor_id"] == d["id"])
        assert n == int(len(valid_slots[d["id"]]) * BOOKED_FRACTION), (d["id"], n)


def summary(data):
    total_slots = sum(len(slots_for(d)) for d in data["doctors"])
    booked = len(data["appointments"])
    booked_pids = {a["patient_id"] for a in data["appointments"]}
    phones = {}
    for p in data["patients"]:
        phones.setdefault(p["phone"], []).append(p["name"])
    shared = {k: v for k, v in phones.items() if len(v) > 1}
    print(f"wrote {OUT}")
    print(f"doctors: {len(data['doctors'])}  slots: {total_slots}  booked: {booked} ({booked / total_slots:.0%})")
    print(f"patients: {len(data['patients'])}  with booking: {len(booked_pids)}  "
          f"without: {len(data['patients']) - len(booked_pids)}")
    print(f"shared phones: {len(shared)}")
    for d in data["doctors"]:
        n_all = len(slots_for(d))
        n = sum(1 for a in data["appointments"] if a["doctor_id"] == d["id"])
        print(f"  {d['id']} {d['name']:<20} {d['specialty']:<18} off {d['day_off']}  "
              f"{d['hours'][0]}-{d['hours'][1]}  {n}/{n_all} booked")


if __name__ == "__main__":
    main()
