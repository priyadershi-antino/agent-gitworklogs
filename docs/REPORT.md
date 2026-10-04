# GitWorklog Agent: Project Report

| | |
|---|---|
| **Project** | GitWorklog Agent, a local command-line AI assistant for Git |
| **Version** | 0.1.0 (MVP) |
| **Language / runtime** | Python 3.11+ (developed on 3.13.3) |
| **Size** | about 5,300 lines of source, about 2,000 lines of tests, 219 tests |
| **Model** | `openai/gpt-oss-20b`, hosted by NVIDIA behind an OpenAI-compatible API |
| **License / status** | Local project, 4 commits on `main`, not pushed to a remote |

---

## 1. Executive summary

GitWorklog Agent answers one question reliably: **"What did I actually work on during this time
period?"** It reads a Git repository, groups commits into logical tasks and produces timesheet-ready
text. It also reviews code, writes commits, summarises work, prepares standups and pull request
descriptions, compares branches and checks repository health.

Its design rests on three principles:

1. **Evidence first.** Every statement comes from commits, files or diffs. The AI rewrites wording;
   it does not decide what happened. Its output is checked against the evidence before use.
2. **No invented hours.** Git timestamps show activity, not time worked. Hours appear only when
   the user supplies them.
3. **Nothing changes without approval.** Staging, committing, file edits and destructive Git
   operations need an explicit human answer, and secrets are never shown.

It is a small, single-agent system with no agent framework: a direct tool-calling loop over the
OpenAI-compatible API, with typed tools and a safety layer.

---

## 2. Capabilities at a glance

| # | Capability | Command | Needs the AI? |
|---|---|---|---|
| 1 | Repository and Git status | `status` | No |
| 2 | Code review | `review` | Optional (static checks without it) |
| 3 | Commit assistant (feature-wise) | `commit` | Optional |
| 4 | Evidence-based worklog | `worklog` | Optional |
| 5 | Intelligent task grouping | (inside worklog and summaries) | No |
| 6 | Daily / weekly summary | `today`, `yesterday`, `week`, `summary` | Optional |
| 7 | Standup generator | `standup` | Optional |
| 8 | Branch comparison | `compare` | No |
| 9 | PR description generator | `pr` | Optional |
| 10 | Repository health and safety check | `health` | No |
| 11 | Conversational agent | `chat`, `ask`, or bare `gitworklog` | Yes |

---

## 3. Usage modes

All modes work on the repository in the current folder, or on `-C <path>`.

### 3.1 Chat mode (default)

```powershell
gitworklog          # or: gitworklog chat
```

- An interactive conversation. The agent remembers earlier messages, so "fix that" and "explain
  it" work.
- It decides which tools to call and shows them as `-> git_diff`.
- It can review, edit, stage and commit, but each change asks for approval.
- Commands inside chat: `reset` clears the conversation; `exit` quits.
- Needs `NVIDIA_API_KEY`.

### 3.2 Ask mode (single question)

```powershell
gitworklog ask "what did I work on this week?"
```

The same agent and safety rules as chat, but it answers once and exits, with no memory afterwards.
Useful for scripts and quick lookups. Needs `NVIDIA_API_KEY`.

### 3.3 Direct commands (task mode)

Fixed, predictable jobs. The AI only improves wording.

| Command | Purpose | Notable options |
|---|---|---|
| `status` | Branch, changes, recent commits | |
| `review` | Severity-ranked review | `--staged`, `--branch main` |
| `commit` | Split into feature commits, approve once, commit and verify | `--dry-run`, `--single`, `-m "msg"`, `--all` |
| `worklog` | Timesheet per day | `today`, `yesterday`, `week`, `last-week`, `month`, `last-month`, `--from/--to`, `--format normal\|formal\|structured\|csv\|json`, `--hours N`, `--split-hours`, `-o FILE`, `--author`, `--all-authors` |
| `today`, `yesterday`, `week` | Summary by area | `--author`, `--all-authors` |
| `summary` | Summary for any range (default: this week) | `--from`, `--to` |
| `standup` | Last active day, today in progress, blockers | `week`, `--plan "..."` |
| `compare [base]` | Branch vs base | |
| `pr [base]` | PR description | |
| `health` | Hygiene and secrets check | exit code 2 on a problem |
| `version` | Version | |

### 3.4 Offline mode (no AI)

```powershell
gitworklog --no-llm worklog week
```

Works without an API key or network, is instant, and is fully deterministic. Wording is plainer
and review uses static checks only. `ask`, `chat` and the bare command refuse to run with `--no-llm`.

