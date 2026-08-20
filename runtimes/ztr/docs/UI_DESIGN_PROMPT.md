# ZTR Dashboard — UI Design Prompt (for Google Stitch)

## Project Overview

Build a dashboard UI for "Zero-Token Roundtable (ZTR)" — a multi-agent code review orchestration system. Multiple AI agents (Gemini, Claude, Ollama) take turns writing and reviewing code. The dashboard shows sessions, round-by-round discussions, decisions, agent performance, and live progress.

**Tech style**: Clean, professional developer tool. Dark theme preferred (like GitHub Dark or Linear). Monospace font for code blocks. Minimal decoration, high information density.

---

## Page 1: Session List (Main Page)

### Layout
- Top bar: "ZTR Dashboard" logo/title on left, live indicator dot (green pulsing) + "LIVE" badge on right
- Below top bar: Summary stat cards in a row (4 cards)
- Main content: Session table with filters

### Summary Cards (horizontal row, 4 cards)
1. **Total Sessions** — number, with small "+N today" subtitle
2. **Success Rate** — percentage with green/red color, circular progress indicator
3. **Avg Rounds** — average rounds per session
4. **Total Tokens** — total tokens consumed across all agents

### Filter Bar (above table)
- Verdict filter: chips/pills for ALL | PASS | CONDITIONAL | FAIL | TIMEOUT | ERROR (selectable, highlight active)
- Date range picker (last 7 days / 30 days / all)
- Search box: search by task description

### Session Table
Columns:
| # | Status | Verdict | Task | Target File | Rounds | Time | Agents | Date |
|---|--------|---------|------|-------------|--------|------|--------|------|

- **Status**: Color-coded badge — green "completed", red "failed", yellow "cancelled", blue "running"
- **Verdict**: Icon + text — checkmark PASS (green), warning CONDITIONAL (yellow), X FAIL (red), clock TIMEOUT (gray), alert ERROR (red)
- **Task**: Truncated to ~40 chars, full text on hover tooltip
- **Target File**: Monospace, file path
- **Rounds**: "2/5" format (completed/max)
- **Time**: "32.5s" format
- **Agents**: Small avatar-like badges showing which agents participated (G=Gemini, C=Claude, O=Ollama, N=Nitpicker)
- **Date**: Relative time ("2 min ago", "1 hour ago")

Each row is clickable, navigates to Session Detail page.

**Running sessions** should appear at the top with a subtle pulsing border/glow animation.

---

## Page 2: Session Detail

### Layout
- Breadcrumb: Sessions > Session #12
- Session header card (summary info)
- Round timeline (main content, scrollable)
- Right sidebar: Findings summary + Cost breakdown

### Session Header Card
```
Session #12                                    [PASS] badge
Task: "greet 함수를 구현하세요. name을 받아서 인사말을 반환합니다."
Target: src/__init__.py
Rounds: 2/5  |  Time: 32.5s  |  Started: 2026-04-13 17:30:12
```

### Round Timeline (vertical, each round is an expandable card)

Each round card contains 3 sections stacked vertically:

#### Writer Section
```
[Writer] gemini-pro                    14.2s  |  350 tokens
─────────────────────────────────────────────────────────
def greet(name: str) -> str:
    """주어진 이름을 사용하여 인사말을 생성하고 반환합니다."""
    if not isinstance(name, str):
        raise TypeError(f"Expected str, got {type(name).__name__}")
    return f"안녕하세요, {name}님!"
```
- Agent name as badge (color-coded per agent)
- Latency and token count on the right
- Code block with syntax highlighting (python)

#### Nitpicker Section (if present, collapsible)
```
[Nitpicker] nitpicker-prefilter        0.8s  |  Layer 2
─────────────────────────────────────────────────────────
Status: PASS (ruff: 0 errors, mypy: 0 errors)
```
- Green check if PASS, red X if REJECT
- Show ruff/mypy error counts

