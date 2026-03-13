# Remote Xcode Server

Run `xcodebuild` from a Windows development machine by forwarding your local Git diff to a Mac, applying it there, and streaming the build output back in near real time.

This project exists for a very practical workflow:

- Active development happens on a Windows desktop (faster machine).
- Xcode builds must run on a Mac.
- The client makes it feel like `xcodebuild` is running locally by streaming logs back to the client terminal.

It is especially intended for AI-assisted iOS development workflows, where an agent running on Windows needs access to Xcode build output without directly operating on the Mac.

## What It Does (Current Behavior)

At a high level:

1. The client runs inside a Git repo for an Xcode project.
2. It generates a patch using `git diff HEAD`.
3. It sends that patch (`gitdiff.diff`) to the server running on a Mac.
4. The server saves the patch and runs `git apply` in the project directory.
5. The server starts `xcodebuild` with `stdout` piped (and `stderr` merged into `stdout`).
6. The client opens a TCP socket connection to the Mac.
7. The server forwards subprocess output chunks over that socket as they arrive.
8. The client prints the incoming bytes, so the terminal behaves like a live local build.

## Why This Exists

The goal is to let a Windows-based workflow (including AI agents) trigger and monitor Xcode builds on a Mac without manually switching machines for every build/test iteration.

Longer term, the plan is to integrate this into editor/agent tooling (for example via VS Code or Antigravity extensions) so an agent can invoke builds directly and receive streamed output in its own terminal session.

## Project Files

- `mcp_server.py`: Flask server that receives a diff, applies it, starts `xcodebuild`, and streams build output over a TCP socket.
- `mcp_client.py`: Client that creates/sends the Git diff, then connects to the server socket and prints streamed build output.
- `mcp_utils.py`: Shared helpers (path normalization, project root/app name detection, port, etc.).
- `mcp_sockets.py`: Scratch/experimental socket work (if present locally; not part of the main flow yet).
- `reset.py`: Utility script (currently not part of the main flow).
- `parseargs.py`: Early argument parsing helper (not currently integrated into the main client flow).

## Requirements

### Server (Mac)

- macOS
- Python 3
- Xcode / `xcodebuild`
- Git
- Python package: `flask`

### Client (Windows or any machine)

- Python 3
- Git
- Python package: `requests`

## Installation

Install Python dependencies on each machine as needed.

Server (Mac):

```bash
pip install flask
```

Client (Windows/other):

```bash
pip install requests
```

## How To Run (Current)

### 1. Prepare the Mac (server)

- Put this project in the root of the iOS project repo on the Mac (or at least run the server from the repo root).
- Start the server:

```bash
python mcp_server.py
```

Notes:

- The server listens on `0.0.0.0` and port `8751` (from `mcp_utils.py`).
- The current socket log stream also uses a separate TCP port (`50271` in the current code).
- The server assumes it is running in the iOS project directory (where the `.xcodeproj` lives).

### 2. Pairing (required before secured requests)

Before the client can call secured endpoints, pairing must be enabled on the server:

1. Trigger pairing mode (60 seconds):

```bash
curl -k https://<server-ip>:8751/enable_pairing
```

2. Run the client while pairing is active. It will discover the server over UDP, then persist:
- pinned server certificate
- shared HMAC secret
- updated `serverinfo.txt` metadata

If pairing is not active and local credentials are missing, client startup will fail and ask you to pair.

### 3. Run the client from your project repo

On the client machine, run one of these:

```bash
# Default (same as `build`)
python mcp_client.py

# Build flow (sync changes + start remote build + stream logs)
python mcp_client.py build

# Sync-only flow (send changes to server, no build)
python mcp_client.py sendchanges
```

Current expectation:

- Run it from the project root (the code is intended to work from subdirectories too, but this is not fully reliable yet).
- The repo should have the same base history on both machines so `git apply` works cleanly.

### Client Commands (Temporary / Minimal)

Current `mcp_client.py` CLI behavior is intentionally minimal and temporary:

- `build`: the normal workflow. This is the default if no command is provided.
- `sendchanges`: syncs local changes to the server without starting a build (convenience for testing/syncing only).