### 3.5 Global options (combine with any mode)

| Option | Effect |
|---|---|
| `-C, --repo PATH` | Analyse another repository |
| `--no-llm` | Deterministic output only |
| `-v, --verbose` | Show tool results |

Global options go **before** the command: `gitworklog -C D:\proj --no-llm worklog week`.

---

## 4. Features in detail

### 4.1 Status intelligence
Branch, detached-HEAD state, upstream, commits ahead and behind, staged, unstaged, untracked and
conflicted files, merge, rebase, cherry-pick or revert in progress, and the latest commits. Large
lists are summarised, not dumped.

### 4.2 Code review
- Scope: working tree (including untracked files), `--staged`, or `--branch BASE`.
- Findings carry **Severity** (CRITICAL, HIGH, MEDIUM, LOW, SUGGESTION), file, line range, problem,
  why it matters, suggested fix and an evidence label (FACT or INFERENCE).
- Two layers:
  - **Static checks** catch likely hard-coded secrets, merge-conflict markers, debug statements,
    new TODOs and large untested changes.
  - **AI review** covers correctness, edge cases, security, error handling, performance,
    maintainability and missing tests.
- Findings that do not reference a file in the reviewed diff are discarded.
- If nothing meaningful is found, it says so.
- Large diffs are budgeted. Lock files and generated files are skipped, and skipped or truncated
  files are listed.

### 4.3 Commit assistant (feature-wise)
1. Inspect status and the diff.
2. Group the changes: **one commit per feature or logical change**. Code, its tests, its docs and
   its config stay together. Unrelated work is split into separate commits.
3. Write a concise Conventional Commit message for each.
4. Show a single plan: every commit, its files, its message and every exact `git add` and
   `git commit` command.
5. Ask for **one explicit approval**.
6. Create the commits in order and verify each one (HEAD moved, correct files).

Grouping rules and protections:
- With the AI, the model groups the files and the plan is validated. Every file must be in
  exactly one commit, invented files are ignored, a duplicated file stays in its first commit and
  missing files get their own commit.
- Without the AI, or if the plan is unusable, files are grouped by module. Tests and docs join
  the module they name, and generic files join the largest group.
- Files you staged are split with `git commit -- <files>`. If a file is partly staged, the index
  is committed as one commit so your staging stays exactly as it is.
- Secret files (`.env`, keys) are never auto-staged. A warning appears if added lines contain a
  possible secret.
- Splitting works on whole files; one file with two unrelated edits stays in one commit.

Message limits (configurable): subject at most 72 characters, whole message at most 80 words
(aim for about 15), body at most 6 lines of 100 characters. A model message that is too long is
trimmed or retried; a message you write that is too long is refused before any approval prompt.

### 4.4 Worklog generator
- Collects commits across **all branches** for a date range, using the author's local date (so
  timezones do not shift days) and your `git config user.email` by default.
- Groups them per day into logical tasks, each with retained evidence (commit SHAs, files,
  additions and deletions, grouping reason) and a confidence rating.
- Formats: **normal**, **formal** (paragraphs), **structured** (table: Date | Task | Evidence |
  Confidence), **csv** and **json**. `-o FILE` writes to a file.
- Hours: shown only with `--hours N` (hours per active day, labelled USER-PROVIDED).
  `--split-hours` proportionally divides your hours across tasks by change size, labelled SUGGESTED.

### 4.5 Task grouping (deterministic)
Related commits become one task; unrelated commits stay separate. Grouping uses:
- shared Conventional Commit scope,
- shared non-generic files,
- keyword similarity of messages and file names (Jaccard at least 0.3),
- distinct explicit scopes as hard task boundaries.

Confidence:
- **High**: shared scope or files with descriptive messages.
- **Medium**: grouped by similarity only.
- **Low**: vague messages such as "wip"; the task is described from file names only.

Example: 15 commits across rider, API and profile work collapse into 3 tasks.

### 4.6 Summaries
Work grouped by area (Backend, Frontend, Tests, Docs, Config/CI) plus a "Bug fixes" section, with
commit count, files changed and line totals.

### 4.7 Standup
- Previous work: the most recent active day (handles weekends).
- Today: commits so far, uncommitted work in progress and items you add with `--plan`.
- Blockers: merge conflicts, rebase in progress or a diverged branch.
- Every line is labelled **Observed** or **User-provided**. If nothing is known it says
  "No confirmed future work from Git history" and "No blockers observable from repository evidence".

### 4.8 Branch comparison
Current branch, base, merge base, commits ahead and behind, files changed, additions and deletions,
major logical changes and other local branches not yet merged. It never merges, rebases, resets or
deletes anything.

