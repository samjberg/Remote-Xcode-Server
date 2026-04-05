# Status Codes Reference

This document tracks internal status strings and related response fields used in the current codebase.

## 1) Build Job Status (`JOBS[job_id]["status"]`)

Defined/used in `mcp_server.py`.

- `pending`
  - Set when a job is created in `/start-build-job/<appname>`.
- `running`
  - Set when `run_xcodebuild(...)` starts execution.
- `done`
  - Set when `xcodebuild` exits with return code `0`.
- `error`
  - Set when:
    - argument validation fails (invalid xcodebuild args),
    - `xcodebuild` exits non-zero,
    - exception occurs in build execution flow.

Related field:
- `JOBS[job_id]["error"]`
  - `None` or an error message string for error cases.

Notes:
- `/checkprogress/<job_id>/<offset>` currently:
  - returns text `"Build already Complete"` when status is `done`,
  - returns text `"Job status returned error.  Error message: "` when status is `error`,
  - otherwise returns JSON with `status: "pending"` (even while actual job status may be `running`).

## 2) Reconcile Result Status (`reconcile_result["status"]`)

Defined in `mcp_client.py` constants:

- `ALIGNED`
- `RECONCILED`
- `NEEDS_ACTION`
- `BLOCKED_DIRTY_WORKTREE`
- `BLOCKED_MISSING_COMMIT_OBJECT`
- `BLOCKED_DIVERGED_HISTORY`
- `BLOCKED_DETACHED_HEAD`
- `BLOCKED_NO_ORIGIN`
- `ERROR`

Returned from `_reconcile_result(...)` with schema:
- `status`
- `authority_side` (`"none"`, `"client"`, or `"server"`)
- `target_branch`
- `target_commit`
- `actions_applied` (list of action labels)
- `message`

Operational meaning (high level):
- Terminal success:
  - `ALIGNED`, `RECONCILED`
- Action-required intermediate:
  - `NEEDS_ACTION` (used during decision and action application)
- Terminal blocked:
  - all `BLOCKED_*` values
- Terminal failure:
  - `ERROR`

## 3) File Transfer Session Statuses

Defined/used in `mcp_server.py`.

### 3.1 Inbound transfer (client -> server)
Session map: `TRANSFER_SESSIONS[transfer_id]`

Statuses seen:
- `initialized`
  - session created in `/sendfilessocket/init/<appname>`
- `awaiting_socket`
  - transfer thread waiting for file socket connect
- `receiving`
  - actively receiving file frames
- `received`
  - socket transfer completed successfully at protocol level
- `completed`
  - `/sendfilessocket/complete/<appname>` verification passed
- `error`
  - any protocol/validation/integrity/runtime error

### 3.2 Outbound transfer (server -> client)
Session map: `OUTBOUND_TRANSFER_SESSIONS[transfer_id]`

Statuses seen:
- `initialized`
  - session created in `/sendfilesfromserver/init/<appname>`
- `awaiting_socket`
  - sender thread waiting for file socket connect
- `sending`
  - actively sending file frames
- `sent`
  - socket transfer completed successfully at protocol level
- `completed`
  - `/sendfilesfromserver/complete/<appname>` verification passed
- `error`
  - any protocol/validation/integrity/runtime error

### 3.3 Active-transfer guard

`_has_active_file_transfer()` treats these as active:
- inbound: `initialized`, `awaiting_socket`, `receiving`
- outbound: `initialized`, `awaiting_socket`, `sending`

## 4) Route-Level Completion Flags (`ok`) and HTTP status

Used in transfer init/complete routes:
- `ok: true|false`
- HTTP typically:
  - `200` success
  - `400` request/validation/integrity failure
  - `404` unknown `transfer_id`
  - `409` transfer conflict (another transfer active)
  - `410` legacy route no longer supported (`/sendfilessocket/<appname>`)

`ok` is not a session status; it is per-response success for a specific request.



### 3.4 project_bundle structure
- `id`: str, generated via mcp_utils.generate_project_id
- `project_name`: str, the name of the project, usually the same as the directory name of the project's root directory
- `project_root_path`: str, absolute path to the project root directory
- `tracked_timestamp`: int, unix timestamp of when the project was first tracked
- `last_command_timestamp`: int, unix timestamp of when the last command was executed with this project active
- `known_clients`: list[str], a list of IP addresses of known clients that have run commands on this project
