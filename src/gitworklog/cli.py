"""Command-line interface. I/O only: all logic lives in services/, tools/ and agent.py."""

from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from gitworklog import __version__
from gitworklog.agent import Agent, AgentEvent
from gitworklog.config import ConfigError, Settings, load_settings
from gitworklog.llm import LLMClient, LLMError
from gitworklog.prompts import agent_system_prompt
from gitworklog.safety import OperationDenied, ProposedOperation, mask_secrets
from gitworklog.services import commits as commit_service
from gitworklog.services import health as health_service
from gitworklog.services import pr as pr_service
from gitworklog.services import review as review_service
from gitworklog.services import summaries, worklog
from gitworklog.tools.git import GitError, GitRunner, git_log, git_status, operation_in_progress
from gitworklog.tools.registry import ToolContext, build_registry

app = typer.Typer(
    help="GitWorklog Agent: Git intelligence and evidence-based worklogs. "
    "Run without a command to start chat mode.",
    add_completion=False,
    rich_markup_mode=None,
)
console = Console(highlight=False, soft_wrap=True)


@dataclass
class AppState:
    repo: Path
    no_llm: bool = False
    verbose: bool = False
    _git: GitRunner | None = None
    _settings: Settings | None = None

    @property
    def git(self) -> GitRunner:
        if self._git is None:
            git = GitRunner(self.repo)
            self._settings = load_settings(git.root)
            git.limits = self._settings.limits
            self._git = git
        return self._git

    @property
    def settings(self) -> Settings:
        _ = self.git  # settings are loaded together with the repository
        assert self._settings is not None
        return self._settings

    def llm(self, required: bool = False) -> LLMClient | None:
        if self.no_llm:
            if required:
                raise ConfigError("This command needs the LLM; remove --no-llm.")
            return None
        settings = self.settings
        if not settings.api_key:
            if required:
                settings.require_api_key()
            console.print("[yellow]NVIDIA_API_KEY not set: using deterministic output.[/]")
            return None
        return LLMClient(settings)


def _state(ctx: typer.Context) -> AppState:
    return ctx.ensure_object(AppState)


def emit(text: str) -> None:
    """Print plain text with secrets masked (defence in depth)."""
    console.print(mask_secrets(text), markup=False, highlight=False)


def cli_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (GitError, ConfigError, LLMError, ValueError) as exc:
            console.print(f"[red]Error:[/] {mask_secrets(str(exc))}", markup=True)
            raise typer.Exit(1) from exc

    return wrapper


def _interactive() -> bool:
    return sys.stdin.isatty() or os.environ.get("GITWORKLOG_ALLOW_PIPED_APPROVAL") == "1"


class ConsoleApprover:
    """Asks the human. Destructive operations require typing 'yes'."""

    def confirm(self, op: ProposedOperation) -> bool:
        style = "red" if op.destructive else "yellow"
        body = [f"What will happen: {op.summary}", "Exact operation(s):"]
        body += [f"  {i}. {cmd}" for i, cmd in enumerate(op.all_commands, 1)]
        body += [f"Consequence: {c}" for c in op.consequences]
        panel = Panel(mask_secrets("\n".join(body)), title="Approval required", border_style=style)
        console.print(panel, soft_wrap=False)
        if op.preview:
            console.print(mask_secrets(op.preview[:6000]), markup=False, highlight=False)
        if not _interactive():
            console.print("[red]Non-interactive session: operation NOT approved or executed.[/]")
            return False
        try:
            if op.destructive:
                answer = input("Type 'yes' to proceed (anything else cancels): ").strip()
                return answer == "yes"
            answer = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")


@app.callback(invoke_without_command=True)
def main_options(
    ctx: typer.Context,
    repo: Annotated[Path, typer.Option("--repo", "-C", help="Repository path")] = Path("."),
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Deterministic output only")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool calls")] = False,
) -> None:
    ctx.obj = AppState(repo=repo, no_llm=no_llm, verbose=verbose)
    if ctx.invoked_subcommand is None:
        chat(ctx)  # bare `gitworklog` opens chat mode in the current repository


@app.command()
def version() -> None:
    """Show the version."""
    emit(f"gitworklog {__version__}")


# ------------------------------------------------------------------------ git intelligence


