"""`kb memory` CLI subcommands — agent memory CRUD + rendering."""

import json
import sys

import click

from kb.memory import MemoryStore


def _store() -> MemoryStore:
    return MemoryStore()


def _print_entries_text(entries):
    if not entries:
        click.echo("(no entries)")
        return
    # Group by section for display
    by_section = {}
    for e in entries:
        by_section.setdefault(e["section"], []).append(e)
    for section in sorted(by_section.keys()):
        click.echo(f"\n[{section}]")
        for e in by_section[section]:
            tags = f" ({e['tags']})" if e.get("tags") else ""
            click.echo(f"  #{e['id']}  {e['content']}{tags}")


def _print_entries_json(entries):
    click.echo(json.dumps(entries, indent=2, default=str))


@click.group()
def memory():
    """Manage per-agent memory stored in the knowledge base."""
    pass


@memory.command("show")
@click.option("--agent", required=True, help="Agent name (e.g. beta, alpha)")
@click.option("--section", default=None, help="Filter by section")
@click.option("--limit", type=int, default=None, help="Max entries to return")
@click.option("--format", "output_format",
              type=click.Choice(["text", "json"]), default="text")
def show(agent, section, limit, output_format):
    """List memory entries for an agent."""
    entries = _store().get_entries(agent, section=section, limit=limit)
    if output_format == "json":
        _print_entries_json(entries)
    else:
        _print_entries_text(entries)


@memory.command("add")
@click.option("--agent", required=True, help="Agent name")
@click.option("--section", required=True, help="Section name (e.g. short_term)")
@click.option("--content", required=True, help="Entry content")
@click.option("--tags", default="", help="Comma-separated tags")
def add(agent, section, content, tags):
    """Add a memory entry."""
    eid = _store().add_entry(agent, section, content, tags=tags)
    click.echo(f"Added entry #{eid}")


@memory.command("update")
@click.option("--agent", required=True, help="Agent name")
@click.option("--id", "entry_id", required=True, type=int, help="Entry id")
@click.option("--content", default=None, help="New content")
@click.option("--tags", default=None, help="New tags (comma-separated)")
def update(agent, entry_id, content, tags):
    """Update a memory entry by id."""
    _ = agent  # kept for interface consistency; entries are id-addressable
    if content is None and tags is None:
        raise click.UsageError("Provide --content and/or --tags to update.")
    if _store().update_entry(entry_id, content=content, tags=tags):
        click.echo(f"Updated entry #{entry_id}")
    else:
        click.echo(f"No entry found with id {entry_id}", err=True)
        sys.exit(1)


@memory.command("delete")
@click.option("--agent", required=True, help="Agent name")
@click.option("--id", "entry_id", required=True, type=int, help="Entry id")
def delete(agent, entry_id):
    """Delete a memory entry by id."""
    _ = agent
    if _store().delete_entry(entry_id):
        click.echo(f"Deleted entry #{entry_id}")
    else:
        click.echo(f"No entry found with id {entry_id}", err=True)
        sys.exit(1)


@memory.command("search")
@click.option("--agent", required=True, help="Agent name")
@click.option("--query", required=True, help="Substring to search for")
@click.option("--limit", type=int, default=None, help="Max results")
@click.option("--format", "output_format",
              type=click.Choice(["text", "json"]), default="text")
def search(agent, query, limit, output_format):
    """Search an agent's memory (case-insensitive substring)."""
    hits = _store().search(agent, query, limit=limit)
    if output_format == "json":
        _print_entries_json(hits)
    else:
        _print_entries_text(hits)


@memory.command("promote")
@click.option("--agent", required=True, help="Agent name")
@click.option("--id", "entry_id", required=True, type=int, help="Entry id")
@click.option("--target-section", default="long_term",
              help="Section to promote into (default: long_term)")
def promote(agent, entry_id, target_section):
    """Promote a short_term entry to long_term (or other target section)."""
    _ = agent
    if _store().promote_to_long_term(entry_id, target_section=target_section):
        click.echo(f"Promoted entry #{entry_id} -> {target_section}")
    else:
        click.echo(f"No entry found with id {entry_id}", err=True)
        sys.exit(1)


@memory.command("clear-short-term")
@click.option("--agent", required=True, help="Agent name")
@click.option("--confirm", is_flag=True, help="Required to actually delete")
def clear_short_term(agent, confirm):
    """Delete all short_term entries for an agent."""
    if not confirm:
        click.echo("Refusing to clear without --confirm", err=True)
        sys.exit(1)
    n = _store().clear_section(agent, "short_term")
    click.echo(f"Cleared {n} short_term entries for {agent}")


@memory.command("render")
@click.option("--agent", required=True, help="Agent name")
@click.option("--max-chars", type=int, default=4000,
              help="Cap the rendered output length")
def render(agent, max_chars):
    """Render an agent's memory as Markdown for system-prompt injection."""
    click.echo(_store().render_for_prompt(agent, max_chars=max_chars))


@memory.command("consolidate")
@click.option("--agent", required=True, help="Agent name (e.g. alpha, beta)")
@click.option("--limit", type=int, default=50,
              help="Max short_term entries to review per run")
@click.option("--dry-run", is_flag=True,
              help="Run the LLM but skip all DB mutations")
@click.option("--model", default="haiku",
              type=click.Choice(["haiku", "sonnet", "opus"]),
              help="LLM model to use (default: haiku)")
def consolidate(agent, limit, dry_run, model):
    """Promote old short-term entries into durable memory sections.

    Fetches the oldest entries (skipping anything younger than 48 hours),
    asks an LLM to extract lessons/facts/decisions, writes them to the
    appropriate sections, and hard-deletes the originals.
    """
    from kb.consolidation import consolidate_short_term

    if dry_run:
        click.echo(f"[dry-run] consolidating short_term for {agent} "
                   f"(limit={limit}, model={model})")

    stats = consolidate_short_term(
        agent=agent,
        limit=limit,
        dry_run=dry_run,
        model=model,
    )

    click.echo(f"Entries reviewed : {stats['entries_reviewed']}")
    click.echo(f"Lessons added    : {stats['learned_added']}")
    click.echo(f"Facts added      : {stats['long_term_added']}")
    click.echo(f"Decisions added  : {stats['decisions_added']}")
    click.echo(f"Entries deleted  : {stats['entries_deleted']}")
    if dry_run:
        click.echo("(dry-run: no DB changes written)")


@memory.command("compress")
@click.option("--agent", required=True, help="Agent name (e.g. alpha, beta)")
@click.option("--dry-run", is_flag=True,
              help="Run the LLM but skip all DB mutations")
@click.option("--model", default="haiku",
              type=click.Choice(["haiku", "sonnet", "opus"]),
              help="LLM model to use (default: haiku)")
def compress(agent, dry_run, model):
    """Compress all short-term entries into a single session summary.

    Asks an LLM for a 1-2 sentence summary, writes it to the episodic
    section, and clears short_term.
    """
    from kb.consolidation import compress_session

    if dry_run:
        click.echo(f"[dry-run] compressing short_term for {agent} "
                   f"(model={model})")

    stats = compress_session(
        agent=agent,
        dry_run=dry_run,
        model=model,
    )

    click.echo(f"Entries compressed : {stats['entries_compressed']}")
    click.echo(f"Target section     : {stats['target_section']}")
    if stats["summary_text"]:
        click.echo(f"Summary            : {stats['summary_text']}")
    if dry_run:
        click.echo("(dry-run: no DB changes written)")
