# CLAUDE.md — GitWorklog Agent

## Purpose
Local CLI AI agent for Git intelligence and evidence-based worklogs: status, code review,
commit assistance, worklogs/timesheets, summaries, standups, branch comparison, PR
descriptions and repository health. Core promise: **every claim is backed by repository
evidence; working hours are never fabricated.**

## Architecture
```
cli.py (Typer/Rich, I/O only)
  -> services/*   deterministic business logic (grouping, rendering, health, review ctx)
  -> agent.py     tool-calling loop (max iterations, history trimming)
       -> llm.py  the ONLY module that talks to the OpenAI-compatible API
       -> tools/  typed tools + registry exposed to the LLM
  -> safety.py    secret masking, destructive-op classification, approval gate
  -> tools/git.py GitRunner: the ONLY place that spawns `git` (allowlisted subcommands)
```
- `src/gitworklog/` package, `tests/` pytest suite, `.gitworklog/config.json` optional per-repo config.
- Services work without the LLM (`--no-llm`); the LLM only *rewrites* grounded evidence.

## Commands
```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # setup (Windows path)
.venv/Scripts/python -m pytest -q                               # tests
.venv/Scripts/ruff check src tests                              # lint
.venv/Scripts/ruff format src tests                             # format
.venv/Scripts/gitworklog --help                                 # run CLI
.venv/Scripts/gitworklog health --repo <path>                   # try against another repo
```
Tests marked `live` hit the real LLM and are skipped unless `NVIDIA_API_KEY` is set and
`GITWORKLOG_LIVE_TESTS=1` (`GITWORKLOG_LIVE_TESTS=1 .venv/Scripts/python -m pytest -m live`).
The full suite takes ~2-3 min on Windows (it spawns many `git` processes).

Manual end-to-end: `tests/helpers.py::build_sample_repo(path)` creates a dated rider-app repo;
run the CLI against it with `--repo`. `GITWORKLOG_ALLOW_PIPED_APPROVAL=1` lets you answer
approval prompts through piped stdin (otherwise non-TTY sessions always deny).

## Project-specific gotchas
- Windows: `Path.write_text`/`read_text` translate newlines. Use `read_bytes`/`write_bytes`
  when modifying user files (see `tools/filesystem.py::_planned_edit`), and keep sources LF.
- The target repo's `.env` is never loaded (it could redirect `GITWORKLOG_BASE_URL`); only the
  real environment, `~/.gitworklog/.env` and the tool checkout's `.env`.
- `gpt-oss` may leak template tokens into tool names (`name<|channel|>...`); `llm.py` strips them.
  Send assistant messages back via `AssistantMessage.to_message()` (drops reasoning fields).
- Porcelain v2 status uses `.` for "unchanged", not a space.
- Distinct Conventional Commit scopes are hard task boundaries in `services/grouping.py`.

## Development rules
- Inspect before modifying. Prefer the smallest change that solves the problem.
- Keep business logic out of `cli.py` and out of `llm.py`.
- Do not add dependencies without a clear reason (current: openai, python-dotenv, typer, rich).
- No agent frameworks (LangChain etc.); the loop in `agent.py` is intentionally small.
- Run `pytest` and `ruff check` after changes. Do not claim success without running them.
- Python >= 3.11, type hints everywhere, dataclasses for structured data, PEP 8 via Ruff (line length 100).
- New tools: add a typed function, validate inputs, return JSON-serialisable dicts, cap output
  size, mask secrets, register in `tools/__init__.py`, add a unit test.

## Safety rules (non-negotiable)
- Do not fabricate repository facts. Label FACT / INFERENCE / SUGGESTION in agent output.
- Never commit, push or run destructive Git operations (reset --hard, clean, checkout --,
  branch -D, rebase, merge, push, push --force) without explicit human approval through
  `safety.Approver`. The LLM can never approve on the user's behalf.
- Never expose arbitrary shell execution to the LLM. All git calls go through `GitRunner`.
- Never print or send secrets: all tool output passes `safety.mask_secrets`; `.env`/key files are never read.
- Git timestamps show activity, not hours worked. Only user-provided hours are reported as hours.
- Keep LLM context focused: respect the limits in `config.Limits`; never send the whole repo.

## Secrets
- Credentials come only from environment variables (`NVIDIA_API_KEY`), optionally loaded from `.env`.
- `.env` is git-ignored. Never commit it, never store secrets in `.gitworklog/config.json`.

## Git conventions (for this repo)
- Conventional Commits: `feat(scope): ...`, `fix(...)`, `refactor`, `test`, `docs`, `chore`.
- Small focused commits; never commit or push unless the user asks.
