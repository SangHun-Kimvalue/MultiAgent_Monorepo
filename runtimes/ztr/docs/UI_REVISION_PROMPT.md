# ZTR Dashboard — Revision Feedback

The initial design is excellent. Please apply the following corrections to match our actual system data and remove unused elements.

---

## Global Changes (All Pages)

### Navigation Sidebar
- **Remove** "Nodes" and "Deployments" tabs — they don't exist in our system
- Final nav items: **Dashboard** (Session List), **Live Monitor**, **Agent Stats**, **Diff Viewer**, **Settings**

### Agent Names (use these exact IDs everywhere)
| Display Name | Agent ID | Type | Color |
|---|---|---|---|
| gemini-pro | gemini-pro | gemini_api | Blue #58a6ff |
| claude-primary | claude-primary | claude_code_cli | Purple #a371f7 |
| ollama-local | ollama-local | ollama | Green #3fb950 |
| nitpicker-prefilter | nitpicker-prefilter | nitpicker | Orange #d29922 |

Do NOT use model names like "claude-3-opus" or "llama-3-8b". Use the agent IDs above.

### Cost Display
- **Remove** all dollar amounts ($0.0042 etc.) — we don't have cost calculation yet
- Replace with **token counts only** (e.g., "870 tokens" instead of "$0.0042")

---

## Page 1: Session List

### Summary Cards — fix values to match sample data:
1. **Total Sessions**: 15 (+2 today)
2. **Success Rate**: 85% (circular progress)
3. **Average Rounds**: 2.4 iterations/task
4. **Total Tokens**: 125K (not dollars)

### Table — add "Elapsed" column:
| # | Status | Verdict | Task | Target File | Rounds | Elapsed | Date |
|---|--------|---------|------|-------------|--------|---------|------|
| #015 | RUNNING | — | JWT Refresh Logic | src/auth.py | 2/5 | 45.2s | just now |
| #014 | COMPLETED | PASS | greet() function | src/__init__.py | 1/5 | 32.5s | 2 min ago |
| #013 | COMPLETED | CONDITIONAL | Error handlers | src/api.py | 3/5 | 89.1s | 1 hr ago |
| #012 | FAILED | FAIL | DB migration | src/db.py | 5/5 | 245.3s | 3 hrs ago |

### Agent badge abbreviations in table:
- G = gemini-pro (blue dot)
- C = claude-primary (purple dot)
- O = ollama-local (green dot)
- N = nitpicker-prefilter (orange dot)

---

## Page 2: Session Detail

### Header — use this exact layout:
```
Session #12                                         [PASS]
Task: "greet 함수를 구현하세요"
Target: src/__init__.py
Active Rounds: 02 / 05    Execution Time: 32.54s
```

### Insights Summary (right side):
```
BLOCKERS  MAJORS  MINORS
   00       01      02
```

### Session Billing — rename to "Token Usage":
```
Agent              Tokens    Time
gemini-pro           350    14.2s
claude-primary       520    18.1s
Total                870    32.5s
```
No dollar amounts. Only tokens and time.

### Round Detail — use this exact format:
```
Round 1: Logic Analysis

[Writer] gemini-pro          14.2s | 350 tokens | SUCCESS
──────────────────────────────────────────────
def greet(name: str) -> str:
    """주어진 이름으로 인사말을 반환합니다."""
    if not isinstance(name, str):
        raise TypeError(f"Expected str, got {type(name).__name__}")
    return f"안녕하세요, {name}님!"

[Nitpicker] nitpicker-prefilter    0.05s | Layer 2
──────────────────────────────────────────────
ruff: 0 errors | mypy: 0 errors → PASS

[Critic] claude-primary      18.1s | 520 tokens | VERIFIED
──────────────────────────────────────────────
[STATUS: PASS]
[BLOCKERS: 0] [MAJORS: 0] [MINORS: 1]

severity: minor
finding: naming convention — 'greet'보다 'create_greeting'이 더 명확

Consensus: PASS | Method: status_keyword
```

### Add: Critic Feedback Timeline (below rounds)
Show findings count per round as a horizontal bar:
```
Findings Trend
Round 1: ████████ 3 findings
Round 2: ████ 1 finding
Round 3: █ 0 findings → PASS
```
This shows how Critic feedback decreased across rounds until consensus.