@app.command()
@cli_errors
def status(ctx: typer.Context) -> None:
    """Branch, ahead/behind, staged/unstaged/untracked files and recent commits."""
    git = _state(ctx).git
    st = git_status(git)
    head = st.head[:8] if st.head else "no commits"
    lines = [f"Repository: {git.root}", f"Branch: {st.branch or 'DETACHED HEAD'} ({head})"]
    if st.upstream:
        lines.append(f"Upstream: {st.upstream} (ahead {st.ahead}, behind {st.behind})")
    else:
        lines.append("Upstream: none configured")
    for op in operation_in_progress(git):
        lines.append(f"In progress: git {op}")
    for label, entries in (
        ("Staged", st.staged),
        ("Unstaged", st.unstaged),
        ("Untracked", st.untracked),
        ("Conflicted", st.conflicted),
    ):
        if entries:
            lines.append(f"{label} ({len(entries)}):")
            lines += [f"  {e.path}" for e in entries[:25]]
            if len(entries) > 25:
                lines.append(f"  ... {len(entries) - 25} more")
    if st.is_clean:
        lines.append("Working tree clean")
    recent = git_log(git, max_count=5, no_merges=False)
    if recent:
        lines.append("Recent commits:")
        lines += [f"  {c.short_sha} {c.authored_at:%Y-%m-%d %H:%M} {c.subject}" for c in recent]
    emit("\n".join(lines))


@app.command()
@cli_errors
def review(
    ctx: typer.Context,
    staged: Annotated[bool, typer.Option("--staged", help="Review staged changes only")] = False,
    branch: Annotated[
        str | None, typer.Option("--branch", "-b", help="Review this branch vs BASE")
    ] = None,
) -> None:
    """Review current changes (or --staged, or --branch main) for bugs and risks."""
    state = _state(ctx)
    llm = state.llm()
    with console.status("Reviewing changes..."):
        result = review_service.review(state.git, llm, staged=staged, base=branch)
    emit(review_service.render_review(result))


_LIMIT_KEYS = "commit_subject_max, commit_max_words, commit_body_max_lines, commit_body_line_max"


def _limit_error(problems: list[str]) -> str:
    return (
        "; ".join(problems) + f". Shorten the message, or change the limits in "
        f".gitworklog/config.json ({_LIMIT_KEYS})."
    )


def _read_message() -> str:
    typed: list[str] = []
    while line := input():
        typed.append(line)
    return "\n".join(typed).strip()


def _maybe_edit_messages(messages: list[str], limits: commit_service.MessageLimits) -> list[str]:
    """Let the user replace any message; re-prompt while one breaks the length limits."""
    try:
        if input("\nEdit the message(s)? [y/N]: ").strip().lower() not in ("y", "yes"):
            return messages
        edited = []
        for i, current in enumerate(messages, 1):
            while True:
                emit(f"Commit {i}: new message, finish with an empty line (empty keeps it):")
                candidate = _read_message()
                if not candidate:
                    edited.append(current)
                    break
                problems = commit_service.length_problems(candidate, limits)
                if not problems:
                    edited.append(candidate)
                    break
                console.print(f"[yellow]Too long: {'; '.join(problems)}. Try again.[/]")
        return edited
    except EOFError:
        return messages


def _describe_limits(limits: commit_service.MessageLimits) -> str:
    body = (
        f"body <= {limits.body_max_lines} lines of {limits.body_line_max} chars"
        if limits.body_max_lines
        else "no body"
    )
    return (
        f"subject <= {limits.subject_max} chars, <= {limits.max_words} words "
        f"(aim ~{limits.target_words}), {body}"
    )


@app.command()
@cli_errors
def commit(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only show the commit plan")] = False,
    all_files: Annotated[bool, typer.Option("--all", "-a", help="Include untracked files")] = False,
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Use this message (one commit)")
    ] = None,
    single: Annotated[
        bool, typer.Option("--single", help="One commit, even for unrelated changes")
    ] = False,
) -> None:
    """Commit changes feature by feature: unrelated changes become separate commits."""
    state = _state(ctx)
    git = state.git
    limits = commit_service.MessageLimits.from_settings(state.settings)
    if message and (problems := commit_service.length_problems(message, limits)):
        raise commit_service.CommitMessageError(_limit_error(problems))
    llm = None if message else state.llm()
    with console.status("Inspecting and grouping changes..."):
        plan = commit_service.plan_commit(
            git, llm, include_untracked=all_files, limits=limits, split=not (single or message)
        )
    messages = [message] if message else [g.message for g in plan.groups]
    lines = [
        f"Branch: {plan.status.branch or 'DETACHED HEAD'}",
        ("Already staged" if plan.already_staged else "Will stage")
        + f": {len(plan.files)} file(s), +{plan.context.additions} / -{plan.context.deletions}",
    ]
    if plan.sensitive_skipped:
        lines.append(
            "Potential secret files (never auto-staged, values withheld): "
            + ", ".join(plan.sensitive_skipped)
        )
    if plan.untracked_skipped:
        lines.append(
            f"Untracked files not included (use --all): {', '.join(plan.untracked_skipped[:10])}"
        )
    lines += [*plan.notes, "", f"Commit plan (limits: {_describe_limits(limits)}):"]
    for i, (group, text) in enumerate(zip(plan.groups, messages, strict=True), 1):
        source = "provided with -m" if message else group.source
        lines += ["", f"[{i}/{len(plan.groups)}] {len(group.files)} file(s), message by {source}"]
        lines += [f"    {f}" for f in group.files[:30]]
        if len(group.files) > 30:
            lines.append(f"    ... {len(group.files) - 30} more")
        lines += ["", *[f"  {line}" for line in text.splitlines()]]
    emit("\n".join(lines))
    if dry_run:
        return
    if _interactive() and not message:
        messages = _maybe_edit_messages(messages, limits)
    for i, text in enumerate(messages, 1):
        if not commit_service.is_valid_message(text, limits):
            console.print(f"[yellow]Note: commit {i} is not in Conventional Commit format.[/]")
    try:
        results = commit_service.execute_commit(git, ConsoleApprover(), plan, messages, limits)
    except OperationDenied:
        emit("Commit cancelled. No changes were made to the repository.")
        return
    for result in results:
        emit(
            f"Committed and verified: {result['sha']} {result['subject']} "
            f"({len(result['files'])} file(s))"
        )
        if not result["files_match"]:
            console.print("[yellow]  Note: committed files differ from the plan; check git log.[/]")


