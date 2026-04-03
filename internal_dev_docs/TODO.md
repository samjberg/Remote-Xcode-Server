# TODO

## Project Context Disambiguation (Deferred)

### Problem
- Current known-repo fallback in `projects_context_manager._get_project_by_name` matches by repo folder name only.
- If multiple repos share the same name, server may bind the wrong project.

### Goal
- Keep network payload minimal and avoid sending absolute paths.
- Make server-side project resolution deterministic.

### Plan
1. Client request payload/query should include:
- `project_name` (already present)
- `project_id` (hash-based id, already derivable)
- optional `git_remote_url` (or canonical repo slug)

2. Server resolution order:
- resolve by tracked `project_id` first
- fallback to name matches in known repos
- if multiple name matches, disambiguate by remote URL
- if still ambiguous, return explicit error with candidate choices

3. Error handling:
- Add a dedicated ambiguity error type/message (e.g. `project_ambiguous`)
- Include candidate metadata (safe fields only) for debugging

4. Known repos schema extension:
- Store per-repo metadata (at least `path`, `name`, optional `remote_url`)
- Normalize comparisons to reduce false mismatches

### Constraints
- Do **not** send absolute local paths from client to server.
- Preserve current first-run known-repo scan flow.

### Nice-to-have
- Add a CLI flag to force re-scan (`--scan-for-repos`) and optionally accept repeated scan roots.