---

## Page 3: Agent Stats

### Agent Cards — use correct data:
```
gemini-pro              HEALTHY
Calls: 142    Latency: 1.24s
Success: 95.2%    CircuitBreaker: CLOSED

claude-primary          HEALTHY
Calls: 89     Latency: 2.84s
Success: 98.9%    CircuitBreaker: CLOSED

ollama-local            DEGRADED
Calls: 512    Latency: 0.64s
Success: 82.4%    CircuitBreaker: OPEN

nitpicker-prefilter     HEALTHY
Calls: 2,104  Latency: 0.11s
Success: 99.8%    CircuitBreaker: CLOSED
```

### Latency Comparison bar chart — correct values:
```
gemini-pro      ████████████████ 1,247ms
claude-primary  ████████████████████████████████████████ 2,843ms
ollama-local    ████████ 640ms
nitpicker-v2    █ 110ms
```

### Error Table — rename column headers:
| Agent | Timeout | Rate Limit | Parse Error | Context Limit | Other |
|-------|---------|------------|-------------|---------------|-------|
| gemini-pro | 12 | 42 | 3 | 9 | 0 |
| claude-primary | 24 | 2 | 1 | 4 | 0 |

### Add: CircuitBreaker State History
Below the error table, add a timeline showing state transitions:
```
gemini-pro:    CLOSED ──→ OPEN (429 rate limit) ──→ HALF_OPEN ──→ CLOSED
claude-primary: CLOSED (stable)
ollama-local:  CLOSED ──→ OPEN (timeout) ──→ HALF_OPEN ──→ OPEN (timeout)
```

---

## Page 4: Live Monitor

### Session info — use Korean:
```
SESSION #15                    NODE_ACTIVE_LIVE
JWT 갱신 로직 추가              RUNNING_STAGE_02
```

### Round progress — show 3 states:
1. **Completed round**: collapsed, gray, shows verdict badge
2. **Current round**: expanded, show each step appearing one by one with spinner
3. **Future rounds**: not shown yet

### Steps within current round (appear sequentially):
```
Round 2 — Current Processing Stage

Writer    claude-primary   ◐ thinking...     (spinner)
Nitpicker                  — waiting
Critic                     — waiting
Consensus                  — waiting
```

When Writer completes:
```
Writer    claude-primary   ✓ 14.2s  350tk    (done)
Nitpicker nitpicker-pref   ◐ analyzing...    (spinner)
Critic                     — waiting
Consensus                  — waiting
```

### Session Queue (right sidebar):
```
GLOBAL SESSION QUEUE

● Running: Session #15 — Round 2/5
○ Queued:  Session #16 — 대기 중
○ Queued:  Session #17 — 대기 중
```

---

## Page 5: Diff Viewer

### Header:
```
src/__init__.py                    DIFF VIEWER
orchestrator_v2.py.bak (Original)  │  orchestrator_v2.py (Merged Result)
```

### Buttons:
- "Merge Changes" (green, primary action)
- "Rollback" (red outline, destructive action)
- "2 Unsaved Conflicts" badge (if any)

### Diff content — use Python code with Korean comments:
Left side (before):
```python
# TODO: implement greeting
def greet(name): pass
```

Right side (after):
```python
from __future__ import annotations

def greet(name: str) -> str:
    """주어진 이름으로 인사말을 반환합니다."""
    if not isinstance(name, str):
        raise TypeError(f"Expected str, got {type(name).__name__}")
    return f"안녕하세요, {name}님!"
```

### Bottom status bar:
```
Python 3.13  |  feat/async-orchestrator-v2  |  0 errors / 3 hints  |  UTF-8  |  LF
```

---

## Summary of Changes
1. Remove "Nodes", "Deployments" nav items
2. Fix all agent names to actual IDs (gemini-pro, claude-primary, ollama-local, nitpicker-prefilter)
3. Remove all dollar amounts, show tokens only
4. Add "Elapsed" column to session table
5. Add Critic Feedback Timeline (findings trend per round)
6. Add CircuitBreaker State History to Agent Stats
7. Fix sample data to match actual system values
8. Korean text for task descriptions and code comments
9. Live Monitor: sequential step appearance with spinner states
10. Rename "Session Billing" → "Token Usage"
