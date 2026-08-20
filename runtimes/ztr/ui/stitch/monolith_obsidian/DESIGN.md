# Design System Specification

## 1. Overview & Creative North Star: "The Obsidian Orchestrator"
This design system is built for the high-stakes environment of multi-agent development. The Creative North Star is **The Obsidian Orchestrator**. 

In an era of cluttered, "boxed-in" developer tools, this system moves in the opposite direction: **Atmospheric Precision**. We achieve "Clean, High Density" not by adding lines and borders, but by using tonal layering, intentional asymmetry, and "Editorial Logic." The UI should feel like a high-end IDE crossed with a premium technical journal—authoritative, deep, and surgically sharp.

We break the "template" look by treating the screen as a single dark canvas where data "floats" at different depths of focus, rather than being trapped in rigid containers.

---

### 2. Colors & Surface Logic
The palette is rooted in deep obsidians and cold slates, providing a low-fatigue environment for long-form debugging and architectural review.

#### Tonal Architecture
| Token | Hex | Role |
| :--- | :--- | :--- |
| `surface` | #10141a | The base canvas (Global background). |
| `surface-container-low` | #181c22 | Primary layout sections (Sidebars, secondary panels). |
| `surface-container` | #1c2026 | Standard interactive elements. |
| `surface-container-high`| #262a31 | Hover states and active selections. |
| `surface-container-highest`| #31353c | Tooltips and floating modals. |

#### The "No-Line" Rule
**Explicit Instruction:** Do not use 1px solid borders to define the edges of sections or cards. Instead:
- **Nesting:** Place a `surface-container-low` panel directly onto the `surface`. The shift in tone creates the boundary.
- **Elevation through Contrast:** Use `surface-container-highest` for elements that need to appear "on top" of others.

#### The "Glass & Gradient" Rule
To add soul to the "Obsidian" aesthetic, use subtle radial gradients on the primary background. A faint glow of `primary_container` (#58a6ff) at 5% opacity in the top-left corner of the viewport adds depth. Floating agent panels must use `backdrop-blur: 12px` with a semi-transparent `surface_variant` (#31353c) at 60% opacity.

---

### 3. Typography: Editorial Authority
We use a high-contrast scale to ensure that even at high densities, the hierarchy is undeniable.

*   **Headlines & Body:** Inter (Sans-Serif) — Tight tracking (-0.02em) for headings to feel "engineered."
*   **Data & Logic:** JetBrains Mono (Monospace) — Used for code, status logs, and timestamps.

| Level | Size | Token | Usage |
| :--- | :--- | :--- | :--- |
| **Display Small** | 2.25rem | `display-sm` | Hero agent status or major session titles. |
| **Title Medium** | 1.125rem | `title-md` | Card headers and panel titles. |
| **Body Medium** | 0.875rem | `body-md` | Standard agent communication and descriptions. |
| **Label Small** | 0.6875rem | `label-sm` | Metadata, micro-stamps, and uppercase sub-headers. |

---

### 4. Elevation & Depth
Depth is a functional tool for focus, not just a visual flourish.

*   **Tonal Layering:** Avoid drop shadows for standard UI elements. A card should be distinguished from its background by moving from `surface` to `surface-container-low`.
*   **Ambient Shadows:** For floating dialogs, use a "Tinted Shadow."
    *   *Shadow Style:* `0px 20px 40px rgba(0, 0, 0, 0.4), 0px 0px 10px rgba(162, 201, 255, 0.05)`.
*   **The Ghost Border:** If a boundary is required for accessibility in dense data grids, use `outline-variant` (#414752) at **20% opacity**. It should be felt, not seen.

---

### 5. Components

#### Agent Identity & Status
Agents are the core of this system. They are identified by "Signature Accents":
- **Gemini:** Blue (`#58a6ff`)
- **Claude:** Purple (`#a371f7`)
- **Ollama:** Green (`#3fb950`)
- **Nitpicker:** Orange (`#d29922`)

**Agent Avatars:** Never just a circle. Use a soft-squircle (radius: `lg` 0.5rem) with a 2px inner "Ghost Border" of the agent’s specific accent color.

**Pulsing Live Indicators:**
Used when an agent is "Thinking." Use a 4px dot of the agent's accent color with a CSS pulse animation (`scale: 1.5; opacity: 0;`).

#### Buttons & Inputs
- **Primary Button:** Solid `primary_container` (#58a6ff). Text color `on_primary_fixed` (#001c38). No border.
- **Secondary Button:** `surface-container-high` background. Text `primary` (#a2c9ff).
- **Ghost Inputs:** Text inputs should have no background until focused. On focus, transition to `surface-container-low` with a `primary` 1px bottom-border only.

#### Code Blocks & Data Lists
- **Forbid Dividers:** Do not use horizontal lines between log entries. Use 8px of vertical whitespace or an alternating `surface-container-lowest` background for every second row.
- **Code Blocks:** Use `surface-container-lowest` (#0a0e14). Font: JetBrains Mono at `body-sm`. 

#### Status Badges
- **Compactness:** Badges should use `label-sm` typography. 
- **Success:** Background: `on_tertiary_container` (#004411), Text: `tertiary` (#67df70).

---

### 6. Do’s and Don'ts

#### Do
- **Do** use "Negative Space" as a separator. High density doesn't mean crowded; it means focused.
- **Do** use typography to differentiate "System Output" (Monospace) from "Agent Chat" (Sans-Serif).
- **Do** use asymmetric layouts. A narrow sidebar on the left and a wide chat area on the right, with an overlapping "Active Agent" floating panel.

#### Don't
- **Don't** use 100% white (#FFFFFF). It creates "halation" against the dark background. Use `on_background` (#dfe2eb).
- **Don't** use sharp 90-degree corners. Even for "Professional" tools, use the `DEFAULT` (0.25rem) radius to soften the digital edge.
- **Don't** use standard "Drop Shadows" on cards. Stick to tonal shifts to maintain the "Obsidian" feel.

---

### 7. Spacing Scale
The system uses a 4px baseline grid.
- **Tight (4px/8px):** For related metadata and label groupings.
- **Standard (16px/24px):** For content padding within cards.
- **Sectional (48px+):** To separate the main orchestration area from secondary toolbars. Use this instead of lines.