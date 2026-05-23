Be concise and action-oriented. Default to doing the work directly instead of over-explaining. Do not build, package, or release anything unless I explicitly ask. Before asking where output should go, search this repository for the previous successful build directory and reuse it if possible. Use WORKFLOW.md for repeatable local workflow details when needed. Use RELEASES.md for build, packaging, and release details when needed.

### Tool & Reading Discipline
- PREFER NATIVE TOOLS: Use `Grep` instead of bash `grep`/`rg`. Use `Glob` instead of bash `find`. Use `Read` instead of bash `cat`/`head`/`tail`.
- FILE READING STRATEGY:
  1. Use `LS`, `Glob`, and `Grep` first to find specific targets.
  2. Read only the smallest set of files necessary.
  3. Do not re-read files you already understand unless they changed or a prior assumption failed.
- RTK OPTIMIZATION: Ensure RTK intercepts terminal commands. When reading logs or test outputs, rely on compressed RTK output and do not dump full output into context.

### Token Discipline
- Search first, read second.
- Never reread unchanged files unless they were modified or a prior assumption failed.
- Do not rewrite handoff files from scratch when updating an existing pass; patch only changed sections.
- In handoffs, report evidence paths instead of copying evidence contents.
- In handoffs, summarize verification in one line per check; include raw output only on failure.
- Do not include full command inventories unless explicitly requested.
- Do not restate prior-pass history if it already exists in a prior handoff; reference the prior handoff path instead.
- For ticket/status-only passes, describe only field-level diffs and guarded-directory checks.
- After Codex approval and a completed pass, prefer a fresh session or compacted context before starting the next ticket.

### Repo Boundary Rules
- Treat this repository as the only source of truth unless I explicitly tell you to inspect another repo.
- Do NOT search sibling projects in `Projects/` for patterns, scripts, packaging logic, or build workflows.
- Only use files that exist inside the current repository to determine build, packaging, release, and signing steps.
- For build/release tasks, verify commands and packaging paths from this repo's files first, then report the exact source file used.
- If I mention another project by name, ask before reusing its workflow here.

### Windows Artifact Rules
- When I ask for a Windows build (`.exe`, `Portable.exe`, or `Setup.exe`), first check whether this repo itself already has packaging scripts, specs, or release conventions.
- If yes, use only those repo-local instructions.
- If no, stop and tell me the current repo lacks a defined Windows packaging path. Do not infer processes from other Cove repos or invent a new one.