# ------------------------------------------------------------------------------- worklogs

PeriodArg = Annotated[
    str | None, typer.Argument(help="today|yesterday|week|last-week|month|last-month")
]
FromOpt = Annotated[str | None, typer.Option("--from", help="Start date YYYY-MM-DD")]
ToOpt = Annotated[str | None, typer.Option("--to", help="End date YYYY-MM-DD (default today)")]
AuthorOpt = Annotated[str | None, typer.Option("--author", help="Author name/email substring")]
AllAuthorsOpt = Annotated[bool, typer.Option("--all-authors", help="Include every author")]


@app.command("worklog")
@cli_errors
def worklog_cmd(
    ctx: typer.Context,
    period: PeriodArg = None,
    date_from: FromOpt = None,
    date_to: ToOpt = None,
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="normal|formal|structured|csv|json")
    ] = "normal",
    hours: Annotated[
        float | None, typer.Option("--hours", help="Hours worked per active day (you provide)")
    ] = None,
    split_hours: Annotated[
        bool, typer.Option("--split-hours", help="Suggest a per-task split of --hours")
    ] = False,
    author: AuthorOpt = None,
    all_authors: AllAuthorsOpt = False,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write to file")] = None,
) -> None:
    """Evidence-based worklog / timesheet for a period (default: today)."""
    state = _state(ctx)
    rng = worklog.resolve_range(period, date_from, date_to)
    llm = state.llm()
    with console.status("Analysing commits..."):
        log = worklog.build_worklog(
            state.git,
            rng,
            author=author,
            all_authors=all_authors,
            hours=hours,
            split_hours=split_hours,
            llm=llm,
        )
    text = worklog.render(log, fmt)
    if output:
        output.write_text(mask_secrets(text), encoding="utf-8")
        emit(f"Wrote {fmt} worklog to {output}")
        return
    if fmt == "structured":
        table = Table(title=f"Worklog: {rng.label}", show_lines=True)
        for column in ("Date", "Task", "Evidence", "Confidence", "Hours"):
            table.add_column(column)
        for row in worklog.structured_rows(log):
            hrs = row["hours_suggested_split"] or row["hours_user_provided"]
            label = ("suggested" if row["hours_suggested_split"] != "" else "user") if hrs else ""
            table.add_row(
                row["date"],
                mask_secrets(row["task"]),
                row["evidence"],
                row["confidence"],
                f"{hrs} ({label})" if hrs else "-",
            )
        console.print(table)
        emit(worklog.HOURS_NOTICE)
    elif fmt in ("csv", "json"):
        typer.echo(mask_secrets(text))
    else:
        emit(text)


def _summary(
    ctx: typer.Context,
    period: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
    author: str | None = None,
    all_authors: bool = False,
) -> None:
    state = _state(ctx)
    rng = worklog.resolve_range(period, date_from, date_to)
    llm = state.llm()
    with console.status("Summarising..."):
        result = summaries.build_summary(
            state.git, rng, author=author, all_authors=all_authors, llm=llm
        )
    emit(summaries.render_summary(result))


@app.command()
@cli_errors
def today(ctx: typer.Context, author: AuthorOpt = None, all_authors: AllAuthorsOpt = False):
    """Summary of today's development."""
    _summary(ctx, "today", author=author, all_authors=all_authors)


@app.command()
@cli_errors
def yesterday(ctx: typer.Context, author: AuthorOpt = None, all_authors: AllAuthorsOpt = False):
    """Summary of yesterday's development."""
    _summary(ctx, "yesterday", author=author, all_authors=all_authors)


@app.command()
@cli_errors
def week(ctx: typer.Context, author: AuthorOpt = None, all_authors: AllAuthorsOpt = False):
    """Summary of this week's development."""
    _summary(ctx, "week", author=author, all_authors=all_authors)


