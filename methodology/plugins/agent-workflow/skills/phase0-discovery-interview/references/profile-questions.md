# Profile Questions

Choose one or more profiles. Use minimum questions first; deep questions only when risk appears.

## AI / LLM

- Which provider/runtime is used, and can it fail silently?
- Is mock mode explicit, or can it become accidental fallback?
- Is the LLM output fixed into a structured spec before execution?
- Where are semantic failures detected?
- Does artifact success prove requirement success?
- What is the baseline prompt/tool flow, and what does this add beyond it?

## Web / App

- What is the primary workflow, not the landing-page story?
- What state is URL-owned, server-owned, client-owned, or persisted?
- What user action needs confirmation, undo, or audit?
- What loading/error/empty states are required?

## Automation / Script

- What is the idempotency rule?
- What files, accounts, or external systems can be modified?
- What is the dry-run or preview path?
- How are logs and artifacts captured?
- What external command/tool contract must be verified before PASS?

## C++ / Equipment / Runtime

- Where is calibration/config/state stored?
- What is persistent source of truth vs runtime cache?
- What tolerance or byte-level comparison defines PASS?
- Is live hardware validation required, or only deterministic simulation?

## Legacy Migration

- Which legacy behavior must remain compatible?
- Which distributed state or hidden dependency is the root problem?
- What is the rollback plan?
- What before/after evidence proves improvement?

## Product / MVP

- Is this technical MVP, product golden path, or commercial MVP?
- What is explicitly not claimed?
- Are legal, security, account, billing, deployment, and operations in scope?
- What single scenario is the first golden path?

## Document / Evaluation

- Who is the audience?
- Which claims need source verification?
- Which internal terms should be translated into external language?
- What wording would overclaim beyond evidence?

## Wrapper / Adapter / Skill Composition

- What is the wrapped baseline tool/skill/API?
- What concrete failure happened when using the baseline directly?
- What does this wrapper add: workflow, validation, UX, policy, evidence capture, or real capability?
- Which part of the core value is delegated to the wrapped tool?
- Is the wrapped tool's input/output contract verified from its docs or a PoC?
- If the wrapped tool cannot provide the core capability, will this project switch backend or downgrade the value claim?
- What silent success is possible: output file exists, API returns OK, or artifact opens, but requirement is unmet?
