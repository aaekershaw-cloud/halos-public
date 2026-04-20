"""CLI commands for review queue management"""

import click
import json
from kb.review import (
    list_pending_reviews,
    get_review_details,
    approve_review,
    reject_review,
    get_review_stats,
    auto_approve_all,
    display_review
)
from kb.errors import PermanentError


@click.group()
def review():
    """Manage human-in-the-loop review queue"""
    pass


@review.command('list')
@click.option('--limit', default=20, type=int, help='Maximum number of reviews to show')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def review_list(limit: int, output: str):
    """
    List pending reviews.

    Example:
        kb review list
    """
    reviews = list_pending_reviews(limit=limit)

    if output == 'json':
        click.echo(json.dumps(reviews, indent=2))
    else:
        if not reviews:
            click.echo("No pending reviews")
            return

        click.echo(f"Pending Reviews ({len(reviews)}):\n")

        for review in reviews:
            click.echo(f"ID: {review['id']}")
            click.echo(f"  Summary: {review['summary']}")
            click.echo(f"  Source: {review['source_file']}")
            click.echo(f"  Created: {review['created_at']}")
            if review['job_id']:
                click.echo(f"  Job ID: {review['job_id']}")
            click.echo("")


@review.command('show')
@click.argument('review_id')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def review_show(review_id: str, output: str):
    """
    Show full details for a review.

    Example:
        kb review show 123
    """
    review = get_review_details(review_id)

    if not review:
        click.echo(f"✗ Review not found: {review_id}", err=True)
        raise click.Abort()

    if output == 'json':
        click.echo(json.dumps(review, indent=2))
    else:
        click.echo(display_review(review_id))


@review.command('approve')
@click.argument('review_id')
@click.option('--notes', help='Reviewer notes')
def review_approve(review_id: str, notes: str):
    """
    Approve a review and apply changes.

    Example:
        kb review approve 123 --notes "Looks good"
    """
    try:
        article_id = approve_review(review_id, reviewer_notes=notes)

        click.echo(f"✓ Review approved")
        click.echo(f"  Review ID: {review_id}")
        click.echo(f"  Article ID: {article_id}")
        if notes:
            click.echo(f"  Notes: {notes}")

    except PermanentError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


@review.command('reject')
@click.argument('review_id')
@click.option('--reason', required=True, help='Reason for rejection')
def review_reject(review_id: str, reason: str):
    """
    Reject a review.

    Example:
        kb review reject 123 --reason "Incorrect categorization"
    """
    try:
        success = reject_review(review_id, reason=reason)

        if success:
            click.echo(f"✓ Review rejected")
            click.echo(f"  Review ID: {review_id}")
            click.echo(f"  Reason: {reason}")
        else:
            click.echo(f"✗ Review not found: {review_id}", err=True)
            raise click.Abort()

    except PermanentError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


@review.command('stats')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def review_stats(output: str):
    """
    Show review queue statistics.

    Example:
        kb review stats
    """
    stats = get_review_stats()

    if output == 'json':
        click.echo(json.dumps(stats, indent=2))
    else:
        click.echo("Review Queue Statistics:")
        click.echo(f"  Pending: {stats['pending']}")
        click.echo(f"  Approved: {stats['approved']}")
        click.echo(f"  Rejected: {stats['rejected']}")

        if stats['oldest_pending']:
            click.echo(f"  Oldest pending: {stats['oldest_pending']}")


@review.command('approve-all')
@click.option('--limit', default=100, type=int, help='Maximum number to approve')
@click.confirmation_option(prompt='Auto-approve all pending reviews?')
def review_approve_all(limit: int):
    """
    Auto-approve all pending reviews (for testing/batch operations).

    Example:
        kb review approve-all --limit 10
    """
    result = auto_approve_all(limit=limit)

    click.echo(f"Auto-Approve Summary:")
    click.echo(f"  ✓ Approved: {result['approved']}")
    click.echo(f"  ✗ Failed: {result['failed']}")

    if result['article_ids']:
        click.echo(f"\nCreated articles:")
        for article_id in result['article_ids']:
            click.echo(f"  - {article_id}")


def register(cli):
    """Register review commands with main CLI"""
    cli.add_command(review)
