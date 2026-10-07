# AGENT RULES — binding rules for all agent workers

> Owned by Hermes (Lead Orchestrator). Agents must not edit this file.
> Propose changes in your final report instead.

## 1. Workspace boundaries

- One task = one worktree = one branch (`task/<id>-<slug>`). Provided by Hermes; do not create your own.
- Only create/modify files listed in your task `scope`. Never touch `do_not_touch`, other tasks' files,
  `.orchestrator/**`, or root `AGENTS.md` (unless the task explicitly says otherwise).
- Do not change public interfaces without approval — propose them in your report.

## 2. Git

- Agents write files only. Do NOT commit, merge, rebase, switch branches, amend, or push.
  Hermes reviews, then commits/merges.
- Never commit third-party checkouts, generated files, or large binary artifacts.

## 3. DONE is not "agent says done"

- DONE requires ALL gates to pass: scope/diff review + acceptance criteria + tests +
  lint + typecheck + build (when applicable).

## 4. Evidence

- Final report must include: files changed, exact commands executed, real outputs (logs),
  deviations, uncertainties.
- Unknowns must be marked as unknowns. Fabricated results disqualify the task.

## 5. Escalation

- Same task failing twice → Hermes upgrades the agent (OpenCode/Cline → Devin).
- If blocked by missing permissions/resources, state it clearly in the report; do not work around
  outside scope.

## 6. Language & safety

- Repo artifacts (code, docs, commit messages): English. Reports to the user: Vietnamese.
- Do not install global tooling; keep dev tooling local. Ask before touching system config.
