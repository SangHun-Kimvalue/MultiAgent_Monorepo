# Zero-Token Roundtable (ZTR)

> Multi-agent code review orchestration system. Claude writes, Gemini reviews, Nitpicker pre-filters.

## Quick Start

### 1. Clone + Install

```bash
git clone https://dev.azure.com/shkim2/The%20great%20Roundtable/_git/The%20great%20Roundtable
cd ZeroTokenRoundtable
pip install -e ".[dev]"
```

### 2. API Keys

`Config.ini` (project root):
```ini
GEMINI_API_KEY = your-gemini-api-key
```

Claude CLI:
```bash
claude auth login   # Max plan required for -p flag
```

### 3. Run

```bash
# Full pipeline: Writer -> Nitpicker -> Critic -> Consensus -> Merge
python -m src run --task "fibonacci 함수 구현" --target src/__init__.py

# Auto-approve (CI/CD)
python -m src run --task "버그 수정" --target src/bug.py --auto-approve

# Dashboard
python -m src web   # http://localhost:8000
```

## Architecture

```
User -> CLI (ztr run)
         |
         v
    ConfigLoader -> agents.config.yaml
         |
         v
    AgentRegistry -> 5 agents (claude, gemini-writer, gemini-critic, nitpicker, ollama)
         |
         v
    AsyncOrchestrator (round loop, max 5 rounds)
         |
         +-- Writer (Claude Sonnet / Gemini Pro)
         |     +-- Writer Output Harness [CODE_START]/[CODE_END]
         |     +-- Quality Gate (empty function detection)
         |
         +-- Nitpicker Pre-filter (ruff + mypy, free)
         |
         +-- Critic (Gemini Flash, balanced_critic persona)
         |     +-- Format Guide ([STATUS:] + severity/finding)
         |     +-- Quality Gate (praise-only detection)
         |
         +-- ConsensusEngine (3-layer parse: keyword > counts > heuristic)
         |     +-- Round Diff Analyzer (code change rate)
         |
         +-- ASTMerger (3-tier extract: tag > fence > raw)
               +-- PostMergeVerifier (ast + ruff + mypy)
```

## CLI Commands

| Command | Description |
|---|---|
| `python -m src run --task "..." --target file.py` | Full pipeline |
| `python -m src invoke --agent gemini-writer --prompt "..."` | Single agent call |
| `python -m src health --agent claude-primary` | Health check |
| `python -m src list-agents` | Show configured agents |
| `python -m src history` | Recent session history |
| `python -m src stats` | Agent performance stats |
| `python -m src feedback --session N --agree` | Rate Critic accuracy |
| `python -m src web` | Start dashboard (port 8000) |

## Configuration

Edit `src/config/agents.config.yaml`:

```yaml
agents:
  - id: "claude-primary"
    type: "claude_code_cli"
    config:
      model: "claude-sonnet-4-20250514"  # Sonnet for lower quota usage

  - id: "gemini-writer"
    type: "gemini_api"
    config:
      model: "gemini-2.5-pro"           # High quality writing

  - id: "gemini-critic"
    type: "gemini_api"
    config:
      model: "gemini-2.5-flash"         # Fast + cheap review

orchestration:
  writer_rotation: ["claude-primary", "gemini-writer"]
  critic_rotation: ["gemini-critic"]
  fallback_chain: ["gemini-writer", "claude-primary", "ollama-local"]
```

## Critic Judgment Criteria

```
REJECTED    : crash, security, data loss, resource leak
CONDITIONAL : SOLID violation, missing error handling, no abstraction
PASS        : major=0, SOLID compliant, proper error handling, Fail-Fast
```

## Harness Engineering

Every LLM interaction is wrapped in input/output/quality harnesses:

| Harness | What it does |
|---|---|
| H1 Writer Output | [CODE_START] tag enforcement + 3-tier code extraction |
| H2 Round Escalation | 2x same verdict + no improvement -> early termination |
| H3 Evaluation | User feedback (agree/disagree/override) for Critic accuracy |
| H4 Quality Gate | Detect empty PASS, praise-only findings, verdict inconsistency |
| H5 Round Diff | difflib code change rate -> smart escalation |
| H6 Post-Merge | ast.parse + ruff + mypy after file merge |

## Testing

```bash
# Unit tests (206, ~10s)
python -m pytest tests/ --ignore=tests/test_e2e.py -v

# E2E tests (5, ~60s, needs GEMINI_API_KEY)
python -m pytest tests/test_e2e.py -v -s
```

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+, asyncio |
| Config | PyYAML + Pydantic V2 |
| HTTP | httpx AsyncClient |
| DB | SQLite WAL |
| Web | FastAPI + Jinja2 + HTMX + SSE |
| Test | pytest + pytest-asyncio |

## Docs

- `docs/DESIGN.md` - Full design document (v2.8)
- `docs/LESSONS_LEARNED.md` - 13 lessons learned
- `docs/UI_DESIGN_PROMPT.md` - Stitch design spec