#### Critic Section
```
[Critic] claude-primary                18.1s  |  520 tokens
─────────────────────────────────────────────────────────
[STATUS: PASS]
[BLOCKERS: 0] [MAJORS: 0] [MINORS: 1]

severity: minor
finding: naming convention — 'greet'보다 'create_greeting'이 더 명확할 수 있음
```
- Status line color-coded (green PASS, red REJECTED, yellow CONDITIONAL)
- Findings listed as severity-colored tags (red=blocker, orange=major, gray=minor)

#### Consensus Badge (at bottom of round card)
```
Consensus: PASS  |  Parse method: status_keyword  |  "Critic이 [STATUS: PASS] 판정"
```
- Large verdict badge
- Parse method shown as subtle label (helps debug parsing issues)

### Right Sidebar

#### Findings Summary
Visual summary of all findings across all rounds:
- Blockers: 0 (red)
- Majors: 1 (orange)  
- Minors: 2 (gray)
Show trend: "Round 1: 3 findings -> Round 2: 1 finding" with a small sparkline or progress indicator

#### Critic Feedback Timeline
Show how findings decreased across rounds. Small chart:
```
Round 1: ████████ 3 findings
Round 2: ██ 1 finding
```

#### Cost Breakdown
```
Agent           Tokens    Time
gemini-pro        350    14.2s
claude-primary    520    18.1s
Total             870    32.5s
```

#### Agent Fallback History (if any)
```
Round 1: gemini-pro -> claude-primary (CircuitBreaker: rate_limit)
```
Show when CircuitBreaker forced an agent switch.

---

## Page 3: Agent Stats

### Layout
- Agent cards grid (one card per agent, 2x2 or 3 columns)
- Performance comparison chart
- Error breakdown

### Agent Card
```
┌─────────────────────────────────┐
│  [G] gemini-pro          HEALTHY│
│                                 │
│  Calls: 142     Success: 95.2%  │
│  Avg Latency: 1,247ms          │
│  Total Tokens: 48,500           │
│                                 │
│  ████████████████░░ 95.2%       │  (success rate bar)
│                                 │
│  Roles: Writer, Critic          │
│  CircuitBreaker: CLOSED         │
└─────────────────────────────────┘
```
- Agent avatar/icon with color (G=blue, C=purple, O=green, N=orange)
- Health status badge (green HEALTHY, yellow DEGRADED, red QUARANTINED)
- CircuitBreaker state indicator

### Performance Comparison
Horizontal bar chart comparing agents:
- Avg latency (ms)
- Success rate (%)
- Token efficiency (tokens per successful call)

### Error Breakdown
Table or chart showing error types per agent:
| Agent | timeout | rate_limit | parse_error | other |
|-------|---------|------------|-------------|-------|
| gemini-pro | 3 | 7 | 0 | 1 |
| claude-primary | 1 | 0 | 2 | 0 |

---

## Page 4: Live Monitor

### Layout
- Full-width live view of currently running session
- Auto-scrolls as new rounds appear
- SSE-powered (rounds appear in real-time without page refresh)

### Live Session View
```
┌──────────────────────────────────────────────────────┐
│  LIVE  Session #15: "JWT 갱신 로직 추가"    Running...│
│  Round 2/5  |  Elapsed: 45.2s                        │
├──────────────────────────────────────────────────────┤
│                                                      │
│  Round 1  [FAIL]                          completed  │
│  ├─ Writer: gemini-pro (14.2s)                       │
│  ├─ Nitpicker: PASS                                  │
│  ├─ Critic: claude-primary (18.1s)                   │
│  └─ Consensus: FAIL — blocker 1건                    │
│                                                      │
│  Round 2  [IN PROGRESS]               ● ● ● (dots)  │
│  ├─ Writer: claude-primary...          ◐ thinking    │
│  │                                                   │
│                                                      │
└──────────────────────────────────────────────────────┘
```

- Completed rounds: collapsed by default, expandable
- Current round: expanded, with animated spinner for the active step
- Steps within a round appear one by one as they complete (Writer -> Nitpicker -> Critic -> Consensus)
- Each step has a loading state: spinner + "thinking..." text

