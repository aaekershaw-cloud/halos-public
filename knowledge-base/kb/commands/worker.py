"""CLI commands for background worker"""

import click
from kb.worker import start_worker, stop_worker, get_worker_status
import json


@click.group()
def worker():
    """Manage background worker process"""
    pass


@worker.command('start')
@click.option('--daemon', is_flag=True, help='Run as background daemon')
def worker_start(daemon: bool):
    """
    Start the background worker.

    Example:
        kb worker start --daemon
    """
    status = get_worker_status()

    if status:
        click.echo(f"✗ Worker already running (PID: {status['pid']})", err=True)
        raise click.Abort()

    if daemon:
        click.echo("Starting worker as daemon...")
        # _daemonize() handles double-fork and writes PID file
        start_worker(daemon=True)
        # If we reach here, we're the original parent (shouldn't happen
        # as _daemonize exits the parent), but just in case:
        import os
        kb_dir = os.environ.get('KB_DIR', os.path.expanduser('~/.kb'))
        pid_file = os.path.join(kb_dir, 'worker.pid')
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as f:
                click.echo(f"✓ Worker started (PID: {f.read().strip()})")
    else:
        click.echo("Starting worker (Ctrl+C to stop)...")
        start_worker(daemon=False)


@worker.command('stop')
def worker_stop():
    """
    Stop the background worker.

    Example:
        kb worker stop
    """
    status = get_worker_status()

    if not status:
        click.echo("✗ Worker not running", err=True)
        raise click.Abort()

    success = stop_worker()

    if success:
        click.echo("✓ Worker stopped")
    else:
        click.echo("✗ Failed to stop worker", err=True)
        raise click.Abort()


@worker.command('status')
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def worker_status(output: str):
    """
    Show worker status.

    Example:
        kb worker status
    """
    status = get_worker_status()

    if output == 'json':
        click.echo(json.dumps(status or {'running': False}, indent=2))
    else:
        if status:
            click.echo(f"Worker Status: Running")
            click.echo(f"  PID: {status['pid']}")
        else:
            click.echo("Worker Status: Not running")


def register(cli):
    """Register worker commands with main CLI"""
    cli.add_command(worker)
