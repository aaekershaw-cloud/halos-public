"""CLI commands for retention policies"""

import click
import json
from kb.retention import (
    apply_retention_policies,
    cleanup_expired_soft_deletes,
    recover_article,
    archive_article,
    soft_delete_article
)


@click.group()
def retention():
    """Manage data retention policies"""
    pass


@retention.command('run')
@click.option('--dry-run', is_flag=True, help='Show what would be done without applying')
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def retention_run(dry_run: bool, output: str):
    """
    Apply retention policies to all articles.

    Example:
        kb retention run --dry-run
        kb retention run  # Actually apply
    """
    results = apply_retention_policies(dry_run=dry_run)

    if output == 'json':
        click.echo(json.dumps(results, indent=2))
    else:
        mode = "Dry Run" if dry_run else "Applied"
        click.echo(f"Retention Policy {mode}")
        click.echo("=" * 60)
        click.echo(f"Articles evaluated: {results['total_articles']}")
        click.echo()
        click.echo("Actions:")
        click.echo(f"  Archive: {results['actions']['archive']}")
        click.echo(f"  Soft Delete: {results['actions']['soft_delete']}")
        click.echo(f"  Hard Delete: {results['actions']['hard_delete']}")
        click.echo(f"  Keep: {results['actions']['keep']}")

        if not dry_run:
            click.echo()
            click.echo(f"Success: {results['success']}")
            click.echo(f"Failed: {results['failed']}")


@retention.command('cleanup')
@click.option('--grace-days', type=int, default=30,
              help='Grace period in days')
def retention_cleanup(grace_days: int):
    """
    Permanently delete soft-deleted articles past grace period.

    Example:
        kb retention cleanup --grace-days 30
    """
    deleted_count = cleanup_expired_soft_deletes(grace_period_days=grace_days)

    if deleted_count > 0:
        click.echo(f"✓ Permanently deleted {deleted_count} article(s) past grace period")
    else:
        click.echo("✓ No articles to delete")


@retention.command('recover')
@click.argument('article_id')
def retention_recover(article_id: str):
    """
    Recover a soft-deleted article.

    Example:
        kb retention recover <article-id>
    """
    success = recover_article(article_id)

    if success:
        click.echo(f"✓ Recovered article: {article_id}")
    else:
        click.echo(f"✗ Failed to recover article: {article_id}", err=True)
        raise click.Abort()


@retention.command('archive')
@click.argument('article_id')
def retention_archive_cmd(article_id: str):
    """
    Manually archive an article.

    Example:
        kb retention archive <article-id>
    """
    success = archive_article(article_id)

    if success:
        click.echo(f"✓ Archived article: {article_id}")
    else:
        click.echo(f"✗ Failed to archive article: {article_id}", err=True)
        raise click.Abort()


@retention.command('soft-delete')
@click.argument('article_id')
@click.option('--grace-days', type=int, default=30,
              help='Days before permanent deletion')
def retention_soft_delete_cmd(article_id: str, grace_days: int):
    """
    Soft delete an article (with recovery period).

    Example:
        kb retention soft-delete <article-id> --grace-days 30
    """
    success = soft_delete_article(article_id, grace_period_days=grace_days)

    if success:
        click.echo(f"✓ Soft deleted article: {article_id}")
        click.echo(f"  Can be recovered within {grace_days} days")
    else:
        click.echo(f"✗ Failed to soft delete article: {article_id}", err=True)
        raise click.Abort()


def register(cli):
    """Register retention commands with main CLI"""
    cli.add_command(retention)
