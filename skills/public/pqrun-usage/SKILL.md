---
name: pqrun-usage
description: "Use this skill when using pqrun in applications: setup, FastAPI lifespan integration, worker/reaper runtime behavior, shutdown tuning, retry/backoff/stale recovery, and operational troubleshooting. Focus on usage guidance only and route to the right user-facing docs."
---

# pqrun Usage

## Role

Use this file as a usage router for application integration and operation.

## Request Routing

Use this routing order and read only the minimum file set.

### Core docs (always prioritize)

- `references/pqrun-docs/README.md`: package overview, integration patterns, operational notes
- `references/pqrun-docs/user/quickstart.md`: usage flow and examples
- `references/pqrun-docs/user/configuration.md`: env vars and runtime tuning

If repository-local docs exist, you may cross-check with:
- `README.md`
- `docs/user/*.md`

### Topic groups

- Onboarding/setup/examples:
  - Start with `references/pqrun-docs/README.md`
  - Then read `references/pqrun-docs/user/quickstart.md`
- Runtime/configuration/tuning:
  - Start with `references/pqrun-docs/user/configuration.md`
  - Cross-check behavior examples in `references/pqrun-docs/user/quickstart.md`
- FastAPI/lifespan/worker operation:
  - Start with `references/pqrun-docs/user/quickstart.md`
  - Use `references/pqrun-docs/README.md` for deployment pattern context
- Troubleshooting symptoms (pickup/reaper/shutdown/retry):
  - Start with `references/pqrun-docs/user/configuration.md`
  - Then read relevant sections in `references/pqrun-docs/user/quickstart.md`

### Secondary references

- For edge behavior or internals context, use `references/pqrun-docs/developer/*.md` as secondary references.

## Docs Tree

This section is maintained by package maintainers to help navigation.

<!-- DOCS_TREE_START -->
```text
pqrun-docs/
├── assets/ ...
├── developer/
│   ├── architecture.md
│   ├── decisions.md
│   ├── design.md
│   ├── index.md
│   └── release-notes.md
├── user/
│   ├── configuration.md
│   ├── index.md
│   └── quickstart.md
└── index.md
```
<!-- DOCS_TREE_END -->

## Execution Rules

- Keep guidance usage-focused (configure/integrate/operate pqrun in apps).
- Read user docs first and avoid duplicating long explanations; point to the relevant doc paths.
- Explain the primary integration pattern first, then mention valid alternatives when relevant.
