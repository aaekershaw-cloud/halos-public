"""CLI commands for compilation operations"""

import click
import json
from kb.compile import compile_raw_file, compile_all_pending
from kb.jobs import create_job, get_job_status
from kb.errors import PermanentError, TransientError


@click.group()
def compile():
    """Compile raw files into wiki articles using LLM"""
    pass


@compile.command('file')
@click.argument('raw_file_id')
@click.option('--model', default='sonnet', type=click.Choice(['haiku', 'sonnet', 'opus']),
              help='LLM model to use')
@click.option('--auto-approve', is_flag=True,
              help='Auto-approve without review queue')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def compile_file(raw_file_id: str, model: str, auto_approve: bool, output: str):
    """
    Compile a specific raw file into a wiki article.

    Example:
        kb compile file abc123 --model sonnet
    """
    try:
        result = compile_raw_file(
            raw_file_id=raw_file_id,
            model=model,
            auto_approve=auto_approve
        )

        if output == 'json':
            click.echo(json.dumps(result, indent=2))
        else:
            if result['status'] == 'applied':
                click.echo(f"✓ Compiled successfully")
                click.echo(f"  Article ID: {result['article_id']}")
                click.echo(f"  Cost: ${result['cost_usd']:.4f}")
            elif result['status'] == 'awaiting_review':
                click.echo(f"⏳ Awaiting review")
                click.echo(f"  Review ID: {result['review_id']}")
                click.echo(f"  Cost: ${result['cost_usd']:.4f}")
                click.echo(f"\nUse: kb review show {result['review_id']}")

    except PermanentError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except TransientError as e:
        click.echo(f"✗ Transient error: {e}", err=True)
        click.echo("  Retry the operation", err=True)
        raise click.Abort()


@compile.command('all')
@click.option('--model', default='sonnet', type=click.Choice(['haiku', 'sonnet', 'opus']),
              help='LLM model to use')
@click.option('--limit', default=10, type=int,
              help='Maximum number of files to compile')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def compile_all(model: str, limit: int, output: str):
    """
    Compile all pending raw files.

    Example:
        kb compile all --limit 5
    """
    try:
        result = compile_all_pending(model=model, limit=limit)

        if output == 'json':
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Compilation Summary:")
            click.echo(f"  ✓ Applied: {result['compiled']}")
            click.echo(f"  ⏳ Awaiting review: {result['awaiting_review']}")
            click.echo(f"  ✗ Failed: {result['failed']}")
            click.echo(f"  💰 Total cost: ${result['total_cost_usd']:.4f}")

            if result['awaiting_review'] > 0:
                click.echo(f"\nUse: kb review list")

    except PermanentError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


@compile.command('queue')
@click.argument('raw_file_id')
@click.option('--model', default='sonnet', type=click.Choice(['haiku', 'sonnet', 'opus']),
              help='LLM model to use')
@click.option('--auto-approve', is_flag=True,
              help='Auto-approve without review queue')
def compile_queue(raw_file_id: str, model: str, auto_approve: bool):
    """
    Queue a raw file for background compilation.

    Example:
        kb compile queue abc123
    """
    try:
        job_id = create_job('compile', {
            'raw_file_id': raw_file_id,
            'model': model,
            'auto_approve': auto_approve
        })

        click.echo(f"✓ Queued for compilation")
        click.echo(f"  Job ID: {job_id}")
        click.echo(f"\nUse: kb jobs status {job_id}")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


def register(cli):
    """Register compile commands with main CLI"""
    cli.add_command(compile)
