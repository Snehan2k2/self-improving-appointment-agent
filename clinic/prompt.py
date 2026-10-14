"""Builds the agent's system prompt from a versioned rules file + the clinic facts.

prompts/vN.yaml holds behaviour (role + rules) - this is what the improvement loop edits.
Clinic facts (doctors, hours, today's date) always come from the database, never from the YAML.
"""

from datetime import timedelta
from pathlib import Path

import yaml

from clinic.db import DAY_NAMES, ClinicDB

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def active_version() -> int:
    return int((PROMPTS_DIR / "active.txt").read_text().strip())


def load_prompt_spec(version: int | None = None) -> dict:
    v = active_version() if version is None else version
    return yaml.safe_load((PROMPTS_DIR / f"v{v}.yaml").read_text())


def _fmt_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    suffix = "am" if h < 12 else "pm"
    return f"{(h - 1) % 12 + 1}:{m:02d} {suffix}"


def _fill(text: str, values: dict) -> str:
    """Replace known {placeholders} only; any other braces in a rule are left alone."""
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def clinic_facts(db: ClinicDB) -> dict:
    c = db.clinic
    days, d = [], db.week_start
    while d <= db.week_end:
        days.append(d)
        d += timedelta(days=1)
    week_label = f"{days[0].strftime('%a %d %b')} to {days[-1].strftime('%a %d %b %Y')}"
    now = db.now
    return {
        "clinic_name": c["name"],
        "clinic_city": c["city"],
        "front_desk": c["phone"],
        "week_label": week_label,
        "today": f"{now.strftime('%A %d %B %Y')}, {_fmt_time(now.strftime('%H:%M'))}",
        "calendar": ", ".join(x.strftime("%a %d %b") + f" = {x.isoformat()}" for x in days),
        "days": days,
    }


def build_system_prompt(db: ClinicDB, version: int | None = None) -> str:
    spec = load_prompt_spec(version)
    f = clinic_facts(db)
    fill = {k: v for k, v in f.items() if isinstance(v, str)}

    doctor_lines = []
    for doc in db.doctors.values():
        work_days = [DAY_NAMES[x.weekday()] for x in f["days"] if DAY_NAMES[x.weekday()] != doc["day_off"]]
        lo, hi = doc["age_range"]
        ages = "patients under 18 only" if hi < 18 else "all ages"
        doctor_lines.append(
            f"- {doc['id']}: {doc['name']}, {doc['specialty']}. Works {', '.join(work_days)} "
            f"{_fmt_time(doc['hours'][0])}-{_fmt_time(doc['hours'][1])} (off {doc['day_off']}). Sees {ages}.")

    visit_lines = [f"- {v['id']}: {v['description']}" for v in db.visit_types.values()]
    em = db.clinic["emergency"]
    rules = [f"{i}. {_fill(r['text'], fill)}" for i, r in enumerate(spec["rules"], 1)]

    return "\n".join([
        _fill(spec["role"], fill).strip(),
        "",
        "## Clinic facts",
        f"Today is {f['today']} (India time). This is fixed for the whole conversation.",
        f"Calendar of the bookable week: {f['calendar']}.",
        f"Clinic open Mon-Fri, {_fmt_time(db.clinic['open_hours'][0])}-{_fmt_time(db.clinic['open_hours'][1])}. "
        f"All appointments are {db.clinic['slot_minutes']} minutes, starting on the hour or half hour.",
        f"Front desk phone: {db.clinic['phone']}.",
        f"The clinic has no emergency services. Ambulance: {em['ambulance']}. Nearest emergency: {em['nearest_hospital']}.",
        "",
        "Doctors:",
        *doctor_lines,
        "",
        "Visit types:",
        *visit_lines,
        "",
        "## Rules",
        *rules,
    ])
