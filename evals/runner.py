"""Load scenarios, play each one (agent <-> patient simulator), grade it."""

import copy
import time
from pathlib import Path

import yaml

from clinic.agent import Agent
from clinic.db import ClinicDB
from clinic.llm import LLMError
from evals.checks import LLMJudge, grade_run
from evals.simulator import PatientSimulator

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


def load_scenarios(split: str | None = None, only: list[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(SCENARIO_DIR.glob("S*.yaml")):
        sc = yaml.safe_load(path.read_text())
        if split and sc["split"] != split:
            continue
        if only and sc["id"] not in only:
            continue
        out.append(sc)
    return out


def play(scenario: dict, agent_llm, sim_llm, prompt_version: int | None = None) -> dict:
    """Run one conversation. Returns everything the grader needs."""
    db = ClinicDB()
    db.apply_setup(scenario.get("setup", []))
    agent = Agent(agent_llm, db=db, prompt_version=prompt_version)
    sim = PatientSimulator(sim_llm, scenario["patient"], clinic_name=db.clinic["name"])

    transcript, ended_by = [], "max_turns"
    patient_msg = sim.opening()
    for _ in range(scenario.get("max_turns", 10)):
        transcript.append({"speaker": "patient", "text": patient_msg})
        transcript.append({"speaker": "agent", "text": agent.respond(patient_msg)})
        patient_msg, done = sim.next_message(transcript)
        if done:
            # The patient's last message is still delivered (it may contain the final "yes").
            if patient_msg:
                transcript.append({"speaker": "patient", "text": patient_msg})
                transcript.append({"speaker": "agent", "text": agent.respond(patient_msg)})
            ended_by = "patient"
            break

    return {
        "prompt_version": agent.prompt_version,
        "transcript": transcript,
        "trace": agent.trace,
        "changes": db.changes(),
        "patients": copy.deepcopy(db.patients),
        "messages": agent.messages,
        "ended_by": ended_by,
    }


def run_scenario(scenario: dict, make_llm, prompt_version: int | None = None) -> dict:
    """make_llm(role) -> LLM for role in {"agent", "patient", "judge"}."""
    started = time.time()
    llms = {role: make_llm(role) for role in ("agent", "patient", "judge")}
    base = {"id": scenario["id"], "title": scenario["title"], "group": scenario["group"],
            "split": scenario["split"]}
    try:
        run = play(scenario, llms["agent"], llms["patient"], prompt_version)
        graded = grade_run(scenario, run, LLMJudge(llms["judge"]))
        status = "pass" if graded["passed"] else "fail"
    except LLMError as e:
        run, graded, status = {}, {"passed": False, "checks": [], "group_scores": {}, "weighted_score": 0.0}, "error"
        base["error"] = str(e)
    usage = {role: getattr(llm, "calls", []) for role, llm in llms.items()}
    return {**base, "status": status, **graded, **run, "llm_calls": usage,
            "seconds": round(time.time() - started, 1)}


def regrade(saved: dict, scenario: dict, judge_llm) -> dict:
    """Re-grade a saved run with the current checks, without replaying the conversation."""
    if saved.get("status") == "error":
        return saved
    graded = grade_run(scenario, saved, LLMJudge(judge_llm))
    out = {**saved, **graded, "status": "pass" if graded["passed"] else "fail"}
    out["llm_calls"] = {**saved.get("llm_calls", {}), "rescore_judge": getattr(judge_llm, "calls", [])}
    return out
