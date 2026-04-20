"""CLI commands for job queue management"""

import click
import json
from kb.jobs import list_jobs, get_job_status, cancel_job
from kb.errors import PermanentError


@click.group()
def jobs():
    """Manage background compilation jobs"""
    pass


@jobs.command('list')
@click.option('--status', type=click.Choice(['pending', 'running', 'completed', 'failed', 'awaiting_review']),
              help='Filter by status')
@click.option('--limit', default=20, type=int, help='Maximum number of jobs to show')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def jobs_list(status: str, limit: int, output: str):
    """
    List jobs with optional status filter.

    Example:
        kb jobs list --status pending
    """
    job_list = list_jobs(status=status, limit=limit)

    if output == 'json':
        click.echo(json.dumps(job_list, indent=2))
    else:
        if not job_list:
            click.echo("No jobs found")
            return

        filter_msg = f" ({status})" if status else ""
        click.echo(f"Jobs{filter_msg} ({len(job_list)}):\n")

        for job in job_list:
            status_icon = {
                'pending': '⏸️',
                'running': '▶️',
                'completed': '✓',
                'failed': '✗',
                'awaiting_review': '⏳'
            }.get(job['status'], '•')

            click.echo(f"{status_icon} {job['id']}")
            click.echo(f"  Type: {job['type']}")
            click.echo(f"  Status: {job['status']}")
            click.echo(f"  Created: {job['created_at']}")

            if job['started_at']:
                click.echo(f"  Started: {job['started_at']}")

            if job['completed_at']:
                click.echo(f"  Completed: {job['completed_at']}")

            if job['error']:
                click.echo(f"  Error: {job['error']}")

            click.echo("")


@jobs.command('status')
@click.argument('job_id')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def jobs_status(job_id: str, output: str):
    """
    Show detailed status for a specific job.

    Example:
        kb jobs status abc123
    """
    job = get_job_status(job_id)

    if not job:
        click.echo(f"✗ Job not found: {job_id}", err=True)
        raise click.Abort()

    if output == 'json':
        click.echo(json.dumps(job, indent=2))
    else:
        status_icon = {
            'pending': '⏸️',
            'running': '▶️',
            'completed': '✓',
            'failed': '✗',
            'awaiting_review': '⏳'
        }.get(job['status'], '•')

        click.echo(f"Job {job_id} {status_icon}")
        click.echo(f"  Type: {job['type']}")
        click.echo(f"  Status: {job['status']}")
        click.echo(f"  Created: {job['created_at']}")

        if job['started_at']:
            click.echo(f"  Started: {job['started_at']}")

        if job['completed_at']:
            click.echo(f"  Completed: {job['completed_at']}")

        if job['retry_count'] > 0:
            click.echo(f"  Retries: {job['retry_count']}")

        if job['review_id']:
            click.echo(f"  Review ID: {job['review_id']}")

        if job['params']:
            click.echo(f"\n  Parameters:")
            for key, value in job['params'].items():
                click.echo(f"    {key}: {value}")

        if job['result']:
            click.echo(f"\n  Result:")
            for key, value in job['result'].items():
                click.echo(f"    {key}: {value}")

        if job['error']:
            click.echo(f"\n  Error: {job['error']}")


@jobs.command('cancel')
@click.argument('job_id')
def jobs_cancel(job_id: str):
    """
    Cancel a running or pending job.

    Example:
        kb jobs cancel abc123
    """
    success = cancel_job(job_id)

    if success:
        click.echo(f"✓ Cancelled job: {job_id}")
    else:
        click.echo(f"✗ Could not cancel job: {job_id}", err=True)
        click.echo("  Job may not exist or is already completed", err=True)
        raise click.Abort()


def register(cli):
    """Register jobs commands with main CLI"""
    cli.add_command(jobs)
