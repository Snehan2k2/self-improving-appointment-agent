# Walkthrough recording

**Watch it here:** https://drive.google.com/file/d/1UVk8HtcCFF6mzuTdzSyUk4GJP_gmz1y1/view

A short walk through the codebase and the improvement loop:

1. **Opening:** what the project is, and the v1 to v2 result.
2. **The data:** how the synthetic clinic is generated, and the tricky cases planted in it.
3. **The agent:** hard rules in code, judgment calls in the prompt, locked safety rules.
4. **The eval:** how a test is graded, and a failure it catches (test S10, chest pain).
5. **The loop:** the fixer's one change, the re-run, and the score moving from 82% to 98%.
6. **Close:** what still fails, and where to read more.

Files shown in the video:

- [`README.md`](../README.md)
- [`scripts/generate_clinic_data.py`](../scripts/generate_clinic_data.py)
- [`clinic/tools.py`](../clinic/tools.py)
- [`prompts/v1.yaml`](../prompts/v1.yaml)
- [`evals/scenarios/S10_chest_pain.yaml`](../evals/scenarios/S10_chest_pain.yaml)
- [`results/transcripts/S10_v1.md`](../results/transcripts/S10_v1.md)
- [`results/v2/loop_output.txt`](../results/v2/loop_output.txt)
- [`results/transcripts/S10_v2.md`](../results/transcripts/S10_v2.md)
- [`DESIGN.md`](../DESIGN.md)
