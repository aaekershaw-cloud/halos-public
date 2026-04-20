"""Command-line interface for Knowledge Base System"""

import click
import logging
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)

logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version='1.0.0')
def main():
    """Knowledge Base System - LLM-powered knowledge compilation"""
    pass


@main.command()
def init():
    """Initialize knowledge base (create directory structure and database)"""
    from kb.commands.init import cmd_init
    cmd_init()


@main.command()
def migrate():
    """Run pending database migrations"""
    from kb.commands.migrate import cmd_migrate
    cmd_migrate()


@main.group()
def ingest():
    """Ingest source materials into knowledge base"""
    pass


@ingest.command('file')
@click.argument('filepath', type=click.Path(exists=True))
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              default='internal', help='Classification level')
@click.option('--force', is_flag=True, help='Force ingest even if PII detected')
@click.option('--direct', is_flag=True,
              help='Write directly to wiki and index in FTS (immediately searchable, skips compile)')
@click.option('--agent', help='Agent scope (e.g. alpha, beta). Omit for shared articles.')
def ingest_file(filepath, classification, force, direct, agent):
    """Ingest a local file"""
    from kb.commands.ingest import cmd_ingest_file
    cmd_ingest_file(filepath, classification, force, direct=direct, agent=agent)


@ingest.command('url')
@click.argument('url')
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              default='internal', help='Classification level')
def ingest_url(url, classification):
    """Ingest content from URL"""
    from kb.commands.ingest import cmd_ingest_url
    cmd_ingest_url(url, classification)


@ingest.command('pdf')
@click.argument('filepath', type=click.Path(exists=True))
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              default='internal', help='Classification level')
@click.option('--no-marker', is_flag=True,
              help='Disable marker-pdf, use basic pypdf extraction')
@click.option('--max-pages', type=int, default=None,
              help='Maximum pages to extract (marker-pdf only)')
def ingest_pdf(filepath, classification, no_marker, max_pages):
    """Extract and ingest PDF file (uses marker-pdf if available for high quality)"""
    from kb.commands.ingest import cmd_ingest_pdf
    cmd_ingest_pdf(filepath, classification, use_marker=not no_marker, max_pages=max_pages)


@ingest.command('dir')
@click.argument('dirpath', type=click.Path(exists=True, file_okay=False))
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              default='internal', help='Classification level')
@click.option('--direct/--raw', default=True,
              help='Direct: index immediately (default). Raw: queue for compile.')
@click.option('--no-recursive', is_flag=True, help='Only process top-level files')
@click.option('--ext', multiple=True, default=['.md'],
              help='File extensions to include (default: .md). Repeatable: --ext .md --ext .txt')
@click.option('--auto', is_flag=True,
              help='Detect file types by content (magika); ignore --ext. '
                   'Includes any text or pdf file regardless of extension.')
@click.option('--dry-run', is_flag=True, help='List files without ingesting')
def ingest_dir(dirpath, classification, direct, no_recursive, ext, auto, dry_run):
    """Batch ingest all matching files in a directory"""
    from kb.commands.ingest import cmd_ingest_dir
    cmd_ingest_dir(
        dirpath,
        classification,
        direct=direct,
        recursive=not no_recursive,
        ext=tuple(ext),
        dry_run=dry_run,
        auto=auto,
    )


@ingest.command('repo')
@click.argument('repo_url')
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              default='internal', help='Classification level')
def ingest_repo(repo_url, classification):
    """Clone and index git repository"""
    from kb.commands.ingest import cmd_ingest_repo
    cmd_ingest_repo(repo_url, classification)


@main.command()
@click.argument('query')
@click.option('--tag', help='Filter by tag')
@click.option('--classification', type=click.Choice(['public', 'internal', 'confidential']),
              help='Filter by classification')
@click.option('--since', help='Filter by date (YYYY-MM-DD)')
@click.option('--format', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
@click.option('--agent', help='Filter by agent scope')
def search(query, tag, classification, since, format, agent):
    """Search wiki articles"""
    from kb.commands.search import cmd_search
    cmd_search(query, tag, classification, since, format, agent=agent)


@main.command()
@click.option('--agent', help='Agent name (writes to agent session dir)')
@click.option('--output', help='Override output file path')
@click.option('--all', 'all_agents', is_flag=True, help='Generate indexes for all agents')
def index(agent, output, all_agents):
    """Generate abbreviated KB index for agent context loading"""
    from kb.commands.index import cmd_index
    cmd_index(agent, output, all_agents)


# Compile commands (Phase 2)
from kb.commands.compile import compile as compile_group
main.add_command(compile_group)


# Query command (Phase 3)
from kb.commands.query import query
main.add_command(query)


# Lint command (Phase 3)
from kb.commands.lint import lint
main.add_command(lint)


# Review commands (Phase 2)
from kb.commands.review import review as review_group
main.add_command(review_group)


# Jobs commands (Phase 2)
from kb.commands.jobs import jobs as jobs_group
main.add_command(jobs_group)


# Costs commands (Phase 2)
from kb.commands.costs import costs as costs_group
main.add_command(costs_group)


# PII commands (Phase 4)
from kb.commands.pii import pii as pii_group
main.add_command(pii_group)


# Retention commands (Phase 4)
from kb.commands.retention import retention as retention_group
main.add_command(retention_group)


# Worker commands (Phase 4)
from kb.commands.worker import worker as worker_group
main.add_command(worker_group)


# Memory commands (agent memory store)
from kb.commands.memory import memory as memory_group
main.add_command(memory_group)


# Confidence commands (confidence scoring and supersession)
from kb.commands.confidence import confidence as confidence_group
main.add_command(confidence_group)


# Embed commands (semantic vector embeddings)
@main.group()
def embed():
    """Manage semantic vector embeddings for articles"""
    pass


@embed.command('article')
@click.argument('article_id')
def embed_article(article_id):
    """Embed a single article by ID"""
    from kb.commands.embed import cmd_embed_article
    cmd_embed_article(article_id)


@embed.command('all')
@click.option('--batch-size', type=int, default=50, show_default=True,
              help='Number of articles per encode batch')
def embed_all(batch_size):
    """Batch embed all articles"""
    from kb.commands.embed import cmd_embed_all
    cmd_embed_all(batch_size=batch_size)


@embed.command('search')
@click.argument('query')
@click.option('--limit', type=int, default=20, show_default=True,
              help='Maximum number of results')
@click.option('--format', 'output_format',
              type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def embed_search(query, limit, output_format):
    """Semantic vector search across embedded articles"""
    from kb.commands.embed import cmd_embed_search
    cmd_embed_search(query, limit=limit, output_format=output_format)


# Analytics command (Phase 4)
@main.command()
@click.option('--days', type=int, default=7, help='Number of days to analyze')
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def analytics(days, output):
    """Generate analytics report"""
    from kb.commands.analytics import cmd_analytics
    cmd_analytics(days, output)


@main.command()
@click.option('--max-backups', type=int, default=10, help='Max backups to keep')
def backup(max_backups):
    """Create database backup"""
    from kb.commands.backup import cmd_backup
    cmd_backup(max_backups)


@main.command()
@click.argument('backup_path', type=click.Path(exists=True))
@click.option('--dry-run', is_flag=True, help='Validate without applying')
def restore(backup_path, dry_run):
    """Restore from backup"""
    from kb.commands.backup import cmd_restore
    cmd_restore(backup_path, dry_run)


if __name__ == '__main__':
    main()
