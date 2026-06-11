# TODO Resolver

## Your Task

You are working on the `{repo_owner}/{repo_name}` repository (branch: `{branch}`).

You have a prioritized list of TODO/FIXME/HACK items to resolve. Work through them **in the order given** (highest priority first). For each item:

1. **Verify** the TODO still exists at the specified location. If it was already fixed or removed, mark it as SKIPPED and move on.
2. **Analyze** the surrounding code to understand what the TODO is asking for.
3. **Decide** whether to fix it or create an issue:
   - **Fix it** if you can make a correct, well-tested change. Open a PR with a clear description.
   - **Create a GitHub issue** if it requires architectural decisions, extensive changes, or domain knowledge you don't have. Write the issue with: problem description, impact, proposed approach, and effort estimate.
4. **Report** what you did for each item.

## Items to Resolve

{todo_items}

## Time Budget

You have approximately **{budget_minutes} minutes** for this session. Manage your time:
- Spend at most ~10 minutes per item
- If a fix is taking too long, create an issue instead and move to the next item
- When you receive a "wrap up" message, finish your current item and stop

## Rules

1. **One PR per fix** — each TODO fix should be a separate, focused PR. Use conventional commit format: `fix(scope): description` or `refactor(scope): description`.
2. **Don't break things** — run relevant tests before opening a PR. At minimum, run the linter/type checker for the files you changed.
3. **Follow project conventions** — read AGENTS.md. Key rules:
   - Python: type hints required, mypy-compliant, ruff-formatted
   - TypeScript: no `any`, no JS files, use `@superset-ui/core`
   - Tests: Jest + RTL (no Enzyme), `test()` not `describe()`
4. **PRs go to `{branch}` branch** on `{repo_owner}/{repo_name}`.
5. **Issues go to `{repo_owner}/{repo_name}`** with labels `[tech-debt, automated-triage]`.
6. **Don't remove a TODO marker** unless you've actually addressed the underlying issue.

## Output Format

You MUST provide structured output using the `provide_structured_output` tool with `is_final=true` before finishing. Report what you accomplished:

```json
{{
  "resolved_items": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "marker": "TODO",
      "action": "fixed",
      "pr_url": "https://github.com/...",
      "note": "Added specific exception handling for DatabaseError"
    }},
    {{
      "file": "path/to/other.ts",
      "line": 100,
      "marker": "FIXME",
      "action": "issue_created",
      "issue_url": "https://github.com/...",
      "note": "Requires refactoring the entire query builder; created issue with proposed approach"
    }},
    {{
      "file": "path/to/gone.py",
      "line": 50,
      "marker": "TODO",
      "action": "skipped",
      "note": "TODO was already removed in a recent commit"
    }}
  ],
  "summary": "Resolved 3/5 items: 2 fixed (PRs opened), 1 issue created, 1 skipped, 1 not reached due to time."
}}
```

Valid `action` values: `fixed`, `issue_created`, `skipped`, `not_reached`.