### 4.9 PR description
Title, summary, changes, testing and potential risks, built from the branch's commits and diff.
The **Testing** section is always generated by code: "Tests were not executed by GitWorklog",
plus the test files changed on the branch. Risks are labelled as inferences.

### 4.10 Health check
Repository detected, detached HEAD, merge conflicts, operations in progress, uncommitted changes,
upstream divergence, large files (over 5 MB), tracked generated files (`node_modules`, `dist`,
`__pycache__`, `*.pyc`), temporary and backup files, `.env` and key files (and whether they are
git-ignored), likely secrets in tracked files (file, line and type only), debug statements
(`console.log`, `breakpoint()`, `pdb.set_trace`), missing `.gitignore` and stashes.

---

## 5. Use cases

| Who | Situation | What to run |
|---|---|---|
| Developer filling a timesheet | Month-end hours entry | `worklog --from 2026-09-01 --to 2026-09-30 --format formal --hours 8` |
| Contractor billing a client | Evidence for an invoice | `worklog --format csv -o month.csv` |
| Developer in daily standup | "What did I do yesterday?" | `standup --plan "..."` |
| Developer before committing | Catch bugs and secrets, keep commits clean | `review`, then `commit` |
| Developer with a messy working tree | Several unrelated changes at once | `commit` (splits by feature) |
| Developer opening a PR | Description and branch summary | `compare main`, `pr main` |
| Team lead | Weekly activity across the team | `summary --all-authors`, `worklog week --all-authors` |
| Reviewer | Branch review before merging | `review --branch main` |
| Engineer inheriting a repo | Hygiene audit | `health` |
| Anyone exploring history | Follow-up questions | `chat` |
| CI or scripts | Deterministic reports | `--no-llm worklog ... --format json`, `health` (exit code) |

---

## 6. Architecture

### 6.1 Overview

```
User
  |
CLI (Typer + Rich)  . . . . . . . . . . . . . . . . . . I/O and prompts only
  |
  +-- Services (deterministic logic; work with --no-llm)
  |     grouping | worklog | summaries | review | commits | pr | health
  |
  +-- Agent orchestrator (chat / ask)
        |   tool loop, max tool calls, history trimming
        |
        +-- LLM layer (llm.py)      the ONLY module that talks to the model API
        |
        +-- Tool registry           typed tools + JSON schemas
              |-- git.py            GitRunner: the ONLY code that runs `git`
              |-- filesystem.py     read, search and approved edits
              |-- repository.py     repository facts
              `-- registry.py       validation and dispatch

Cross-cutting:  safety.py  (secret masking, sensitive paths, approval gate)
                config.py  (environment variables, per-repo config)
                prompts.py (evidence rules, feature prompts)
                models.py  (dataclasses: Commit, Task, RepoStatus, ...)
