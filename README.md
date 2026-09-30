# GitWorklog Agent

A local command-line AI assistant for Git. It covers code review, commit help, repository
health, and the question every timesheet needs answered:

> "What did I actually work on during this period?"

Every answer is built from your repository's own history. Commits, changed files and diffs are
grouped into logical tasks, each task keeps its evidence, and the tool never makes up working
hours.

## Features

| Area | Command | What it does |
|---|---|---|
| Git status | `status` | Branch, upstream ahead/behind, staged/unstaged/untracked files, recent commits |
| Code review | `review`, `review --staged`, `review --branch main` | Severity-ranked findings (CRITICAL to SUGGESTION) with file, lines, problem, why and fix |
| Commit assistant | `commit`, `commit --dry-run`, `--single` | Splits unrelated changes into one commit per feature, writes concise Conventional Commit messages, asks for **one explicit approval** for the plan, then commits and verifies each |
| Worklog / timesheet | `worklog [period] [--from --to]` | Per-day logical tasks with evidence and confidence; normal, formal, structured, CSV and JSON output |
| Task grouping | (inside worklog/summary) | Groups related commits into one task (by scope, shared files, similar wording) |
| Summaries | `today`, `yesterday`, `week`, `summary` | Work grouped by area (Backend, Frontend, Tests, Docs, Config/CI) plus bug fixes and totals |
| Standup | `standup`, `standup week --plan "..."` | Previous work, today, blockers; every line labelled Observed or User-provided |
| Branch comparison | `compare main` | Ahead/behind, files, +/-, major changes, local branches not yet merged |
| PR description | `pr main` | Title, summary, changes, testing, potential risks |
| Health check | `health` | Conflicts, divergence, large/generated/temp files, `.env`, likely secrets (values withheld), debug statements |
| Agent | `ask "..."`, `chat` | Tool-using conversational agent over all of the above |

## Architecture

```
User → cli.py (Typer + Rich, I/O only)
          │
          ├── services/        deterministic logic (works without the LLM: --no-llm)
          │     grouping · worklog · summaries · review · commits · pr · health
          │
          └── agent.py         one orchestrating agent: LLM ⇄ tools, max tool calls, history trimming
                 │
                 ├── llm.py    the ONLY module that talks to the OpenAI-compatible API
                 └── tools/    typed tools + registry (JSON schemas) for the LLM
                        git.py (GitRunner: the only code that runs `git`)
                        filesystem.py · repository.py · registry.py

safety.py   secret masking · sensitive-file detection · ProposedOperation + human approval gate
config.py   environment variables + optional .gitworklog/config.json
prompts.py  system and feature prompts carrying the evidence rules
```

How grounding works:

1. Services collect evidence deterministically and group commits into tasks.
2. The LLM only rewrites the wording.
3. Its output is checked against the evidence before use:
   - Worklog tasks with invented IDs are dropped.
   - Review findings must name a file that is in the reviewed diff.
   - Commit messages must pass a Conventional Commit check.
   - PR "Testing" sections are never written by the model.
4. If the LLM fails, the deterministic output is shown instead.

No agent framework is used. The loop in `agent.py` is about 100 lines.

## Installation

Requires Python 3.11+ and Git.

```bash
cd gitworklog
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"      # Windows
# source .venv/bin/activate && pip install -e ".[dev]"   # macOS/Linux
```

## Environment setup

Credentials come only from environment variables:

```bash
copy .env.example .env        # then set NVIDIA_API_KEY=...
```

| Variable | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | (required for LLM features) | API key for the NVIDIA-hosted endpoint |
| `GITWORKLOG_MODEL` | `openai/gpt-oss-20b` | Model name |
| `GITWORKLOG_BASE_URL` | `https://integrate.api.nvidia.com/v1` | OpenAI-compatible endpoint |

`.env` is read from the GitWorklog install directory or from `~/.gitworklog/.env`, and never
overrides real environment variables. The `.env` of the repository you analyse is **not** read.
It belongs to that project, and reading it would let any repository redirect the tool's LLM
traffic.

Optional per-repository settings go in `.gitworklog/config.json`. Secrets are rejected there.

```json
{
  "base_branch": "main",
  "commit_style": "conventional",
  "commit_subject_max": 72,
  "commit_body_max_lines": 6,
  "commit_body_line_max": 100,
  "commit_max_words": 80,
  "commit_target_words": 15,
  "max_tool_calls": 20
}
```

The commit message limits work like this:

| Setting | Default | Allowed values | Meaning |
|---|---|---|---|
| `commit_subject_max` | 72 | 20–200 | Maximum length of the whole first line, including `type(scope): ` |
| `commit_body_max_lines` | 6 | 0–50 | Maximum body lines; `0` means subject line only |
| `commit_body_line_max` | 100 | 40–500 | Maximum length of each body line |
| `commit_max_words` | 80 | 5–300 | Maximum words in the whole message |
| `commit_target_words` | 15 | 3–300 | Length generated messages aim for (not enforced) |

How they are enforced:

- The model is told the limits.
- A generated body that is too long is trimmed.
- A generated subject that is too long gets one retry, then a heuristic message is used.
- A message you pass with `-m` or type in, and one the chat agent writes, is refused before
  any approval prompt if it breaks a limit.

### Feature-wise commits

`gitworklog commit` makes one commit per feature or logical change.

- Changes that belong to the same feature stay together: code, its tests, its docs, its config.
  If everything serves one feature, you get one commit.
- Unrelated changes are split into separate commits, for example a new rider feature and an
  unrelated auth fix.
- With the LLM, the model groups the files by feature and writes each message. The plan is then
  checked: every file must be in exactly one commit, files the model invents are ignored, a file
  listed twice stays in its first commit, and files the model misses get their own commit.
- Without the LLM, or if the model's plan is unusable, files are grouped by module. Tests and
  docs join the module their path names, and generic files such as `README.md` or lockfiles join
  the largest group.
- You approve the whole plan once. The prompt lists every commit, its files and message, and
  every exact `git add` / `git commit` command. Commits are then created in order, and each is
  checked for the right files.
- Files you staged yourself are split with `git commit -- <files>`. If a file is only partly
  staged, your index is committed as one commit so your staging is kept exactly as it is.
- `--single` forces one commit. `-m "message"` also always makes one commit.
- Splitting works on whole files. A single file with two unrelated edits stays in one commit.

## Usage

Run from inside any repository, or pass `--repo PATH` (`-C PATH`). Global options go before the
command: `gitworklog --no-llm worklog week`.

```bash
gitworklog                              # no command: chat mode in the current repository
gitworklog status
gitworklog review                       # staged + unstaged + untracked changes
gitworklog review --branch main         # whole branch vs main
gitworklog commit --dry-run             # show the commit plan only
gitworklog commit                       # split by feature, show, ask once, commit, verify
gitworklog commit --single              # everything in one commit
gitworklog worklog                      # today
gitworklog worklog week
gitworklog worklog --from 2026-09-01 --to 2026-09-30 --format csv -o september.csv
gitworklog worklog --from 2026-09-25 --to 2026-09-25 --hours 7 --split-hours
gitworklog today | yesterday | week
gitworklog summary --from 2026-09-01
gitworklog standup --plan "Pair with QA on payouts"
gitworklog compare main
gitworklog pr main
gitworklog health                       # exit code 2 if a problem (✗) is found
gitworklog ask "what did I work on this week?"
gitworklog chat                         # 'reset' clears history, 'exit' quits
```

Worklog options: `--author "name or email"` (default: your `git config user.email`),
`--all-authors`, `--format normal|formal|structured|csv|json`, `--output FILE`.
Periods: `today`, `yesterday`, `week`, `last-week`, `month`, `last-month`.

## Worklog examples

Normal:

```
Worklog: 2026-09-25 to 2026-09-26 (author: dev@example.com)

Sep 25 (Friday)
- Implemented rider availability endpoint and validation
    - Added availability endpoint to riderController.py
    - Implemented status validation logic in riderService.py
    - Wrote tests for availability in tests/test_rider_availability.py
- Fixed authentication token handling for expired and invalid tokens
    - Updated token handling in backend/auth/tokens.py
    - Modified middleware to return 401 on invalid token
  Evidence: 7 commit(s), 6 file(s); commit activity 09:15-17:45 (timestamps, not hours)

Git history can identify development activity, but it cannot reliably determine the exact
number of hours worked.
```

Formal, with hours you supply:

```
Sep 25, 2026 [7 hours, user-provided]: Implemented rider availability endpoint, validation
logic, and corresponding tests. Fixed authentication token handling for expired and invalid tokens.
```

CSV (`--format csv`):