`sendchanges` is intended to package and send the current diff plus changed binary files, so you can sync work-in-progress changes without making a commit/push or manually copying a patch.

Note: the current implementation checks commands using substring matching (e.g. `'build' in arg`), which is temporary and may change.

## What Gets Created

The scripts create helper directories/files automatically:

- Client:
  - `.remote-xcode-server/serverinfo.txt`
  - `.remote-xcode-server/certs/server_cert.pem`
  - `.remote-xcode-server/credentials/hmac_secret.txt`
  - `.remote-xcode-server/gitdiff.diff`
- Server:
  - `.remote-xcode-server/projectinfo.txt`
  - `.remote-xcode-server/.secrets/tls/key.pem`
  - `.remote-xcode-server/.secrets/tls/cert.pem`
  - `.remote-xcode-server/.secrets/auth/hmac_secret.txt`

Notes:

- `uploads/buildlog.txt` may still be created by older/legacy code paths, but the current primary log path is direct socket streaming from the subprocess pipe.

The code also attempts to add `uploads/` and `diffs/` to `.gitignore`.

## Important Limitations (Current State)

This project works fundamentally, but it is still early and buggy. Known limitations include:

- Server IP is hardcoded in `mcp_client.py`.
- CLI arguments are currently very minimal/temporary (`build`, `sendchanges`, defaulting to `build`), and a proper argument/config system is still needed.
- Build command is currently fixed and simple (`xcodebuild ... build` with a derived scheme name).
- Log streaming is now socket-based (raw TCP), but the code still contains older HTTP polling endpoints/logic.
- Error handling is minimal.
- Patch application assumes compatible repo state and can fail if histories diverge.
- Client path/root detection is a work in progress.
- Self-signed TLS is used; certificate trust is TOFU via pairing.
- Pairing mode trigger (`/enable_pairing`) is intentionally unauthenticated in this version.
- Single-user / ad hoc workflow assumptions throughout.
- Socket stream protocol is intentionally simple after authenticated handshake.

## Security and Pairing Notes

- All operational HTTP routes require HMAC auth headers and run over HTTPS.
- Raw TCP channels (build log stream + file transfer sockets) are TLS-wrapped and require an HMAC handshake before payload traffic.
- Pairing discovery response includes:
  - `certificate:<base64-pem>`
  - `secret_key:<shared-secret>`
  - `pairing_expires_unix:<epoch-seconds>`
- Existing legacy `serverinfo.txt` (IP/ports only) is migrated on next successful pairing.

### Re-pair / Reset

If credentials become invalid (clock skew, cert mismatch, signature errors), delete:

- `.remote-xcode-server/serverinfo.txt`
- `.remote-xcode-server/certs/server_cert.pem`
- `.remote-xcode-server/credentials/hmac_secret.txt`

Then re-enable pairing on the server and rerun the client.

## Current Streaming Approach (Server)

Current log transport is:

- `xcodebuild` launched via `subprocess.Popen(...)`
- `stdout=subprocess.PIPE`
- `stderr=subprocess.STDOUT` (merged)
- server reads chunks from `proc.stdout`
- server sends those chunks to the client over a TCP socket

This means the client is receiving a live byte stream, not repeatedly polling for file growth.

## Current Build Command (Server)

The server currently runs:

```bash
xcodebuild -scheme "<project_name>" -destination 'generic/platform=iOS Simulator' build
```

`<project_name>` is inferred from the `.xcodeproj` name in the server’s current directory.

## Intended Future Direction

- Proper CLI arguments (server address, scheme, destination, configuration, workspace/project selection, etc.)
- More reliable project root discovery
- Better diff/patch sync behavior and reset/recovery flows
- Robust job lifecycle/state management
- Authentication and network hardening
- Editor/agent integration (VS Code / Antigravity extensions)
- Native agent-triggered builds with streamed output directly into agent terminals

## Development Notes

This repository is currently optimized for proving the workflow, not for production deployment. The core idea is the important part:

- keep coding on the fast machine,
- run Xcode builds on the Mac,
- surface the output where the developer/agent already is.

## License

No license file is currently included. Add one before publishing or accepting external contributions.
But feel free to use this for personal use if you want to.