```

### 6.2 Module responsibilities

| Module | Lines | Role |
|---|---|---|
| `cli.py` | 601 | Commands, rich output, interactive approval prompt |
| `agent.py` | 110 | Tool-calling loop, tool-call limit, history compaction |
| `llm.py` | 173 | API client, retries, tool-call parsing, JSON extraction |
| `tools/git.py` | 797 | Allowlisted git runner, parsers, read and write operations |
| `tools/registry.py` | 377 | 15 tool definitions, argument validation, error handling |
| `services/grouping.py` | 517 | Commit-to-task grouping and classification |
| `services/commits.py` | 471 | Feature-wise commit planning, limits, execution |
| `services/worklog.py` | 359 | Date ranges, collection, rendering, hours rules |
| `services/review.py` | 320 | Diff budgeting, static checks, validated AI findings |
| `services/health.py` | 280 | Hygiene checks |
| `services/summaries.py` | 191 | Summaries and standup |
| `services/pr.py` | 165 | Comparison and PR description |
| `safety.py` | 202 | Masking, approval gate |
| `models.py` | 250 | Data structures |
| `config.py` | 135 | Settings and limits |
| `prompts.py` | 145 | Prompt text |

### 6.3 The agent loop

1. Send the system prompt, history and tool definitions to the model.
2. If the model returns tool calls, validate and run each one, mask and cap the result, append it
   and repeat.
3. If it returns plain text, that is the answer.
4. After the tool-call limit (default 20), make one final call with tools disabled and ask for the
   best answer from the evidence gathered.

Context control: old tool output is shrunk, and the oldest whole turns are dropped past a size
budget without separating a tool call from its result.

### 6.4 The grounding pattern

Services build the evidence deterministically. The model only rewrites wording, and its output is
validated:

| Output | Validation |
|---|---|
| Worklog task text | Only known task IDs accepted; invented tasks dropped |
| Review findings | Must name a file in the reviewed diff; severity normalised |
| Commit messages | Must match the Conventional Commit format and length limits |
| Commit split plan | Every file in exactly one commit; invented files ignored |
| PR Testing section | Never written by the model |

If the model fails or returns something invalid, the deterministic output is used instead.

---

## 7. The tools (15)

No shell or "run any command" tool exists. The model can call only these.

**Read-only (11)**

| Tool | Purpose |
|---|---|
| `git_status` | Branch, ahead/behind, changed files, operations in progress |
| `git_diff` | Diff and per-file stats (unstaged, staged or branch vs base) |
| `git_log` | Commits with changed files |
| `git_show` | One commit in detail |
| `git_branch` | Local and remote branches with tracking info |
| `git_compare` | Current branch vs base |
| `repository_info` | Root, HEAD, remotes (credentials stripped), commit count, user email |
| `read_file` | Line-numbered file reading, confined to the repository |
| `search_files` | Fixed-string search through tracked files |
| `worklog_evidence` | Commits for a date range grouped into tasks |
| `health_check` | Hygiene and secrets check |

**Changing tools (4, always need approval)**

| Tool | Purpose | Safeguard |
|---|---|---|
| `git_add` | Stage files | Refuses secret files |
| `git_commit` | Commit staged changes | Length limits checked before approval; commit verified after |
| `apply_edit` | Replace one exact snippet | Shows a diff; match must be unique; line endings preserved |
| `protected_git_operation` | `push`, `restore_file`, `delete_branch`, `merge`, `rebase`, `reset_hard`, `clean` | Shows effect and exact command; user must type `yes` |

---

## 8. Safety and security model

| Risk | Control |
|---|---|
| Arbitrary command execution | No shell tool. `GitRunner.run` is a read-only allowlist; `reset`, `push`, `clean -f`, `branch -D`, `checkout --`, `remote add`, `config` writes and similar are refused. Refs and paths are validated and must not start with `-`. |
| Unapproved changes | Mutations run only via `run_approved` after `Approver.confirm`. The model cannot approve. Non-interactive input is denied unless `GITWORKLOG_ALLOW_PIPED_APPROVAL=1` is set. |
| Vague destructive requests | The agent is instructed to inspect and ask. "clean my branch" never becomes `reset --hard`. |
| Secret leakage | Every tool result, diff and printed line is masked (private keys, AWS, GitHub, NVIDIA, OpenAI, Slack and Google keys, JWTs, URL credentials, `password = "..."`, `.env`-style values). `.env` and key files are never read, shown or staged. |
| Prompt exfiltration through a repo | The analysed repo's own `.env` is not loaded, so it cannot redirect the tool's API key or endpoint. |
| Path escape | File access resolves inside the repository root only. |
| Runaway agent | Tool-call limit, per-output size caps, history budget. |
| False claims | Evidence labels (FACT, INFERENCE, SUGGESTION), no estimated hours, tests never claimed as run. |
| Bad commits | Verification after every commit; failure reports what was already created. |

---

## 9. Technology stack

| Layer | Technology | Version | Why |
|---|---|---|---|
| Language | Python | 3.11+ (3.13.3 used) | Typed, ubiquitous, strong stdlib |
| Version control access | Git via `subprocess` | 2.49 | No library dependency; precise control |
| LLM client | `openai` SDK | 3.21.0 | OpenAI-compatible API, tool calling |
| Model | `openai/gpt-oss-20b` | n/a | Hosted by NVIDIA; tool calls and reasoning |
| Endpoint | `integrate.api.nvidia.com/v1` | n/a | OpenAI-compatible |
| CLI | Typer | 0.27.2 | Commands and options from type hints |
| Terminal output | Rich | 15.0.0 | Tables, panels, Markdown |
| Configuration | python-dotenv | 1.2.3 | Environment loading |
| Tests | pytest | 9.1.1 | Fixtures, parametrisation |
| Lint and format | Ruff | 0.16.9 | Fast; one tool for both |
| Packaging | setuptools via `pyproject.toml` | n/a | Installable, `gitworklog` entry point |

Only four runtime dependencies. No LangChain, LangGraph, CrewAI or AutoGen.

---

## 10. About the model

| Property | Detail |
|---|---|
| Model | `openai/gpt-oss-20b` (open-weight, about 20 billion parameters) |
| Hosting | NVIDIA, through `https://integrate.api.nvidia.com/v1` |
| Interface | OpenAI-compatible chat completions with `tools` / `tool_calls` |
| Behaviour | A reasoning model; returns `reasoning_content` alongside answers |
| Settings used | temperature 0.2, max 8192 tokens, 120 s timeout, 2 SDK retries |
| Credential | `NVIDIA_API_KEY` environment variable |
| Override | `GITWORKLOG_MODEL`, `GITWORKLOG_BASE_URL` |

