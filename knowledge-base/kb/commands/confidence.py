"""CLI commands for confidence scoring and article supersession"""

import click
import json

from kb.scoring import update_confidence, confirm_article, supersede_article, calculate_confidence
from kb.errors import PermanentError


@click.group()
def confidence():
    """Manage article confidence scores and supersession"""
    pass


@confidence.command("update")
@click.argument("article_id")
@click.option("--score", type=float, default=None,
              help="Manual confidence score (0.0-1.0)")
@click.option("--auto", is_flag=True, default=False,
              help="Auto-calculate score from article metadata")
def confidence_update(article_id: str, score: float, auto: bool):
    """
    Update confidence score for an article.

    Examples:

        kb confidence update abc123 --score 0.8

        kb confidence update abc123 --auto
    """
    if score is None and not auto:
        raise click.UsageError(
            "Provide either --score <value> or --auto"
        )

    try:
        final_score = update_confidence(article_id, score=score, auto=auto)
        click.echo(f"Updated confidence for {article_id}: {final_score:.4f}")
    except PermanentError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@confidence.command("confirm")
@click.argument("article_id")
@click.option("--reviewer", default="human",
              help="Reviewer identity (default: human)")
def confidence_confirm(article_id: str, reviewer: str):
    """
    Confirm an article as accurate, boosting its confidence score.

    Sets last_confirmed_at to now and bumps confidence to at least 0.7.

    Examples:

        kb confidence confirm abc123

        kb confidence confirm abc123 --reviewer alice
    """
    try:
        new_score = confirm_article(article_id, reviewer=reviewer)
        click.echo(
            f"Article {article_id} confirmed by '{reviewer}'. "
            f"Confidence: {new_score:.4f}"
        )
    except PermanentError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@confidence.command("supersede")
@click.argument("old_id")
@click.argument("new_id")
def confidence_supersede(old_id: str, new_id: str):
    """
    Mark OLD_ID as superseded by NEW_ID.

    Sets superseded_by on the old article and creates a 'supersedes'
    link from the new article to the old one.

    Examples:

        kb confidence supersede old-article-id new-article-id
    """
    try:
        supersede_article(old_id, new_id)
        click.echo(f"Article {old_id} is now superseded by {new_id}.")
    except PermanentError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@confidence.command("show")
@click.argument("article_id")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]),
              default="text", help="Output format")
def confidence_show(article_id: str, fmt: str):
    """
    Show the current calculated confidence score for an article.

    Examples:

        kb confidence show abc123

        kb confidence show abc123 --format json
    """
    try:
        score = calculate_confidence(article_id)
        if fmt == "json":
            click.echo(json.dumps({"article_id": article_id, "confidence": score}))
        else:
            click.echo(f"Confidence for {article_id}: {score:.4f}")
    except PermanentError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
