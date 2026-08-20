# Nitpicker Wrapper

This folder is copied into target projects by `install.sh`. Use `run_nit.py` as
the only direct entrypoint; never pass raw Git diff text through shell arguments.

## Basic Usage

```bash
ollama list
ollama pull qwen3:8b
python nitpicker/run_nit.py --self-test
python nitpicker/run_nit.py --changed
python nitpicker/run_nit.py --provider mock --changed
```

`--evidence-file` and positional review targets accept only pure repo-relative
paths. The path portion of `--file-timeout PATH=SECONDS` uses the same validator.
Drive-relative (`C:foo`), rooted (`\foo`, `/foo`), drive-absolute, UNC, and paths
that resolve outside `--repo` are blocked before the provider is called. Aliases
inside the repo are resolved to one canonical POSIX path, then deduplicated and
sorted.

Evidence is read once as raw bytes, hashed with SHA-256, decoded as strict UTF-8,
and serialized as compact JSON without newline normalization. The default
serialized bundle budget is 30000 characters. Increase it explicitly when a
review needs a larger approved evidence set; evidence is never truncated.

Each target emits one `Nitpicker-Observation` JSON line. Control remains the
strict status enum and final exit: `ALL PASS`/0, `CHANGES_REQUESTED`/2, or
`BLOCKED`/3. A whole-scope PASS requires every canonical target in the same
invocation to return `ALL PASS`; results from earlier runs are never combined.

## P3 Direct Acceptance

The final P3 evidence bundle is 66792 characters, so this command explicitly
uses a 70000-character budget and gives each of the five sequential targets 300
seconds:

```powershell
python methodology/nitpicker/run_nit.py --model qwen3:8b --keep-going --max-evidence-chars 70000 --evidence-file methodology/docs/discovery/ai-research-skills-20260810/p3-design.md --evidence-file methodology/tests/test_ai_coding_video_benchmark.py --evidence-file methodology/tests/test_ai_research_contract.py --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md=300 --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py=300 --file-timeout methodology/plugins/ai-research/schemas/video_result.schema.json=300 --file-timeout methodology/tests/test_ai_coding_video_benchmark.py=300 --file-timeout methodology/tests/test_ai_research_contract.py=300 methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py methodology/plugins/ai-research/schemas/video_result.schema.json methodology/tests/test_ai_coding_video_benchmark.py methodology/tests/test_ai_research_contract.py
```

## P3 Envelope Integration

Five worst-case inner timeouts total 1500 seconds. The outer envelope adds 300
seconds for provider startup, evidence serialization, and process overhead, so
the canonical timeout is 1800 seconds:

```powershell
python methodology/nitpicker/nit_envelope.py --backend nitpicker --model qwen3:8b --style runnit --timeout 1800 -- python methodology/nitpicker/run_nit.py --model qwen3:8b --keep-going --max-evidence-chars 70000 --evidence-file methodology/docs/discovery/ai-research-skills-20260810/p3-design.md --evidence-file methodology/tests/test_ai_coding_video_benchmark.py --evidence-file methodology/tests/test_ai_research_contract.py --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md=300 --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py=300 --file-timeout methodology/plugins/ai-research/schemas/video_result.schema.json=300 --file-timeout methodology/tests/test_ai_coding_video_benchmark.py=300 --file-timeout methodology/tests/test_ai_research_contract.py=300 methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py methodology/plugins/ai-research/schemas/video_result.schema.json methodology/tests/test_ai_coding_video_benchmark.py methodology/tests/test_ai_research_contract.py
```

An outer process timeout is envelope `BLOCKED` with exit 124 and
`nitpicker-timeout`. An inner typed file timeout is a `TIMEOUT` observation and
`run_nit.py` `BLOCKED`/3; `--style runnit` maps that to envelope `BLOCKED`/2.
These are distinct outcomes and must be reported separately.

Keep secrets and provider tokens out of the repository. Edit
`nitpicker.config.json`, not the example file, for local persistent settings.