@app.command()
@cli_errors
def summary(
    ctx: typer.Context,
    period: PeriodArg = None,
    date_from: FromOpt = None,
    date_to: ToOpt = None,
    author: AuthorOpt = None,
    all_authors: AllAuthorsOpt = False,
) -> None:
    """Development summary grouped by area (default: this week)."""
    _summary(
        ctx,
        period or ("week" if not (date_from or date_to) else None),
        date_from,
        date_to,
        author,
        all_authors,
    )


@app.command()
@cli_errors
def standup(
    ctx: typer.Context,
    period: Annotated[str, typer.Argument(help="yesterday|week")] = "yesterday",
    plan: Annotated[
        list[str] | None, typer.Option("--plan", "-p", help="Today's plan item (repeatable)")
    ] = None,
    author: AuthorOpt = None,
    all_authors: AllAuthorsOpt = False,
) -> None:
    """Standup notes. Observed work only; add today's plan with --plan."""
    if period not in ("yesterday", "week"):
        raise typer.BadParameter("period must be 'yesterday' or 'week'")
    state = _state(ctx)
    llm = state.llm()
    with console.status("Preparing standup..."):
        result = summaries.build_standup(
            state.git, period=period, plan=plan, author=author, all_authors=all_authors, llm=llm
        )
    emit(summaries.render_standup(result))


# ------------------------------------------------------------------------ branches and PRs


@app.command()
@cli_errors
def compare(
    ctx: typer.Context, base: Annotated[str | None, typer.Argument(help="Base branch")] = None
) -> None:
    """Compare the current branch with a base branch (default from config: main)."""
    state = _state(ctx)
    cmp = pr_service.compare_branches(state.git, base or state.settings.base_branch)
    emit(pr_service.render_comparison(cmp))


@app.command()
@cli_errors
def pr(
    ctx: typer.Context, base: Annotated[str | None, typer.Argument(help="Base branch")] = None
) -> None:
    """Generate a PR description from the branch's commits and diff."""
    state = _state(ctx)
    llm = state.llm()
    with console.status("Writing PR description..."):
        result = pr_service.build_pr(state.git, base or state.settings.base_branch, llm)
    emit(pr_service.render_pr(result))


@app.command()
@cli_errors
def health(ctx: typer.Context) -> None:
    """Repository health and safety checks (no LLM)."""
    checks = health_service.run_health_checks(_state(ctx).git)
    emit(health_service.render_health(checks))
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(2)


# --------------------------------------------------------------------------------- agent


def _make_agent(state: AppState) -> Agent:
    llm = state.llm(required=True)
    git = state.git
    settings = state.settings
    registry = build_registry(
        ToolContext(
            git=git,
            approver=ConsoleApprover(),
            today=date.today(),
            base_branch=settings.base_branch,
            commit_limits=commit_service.MessageLimits.from_settings(settings),
        )
    )

    def on_event(event: AgentEvent) -> None:
        if event.kind == "tool_call":
            args = "" if event.detail in ("", "{}") else f" {mask_secrets(event.detail)[:120]}"
            console.print(f"[dim]  -> {event.name}{args}[/]", markup=True, highlight=False)
        elif event.kind == "tool_result" and state.verbose:
            console.print(f"[dim]     {mask_secrets(event.detail)[:200]}[/]")
        elif event.kind == "limit":
            console.print(f"[yellow]  tool-call limit reached ({event.detail})[/]")

    return Agent(
        llm,
        registry,
        agent_system_prompt(git.root.name, date.today()),
        max_tool_calls=settings.max_tool_calls,
        limits=settings.limits,
        on_event=on_event,
    )


def _print_answer(answer: str) -> None:
    console.print(Markdown(mask_secrets(answer)), soft_wrap=False)


@app.command()
@cli_errors
def ask(ctx: typer.Context, question: Annotated[str, typer.Argument(help="Your question")]):
    """Ask the agent anything about this repository."""
    agent = _make_agent(_state(ctx))
    with console.status("Thinking..."):
        answer = agent.ask(question)
    _print_answer(answer)


@app.command()
@cli_errors
def chat(ctx: typer.Context) -> None:
    """Interactive conversation with the agent ('exit' to quit, 'reset' to clear history)."""
    agent = _make_agent(_state(ctx))
    emit("GitWorklog chat. Type 'exit' to quit, 'reset' to clear the conversation.")
    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            emit("")
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if text.lower() == "reset":
            agent.reset()
            emit("Conversation cleared.")
            continue
        try:
            answer = agent.ask(text)
        except LLMError as exc:
            console.print(f"[red]LLM error:[/] {exc}")
            continue
        console.print("\n[bold]Agent:[/]")
        _print_answer(answer)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
