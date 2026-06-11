# TODO/FIXME/HACK Scanner & Ranker

## Your Task

You are analyzing the `{repo_owner}/{repo_name}` repository (branch: `{branch}`) to triage TODO/FIXME/HACK/XXX markers in the codebase.

**Do NOT fix anything. Do NOT open PRs. Do NOT modify code.** Your only job is to read, judge, and rank.

## TODO Locations Found

The following markers were found by a preliminary grep. For each one, you must:
1. Read the surrounding code (at least 20-30 lines around the marker) to understand the context
2. Understand what module/function/class it belongs to and what the code does
3. Judge the **importance** of addressing this TODO
4. Judge the **complexity** of fixing it
5. Categorize the type of work needed
6. Write a brief reasoning for your ranking

{todo_list}

## Ranking Criteria

### Importance (how much does it matter?)
- **critical**: Security risk, data corruption potential, or blocks important functionality
- **high**: Causes bugs, poor error handling in critical paths, or significant tech debt
- **medium**: Code quality issue, missing feature, or moderate tech debt
- **low**: Cosmetic, minor cleanup, or nice-to-have improvement

### Complexity (how hard is the fix?)
- **trivial**: Can be fixed in a few lines with confidence (add a null check, remove dead code, etc.)
- **moderate**: Requires understanding the local code but is self-contained (refactor a function, add error handling)
- **complex**: Touches multiple files or requires understanding broader architecture
- **architectural**: Requires design decisions, new abstractions, or cross-cutting changes

### Category
- **bug_fix**: The TODO describes an actual bug or incorrect behavior
- **error_handling**: Missing or inadequate error/exception handling
- **performance**: Performance improvement needed
- **refactor**: Code structure improvement
- **cleanup**: Dead code removal, stale comments, formatting
- **feature**: Missing functionality
- **documentation**: Missing or outdated docs/comments
- **testing**: Missing or inadequate tests
- **security**: Security-related concern

## Output Format

You MUST provide structured output using the `provide_structured_output` tool with `is_final=true`. The output must match this schema:

```json
{{
  "todos": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "marker": "TODO",
      "text": "the original comment text",
      "importance": "high",
      "complexity": "moderate",
      "category": "error_handling",
      "reasoning": "This catch block silently swallows database connection errors in the query execution path, which could cause silent data loss.",
      "actionable": true
    }}
  ],
  "summary": "Scanned N TODOs. X critical, Y high, Z medium, W low. Key themes: ..."
}}
```

Sort the `todos` array by importance (critical first), then by complexity (trivial first within same importance).

Set `actionable` to `false` for items that:
- Depend on external/upstream changes
- Require product decisions beyond engineering
- Are intentional trade-offs documented as such
- Are so vague that the intent is unclear

## Important

- Be concise in reasoning (1-2 sentences max)
- Don't inflate importance — most TODOs are medium or low
- A TODO in a critical path (auth, query execution, data serialization) is more important than one in a UI helper
- Consider the Superset project's stated priorities from AGENTS.md (type safety, test modernization, security)