### Queue (if multiple sessions)
Small sidebar showing queued/running sessions:
```
Running: Session #15 — Round 2/5
Queued:  Session #16 — Waiting...
```

---

## Page 5: Diff Viewer

### Layout
- Accessed from Session Detail page when a file was merged
- Side-by-side diff view (before / after)
- Syntax highlighted

### Content
```
┌──────────────── Before (.bak) ─────────┬──── After (merged) ──────────────┐
│ # TODO: implement greeting             │ from __future__ import annotations│
│ def greet(name): pass                  │                                   │
│                                        │ def greet(name: str) -> str:      │
│                                        │     """인사말을 반환합니다."""       │
│                                        │     if not isinstance(name, str): │
│                                        │         raise TypeError(...)      │
│                                        │     return f"안녕하세요, {name}님!"│
└────────────────────────────────────────┴───────────────────────────────────┘
```
- Red highlight for removed lines (left side)
- Green highlight for added lines (right side)
- Line numbers on both sides
- "Rollback" button to restore from .bak

---

## Design Tokens

### Colors
- Background: #0d1117 (GitHub dark)
- Card background: #161b22
- Border: #30363d
- Text primary: #e6edf3
- Text secondary: #8b949e
- Accent blue: #58a6ff
- Success green: #3fb950
- Warning yellow: #d29922
- Error red: #f85149
- Purple (Claude): #a371f7
- Blue (Gemini): #58a6ff
- Green (Ollama): #3fb950
- Orange (Nitpicker): #d29922

### Typography
- Headings: Inter or system sans-serif
- Body: Inter or system sans-serif  
- Code: JetBrains Mono or Fira Code (monospace)

### Responsive
- Desktop primary (1280px+)
- Tablet usable (768px+)
- Mobile: session list only, detail pages simplified

---

## Navigation
- Left sidebar (collapsible):
  - Dashboard (Session List)
  - Live Monitor  
  - Agent Stats
  - Settings (future)
- Sidebar icons + text, collapsible to icons-only on narrow screens

---

## Sample Data (use these for realistic mockups)

### Sessions
```json
[
  {"id": 15, "status": "running", "verdict": null, "task": "JWT 갱신 로직 추가", "target": "src/auth.py", "rounds": 2, "max_rounds": 5, "elapsed": "45.2s"},
  {"id": 14, "status": "completed", "verdict": "pass", "task": "greet 함수 구현", "target": "src/__init__.py", "rounds": 1, "max_rounds": 5, "elapsed": "32.5s"},
  {"id": 13, "status": "completed", "verdict": "conditional", "task": "에러 핸들링 추가", "target": "src/api.py", "rounds": 3, "max_rounds": 5, "elapsed": "89.1s"},
  {"id": 12, "status": "failed", "verdict": "fail", "task": "DB 마이그레이션", "target": "src/db.py", "rounds": 5, "max_rounds": 5, "elapsed": "245.3s"},
  {"id": 11, "status": "failed", "verdict": "timeout", "task": "성능 최적화", "target": "src/engine.py", "rounds": 5, "max_rounds": 5, "elapsed": "300.0s"}
]
```

### Agents
```json
[
  {"id": "gemini-pro", "type": "gemini_api", "calls": 142, "success_rate": 95.2, "avg_latency": 1247, "tokens": 48500, "health": "healthy", "circuit": "closed"},
  {"id": "claude-primary", "type": "claude_code_cli", "calls": 98, "success_rate": 89.5, "avg_latency": 8420, "tokens": 0, "health": "healthy", "circuit": "closed"},
  {"id": "ollama-local", "type": "ollama", "calls": 45, "success_rate": 100, "avg_latency": 340, "tokens": 4200, "health": "healthy", "circuit": "closed"},
  {"id": "nitpicker-prefilter", "type": "nitpicker", "calls": 130, "success_rate": 100, "avg_latency": 50, "tokens": 0, "health": "healthy", "circuit": "closed"}
]
```
