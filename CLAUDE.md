# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Clove is a FastAPI-based reverse proxy that exposes a standard Claude API (`/v1/messages`) backed by either:
- **OAuth mode** — talks directly to `api.anthropic.com` using OAuth tokens (the same flow Claude Code uses).
- **Web reverse-proxy mode** — drives `claude.ai` via session cookies, translating the streaming web responses back into Anthropic API events.

The proxy is designed to make web mode look as close to the real API as possible (tool calling, stop sequences, token counting, non-streaming responses, etc.), and it auto-switches between modes based on account capabilities.

The `front/` directory is a git submodule (https://github.com/mirrorange/clove-front.git); the built React app is shipped as `app/static/` inside the wheel.

## Common Commands

The project uses `uv` for dependency management and a Makefile for build/run convenience.

```bash
# Run the dev server (http://localhost:5201, admin UI at /)
make run                       # equivalent to: python -m app.main

# Install in editable mode for development
make install-dev               # pip install -e .

# Tests
python -m unittest discover tests
python -m unittest tests.test_claude_request_models   # single module
python -m unittest tests.test_claude_request_models.MessagesAPIRequestToolParsingTests.test_accepts_custom_tool_payload_without_top_level_input_schema

# Build the Python wheel (also builds frontend submodule via pnpm)
make build                     # full build: frontend + wheel
make build-frontend            # only the React frontend → app/static
make build-wheel               # wheel only (assumes app/static already exists)

# Clean build artifacts
make clean
```

Frontend build requires Node.js and pnpm; `scripts/build_wheel.py` will install pnpm globally if missing. If you only touch backend code, use `make build-wheel` to skip the frontend step.

Optional dependency extras matter at install time:
- `clove-proxy[rnet]` — uses `rnet` for claude.ai HTTP (default for general use)
- `clove-proxy[curl]` — uses `curl_cffi` (does not work on Termux)
- `clove-proxy` (no extras) — OAuth-only mode; web reverse-proxy disabled

The lockfile is `uv.lock`; Docker builds with `uv sync --locked --extra rnet --extra curl`.

## Architecture

### Request flow

`POST /v1/messages` (in `app/api/routes/claude.py`) is the single entry point for inference. The handler is wrapped in a `tenacity` retry that re-runs the entire pipeline on retryable errors (see `app/utils/retry.py`).

A request becomes a `ClaudeAIContext` (`app/processors/claude_ai/context.py`) that carries the original FastAPI `Request`, the parsed `MessagesAPIRequest`, the upstream Claude session, the raw and parsed event streams, and the eventual response. This context is mutated as it passes through `ClaudeAIPipeline` (`app/processors/claude_ai/pipeline.py`).

### Processor pipeline

`app/processors/pipeline.py` defines a generic `ProcessingPipeline` that walks an ordered list of `BaseProcessor`s, each receiving and returning the context. Two metadata flags control flow:
- `context.metadata["skip_processors"]` — list of processor names to bypass
- `context.metadata["stop_pipeline"]` — set by a processor to short-circuit the rest

`ClaudeAIPipeline` registers the default order. Roughly: pre-processing → upstream dispatch → event parsing → post-processing → response shaping.

```
TestMessageProcessor          # SillyTavern test-message shortcut
ToolResultProcessor           # rewrite tool_result blocks for upstream
ClaudeAPIProcessor            # OAuth path → api.anthropic.com (sets stop_pipeline if it handles the request)
ClaudeWebProcessor            # cookie path → claude.ai (fallback)
EventParsingProcessor         # raw SSE → StreamingEvent
ModelInjectorProcessor        # rewrite model id / inject custom prompt
StopSequencesProcessor        # emulate stop_sequences for web mode
ToolCallEventProcessor        # convert claude.ai output into tool_use blocks
MessageCollectorProcessor     # accumulate full Message for non-streaming
TokenCounterProcessor         # estimate token counts via tiktoken
StreamingResponseProcessor    # build final SSE StreamingResponse
NonStreamingResponseProcessor # build final JSONResponse
```

When adding a new transform, prefer writing a new `BaseProcessor` and inserting it at the right pipeline position rather than modifying existing processors.

### Account & session management

`app/services/account.py` (`account_manager`, singleton) is the source of truth for upstream auth. Accounts are keyed by `organization_uuid` and may carry a Claude.ai cookie, an OAuth token, or both. Selection logic favors accounts that can serve a request via the API (OAuth) over web mode, and respects per-cookie session limits and quota state. Accounts are persisted to `${DATA_FOLDER}/accounts.json` (default `~/.clove/data/`) and reloaded on startup.

`app/services/session.py` (`session_manager`) tracks active `ClaudeWebSession` objects (`app/core/claude_session.py`) with idle timeout / cleanup. `app/services/oauth.py` handles the Claude OAuth dance (auto-completed via cookie when possible). `app/services/tool_call.py` keeps tool-call state across the round-trip in web mode (claude.ai requires holding the connection open while the client computes a tool result). `app/services/cache.py` provides the prompt-cache checkpoint store.

All four managers are started/stopped in `app/main.py`'s `lifespan`.

### Configuration

`app/core/config.py` defines a single `settings` instance via `pydantic-settings`. The custom source order (highest to lowest priority): JSON config in `${DATA_FOLDER}/config.json` → env vars → `.env` → defaults. Setting `NO_FILESYSTEM_MODE=true` disables the JSON layer and on-disk persistence entirely (everything stays in memory) — useful for HuggingFace Space and similar ephemeral deploys.

Most operational config (accounts, API keys, cookies) is normally managed through the admin UI at `/` rather than env vars. The temporary admin key printed at startup only exists until you set `ADMIN_API_KEYS` (or create one in the UI).

### Models & API surface

`app/models/claude.py` mirrors the Anthropic Messages API request/response schema. `app/models/internal.py` is the claude.ai web-API shape. `app/models/streaming.py` is the unified `StreamingEvent` representation used across processors.

`app/api/main.py` mounts:
- `/v1/*` — Claude-compatible inference (auth via `AuthDep`, `app/dependencies/auth.py`)
- `/api/admin/{accounts,settings,statistics}/*` — admin UI backend (auth via admin keys)
- `/` — static frontend served from `app/static/` (only present after a frontend build)

## Conventions

- Logging goes through `loguru`; logger configuration lives in `app/utils/logger.py` and runs in `lifespan`.
- Errors raised from `app/core/exceptions.py` (subclasses of `AppError`) are converted to API responses by `app_exception_handler` in `app/core/error_handler.py`. Use `is_retryable_error` (in `app/utils/retry.py`) to mark exceptions that should trigger the route-level retry.
- Tests use `unittest` (no pytest dependency); place them under `tests/` with `test_*.py` filenames. Hatch excludes `app/**/test_*.py` from the wheel, so don't colocate tests inside `app/`.
- Python `>=3.11` is required by `pyproject.toml`; CI/Docker uses 3.11, the repo's `.python-version` pins 3.13 for local dev.
