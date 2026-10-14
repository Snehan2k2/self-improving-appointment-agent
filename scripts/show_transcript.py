"""Print a saved test conversation in a readable form.

    uv run python scripts/show_transcript.py results/v1_baseline/S10.json
    uv run python scripts/show_transcript.py results/v2/S10.json --tools    # also show tool calls
"""

import json
import sys

GREY, RED, GREEN, RESET = "\033[90m", "\033[31m", "\033[32m", "\033[0m"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    r = json.load(open(sys.argv[1]))
    show_tools = "--tools" in sys.argv
    print(f"{r['id']}: {r['title']}  (prompt v{r.get('prompt_version')})\n")

    tools_by_turn = {}
    for t in r.get("trace", []):
        tools_by_turn.setdefault(t["turn"], []).append(t)
    turn = 0
    for t in r.get("transcript", []):
        if t["speaker"] == "patient":
            turn += 1
            print(f"PATIENT: {t['text']}")
            if show_tools:
                for c in tools_by_turn.get(turn, []):
                    res = c["result"].get("error", "ok")
                    print(f"{GREY}   -> {c['tool']}({json.dumps(c['args'])})  [{res}]{RESET}")
        else:
            print(f"AGENT:   {t['text']}\n")

    print("Checks:")
    for c in r.get("checks", []):
        mark = f"{GREEN}pass{RESET}" if c["passed"] else f"{RED}FAIL{RESET}"
        print(f"  {mark}  [{c['group']}] {c['check']}" + ("" if c["passed"] else f"  - {c['detail'][:120]}"))
    print(f"\n{'PASSED' if r.get('passed') else 'FAILED'}  weighted score {r.get('weighted_score', 0):.0%}")


if __name__ == "__main__":
    main()
