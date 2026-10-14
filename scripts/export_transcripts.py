"""Write every saved test conversation as a readable Markdown file.

    uv run python scripts/export_transcripts.py

Reads results/v1_baseline/ and results/v2/, writes results/transcripts/<id>_v<N>.md
"""

import json
from pathlib import Path

SOURCES = {1: Path("results/v1_baseline"), 2: Path("results/v2")}
OUT = Path("results/transcripts")


def to_markdown(r: dict, version: int) -> str:
    lines = [f"# {r['id']}: {r['title']} (prompt v{version})", "",
             f"**Result: {'PASSED' if r['passed'] else 'FAILED'}**, weighted score {r['weighted_score']:.0%}", "",
             "## Conversation", ""]
    tools_by_turn = {}
    for t in r.get("trace", []):
        tools_by_turn.setdefault(t["turn"], []).append(t)
    turn = 0
    for t in r.get("transcript", []):
        if t["speaker"] == "patient":
            turn += 1
            lines.append(f"**Patient:** {t['text']}")
            lines.append("")
            for c in tools_by_turn.get(turn, []):
                status = c["result"].get("error", "ok")
                lines.append(f"> tool: `{c['tool']}({json.dumps(c['args'])})` -> {status}")
            if tools_by_turn.get(turn):
                lines.append("")
        else:
            lines.append(f"**Agent:** {t['text']}")
            lines.append("")
    lines += ["## Checks", "", "| Result | Group | Check | Detail |", "|---|---|---|---|"]
    for c in r.get("checks", []):
        detail = c["detail"].replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {'pass' if c['passed'] else '**FAIL**'} | {c['group']} | {c['check']} | {detail} |")
    return "\n".join(lines) + "\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    for version, folder in SOURCES.items():
        for p in sorted(folder.glob("S*.json")):
            r = json.loads(p.read_text())
            (OUT / f"{r['id']}_v{version}.md").write_text(to_markdown(r, version))
            n += 1
    print(f"wrote {n} files to {OUT}/")


if __name__ == "__main__":
    main()