```
date,task,details,commits,files,confidence,hours_user_provided,hours_suggested_split
2026-09-25,Implemented rider availability,Add availability endpoint; ...,e4dc7d02 fe8937b4 bac0065d,3,High,,
2026-09-26,Worked on settings,Changes in Settings.jsx,b4975baf,1,Low,,
```

Confidence levels:

- **High**: commits share a Conventional Commit scope or files and have clear messages.
- **Medium**: commits are grouped by similar wording only.
- **Low**: messages are vague (such as "wip"), so the task is described from file names only.

## Git history and working hours

Git history can identify development activity, but it cannot reliably determine the exact
number of hours worked.

- Commit timestamps show *when* commits were made (the "commit activity" window). They do not
  show time spent reading, designing, debugging, in meetings or on uncommitted work.
- Hours are shown **only** when you supply them (`--hours N`, meaning N hours per active day).
  They are labelled `USER-PROVIDED`.
- `--split-hours` divides *your* hours across that day's tasks by relative change size. The
  result is labelled `SUGGESTED` and is an estimate, not a measurement.

## Safety

- **No arbitrary shell.** The LLM can only call typed tools. `GitRunner.run` enforces a
  read-only allowlist: `reset`, `push`, `clean -f`, `branch -D`, `checkout --`, `remote add`,
  `config <set>` and similar are refused.
- **Human approval for every change.**
  - Staging, committing, file edits, and every destructive or outward-facing operation
    (`reset --hard`, `clean`, `checkout -- file`, `branch -D`, `rebase`, `merge`, `push`,
    `push --force`) need your approval.
  - They run only through `GitRunner.run_approved`, after `safety.Approver` gets your answer.
  - The prompt shows what will happen, the consequences, **every exact command**, and a preview
    (diff or status).
  - Destructive operations require typing `yes`. Commits accept `y`.
  - The LLM cannot approve anything. When stdin is not a terminal, approval is refused unless
    you explicitly set `GITWORKLOG_ALLOW_PIPED_APPROVAL=1`.
- **Vague requests are not treated as destructive.** "clean my branch" leads the agent to
  inspect and ask, not to run `git reset --hard`.
- **Commits are checked.** After committing, the tool confirms HEAD moved and that `log -1`
  matches. A rejected commit leaves the index and HEAD untouched.
- **Secrets are protected.**
  - Every tool result, diff and printed line passes through `mask_secrets`.
  - `.env` files and key files are never read, staged or shown; they appear as
    "Potential secret file … withheld".
  - Health and review report only `file:line (type)`, never values.
  - The commit assistant warns when the lines being committed contain a potential secret.
- **Context is limited.** Output caps for tool output, diffs (per file and in total), file
  reads, logs and history. Large diffs list the files that were truncated or not reviewed.

## Testing

```bash
.venv\Scripts\python -m pytest -q                         # unit + temp-repo end-to-end
.venv\Scripts\ruff check src tests                        # lint
set GITWORKLOG_LIVE_TESTS=1 && .venv\Scripts\python -m pytest -m live   # real LLM
```

The suite builds temporary Git repositories with fixed author dates and timezones. It covers:

- Git wrappers and output parsing.
- Date filtering, including the author's local day across timezones.
- Task grouping.
- Every worklog format and the hours rules.
- Secret masking.
- Approval, rejection and destructive-operation protection.
- Tool validation.
- The agent loop with a scripted fake LLM, including iteration limits and history trimming.
- CLI end-to-end runs.

## Development workflow

See [CLAUDE.md](CLAUDE.md) for the project rules. In short:

- Business logic goes in `services/`.
- Git access goes in `tools/git.py`.
- The API goes in `llm.py`.
- Every new tool needs input validation, output caps, masking and a test.
- Run `pytest` and `ruff check` before finishing a change.

## Known limitations

- Grouping is heuristic (Conventional Commit scopes, shared files, keyword similarity). Clear
  commit messages and scopes give the best results.
- Only committed work is visible to worklogs. Squashed or rebased history shows the final
  commits, not the original sessions.
- Review quality depends on the model (default `gpt-oss-20b`) and on diff budgets. Very large
  changes are reviewed partially and say so.
- Area detection (Backend, Frontend and so on) uses file paths and extensions.
- No hosted-service integration (GitHub, GitLab, Jira) yet. PR descriptions are printed, not
  posted.

## Future improvements

GitHub/GitLab integration and PR creation; Jira/Linear, calendar and Slack integration; real
development-session tracking; persistent project memory; specialised review agents and parallel
subagents; MCP integrations; automatic daily worklog generation; IDE integration.
