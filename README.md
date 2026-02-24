# Remote Xcode Server

Run `xcodebuild` from a Windows development machine by forwarding your local Git diff to a Mac, applying it there, and streaming the build output back in near real time.

This project exists for a very practical workflow:

- Active development happens on a Windows desktop (faster machine).
- Xcode builds must run on a Mac.
- The client makes it feel like `xcodebuild` is running locally by streaming logs back to the client terminal.

It is especially intended for AI-assisted iOS development workflows, where an agent running on Windows needs access to Xcode build output without directly operating on the Mac.

## What It Does (Current Behavior)

At a high level:

1. The client runs inside a Git repo for an iOS project.
2. It generates a patch using `git diff HEAD`.
3. It sends that patch (`gitdiff.diff`) to the server running on a Mac.
4. The server saves the patch and runs `git apply` in the project directory.
5. The server starts `xcodebuild`.
6. The client polls the server every second for newly appended build log text.
7. The client prints only the new text, so the terminal behaves like a live local build.

## Why This Exists

The goal is to let a Windows-based workflow (including AI agents) trigger and monitor Xcode builds on a Mac without manually switching machines for every build/test iteration.

Longer term, the plan is to integrate this into editor/agent tooling (for example via VS Code or Antigravity extensions) so an agent can invoke builds directly and receive streamed output in its own terminal session.

## Project Files

- `mcp_server.py`: Flask server that receives a diff, applies it, starts `xcodebuild`, and serves build log progress.
- `mcp_client.py`: Client that creates/sends the Git diff and polls for build output.
- `mcp_utils.py`: Shared helpers (path normalization, project root/app name detection, port, etc.).
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
- The server assumes it is running in the iOS project directory (where the `.xcodeproj` lives).

### 2. Configure the client IP

In `mcp_client.py`, the server IP is currently hardcoded:

- `server_ip = '192.168.7.189'` (example from current code)

Update this to your Mac’s LAN IP address.

### 3. Run the client from your project repo

On the client machine, run:

```bash
python mcp_client.py
```

Current expectation:

- Run it from the project root (the code is intended to work from subdirectories too, but this is not fully reliable yet).
- The repo should have the same base history on both machines so `git apply` works cleanly.

## What Gets Created

The scripts create helper directories/files automatically:

- Client:
  - `diffs/`
  - `diffs/gitdiff.diff`
- Server:
  - `uploads/`
  - `uploads/gitdiff.diff`
  - `uploads/buildlog.txt`
  - `uploads/projectinfo.txt`

The code also attempts to add `uploads/` and `diffs/` to `.gitignore`.

## Important Limitations (Current State)

This project works fundamentally, but it is still early and buggy. Known limitations include:

- Server IP is hardcoded in `mcp_client.py`.
- No CLI arguments/config file yet.
- Build command is currently fixed and simple (`xcodebuild ... build` with a derived scheme name).
- Polling is HTTP-based (1s interval), not true streaming/websocket.
- Error handling is minimal.
- Patch application assumes compatible repo state and can fail if histories diverge.
- Client path/root detection is a work in progress.
- Security is minimal (no auth, no TLS, no request validation beyond basic file handling).
- Single-user / ad hoc workflow assumptions throughout.

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