How the project works with this model:
- Assistant messages are rebuilt from role, content and tool calls only, so provider-specific
  reasoning fields are not sent back.
- The model sometimes leaks template tokens into tool names (`git_status<|channel|>commentary`);
  these are cleaned.
- Short tasks (commit messages) use `reasoning_effort="low"` for speed.
- JSON replies are extracted even from code fences or surrounding prose, with one retry.
- Quality varies, so every use is validated against evidence, and the deterministic path remains
  available.

---

## 11. Configuration

Credentials only through environment variables (`NVIDIA_API_KEY`), loaded from the real
environment, `~/.gitworklog/.env` or the tool's own `.env`. Never from the analysed repository.

Optional per-repo `.gitworklog/config.json` (secrets are rejected):

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `base_branch` | `main` | n/a | Default for `compare`, `pr` |
| `commit_style` | `conventional` | n/a | Only Conventional Commits supported |
| `commit_subject_max` | 72 | 20 to 200 | First-line length |
| `commit_max_words` | 80 | 5 to 300 | Whole-message words |
| `commit_target_words` | 15 | 3 to 300 | Length the model aims for |
| `commit_body_max_lines` | 6 | 0 to 50 | `0` means subject only |
| `commit_body_line_max` | 100 | 40 to 500 | Body line length |
| `max_tool_calls` | 20 | 1 to 100 | Agent loop limit |
| `model`, `temperature` | | | Model overrides |

---

## 12. Quality and testing

- **219 tests**, plus 3 live tests against the real model (opt-in with `GITWORKLOG_LIVE_TESTS=1`).
- Tests build temporary Git repositories with fixed dates and timezones and use a scripted fake LLM.
- Coverage areas: Git wrappers and parsing, date filtering, task grouping, every worklog format
  and the hours rules, secret masking, approval and rejection, destructive-operation protection,
  tool validation, the agent loop (limits, history, errors), commit splitting and message limits,
  and CLI end-to-end runs.
- Ruff lint and format pass.
- Manual end-to-end runs against a sample repository with the real model confirmed review, commit
  splitting, worklogs, standup, PR and the approval flow.

Defects found and fixed during testing include: the analysed repo's `.env` overriding the API key;
the approval prompt hiding the `git add` step; Windows line endings being altered by file edits;
porcelain status misparsed (`.` means unchanged); related-work leakage between tasks; model claims
not supported by the diff; camelCase commit scopes being rejected; and false secret warnings on
`os.environ.get(...)` code.

---

## 13. Known limitations

- Task grouping is heuristic. Clear commit messages and Conventional Commit scopes give the best results.
- Only committed work appears in worklogs. Squashed or rebased history shows final commits, not sessions.
- Time spent reading, designing, in meetings or on uncommitted work is invisible to Git.
- Commit splitting works on whole files, not on separate edits within one file.
- Review and wording quality depend on a 20B model; very large diffs are reviewed partially and say so.
- Model latency varies (about 2 to 50 seconds per call).
- Area detection (Backend, Frontend and so on) uses file paths and extensions.
- No integration with hosted services; PR descriptions are printed, not posted.
- Not pushed to a remote; no CI configured yet.

---

## 14. Future improvements

GitHub and GitLab integration and PR creation; Jira, Linear, calendar and Slack integration; real
development-session tracking for actual hours; persistent project memory; specialised review
agents and parallel subagents; MCP integrations; automatic daily worklog generation; IDE
integration; splitting a single file's hunks into separate commits; support for other commit styles.

---

## 15. Quick-start

```powershell
cd D:\Learning\Agent\gitworklog
.venv\Scripts\gitworklog.exe --help            # smoke test
cd D:\your\git\project
gitworklog                                     # chat mode
gitworklog worklog week                        # timesheet
gitworklog commit                              # feature-wise commits
```

Run from anywhere by adding a launcher on PATH (see the README), and set `NVIDIA_API_KEY` in
`gitworklog\.env`.

Development: `.venv\Scripts\python -m pytest -q` and `.venv\Scripts\ruff check src tests`.
See `CLAUDE.md` for the project's development rules.
