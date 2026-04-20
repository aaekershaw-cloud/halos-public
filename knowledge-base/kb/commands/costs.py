"""CLI commands for cost tracking and budget management"""

import click
import json
from datetime import datetime
from kb.costs import (
    get_daily_spending,
    get_cost_summary,
    get_budget_status
)


@click.group()
def costs():
    """Track LLM API costs and budgets"""
    pass


@costs.command('today')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def costs_today(output: str):
    """
    Show today's spending.

    Example:
        kb costs today
    """
    spending = get_daily_spending()

    if output == 'json':
        click.echo(json.dumps({'today_spending_usd': spending}, indent=2))
    else:
        click.echo(f"Today's Spending: ${spending:.4f}")


@costs.command('summary')
@click.option('--days', default=30, type=int, help='Number of days to summarize')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def costs_summary(days: int, output: str):
    """
    Show cost summary for last N days.

    Example:
        kb costs summary --days 7
    """
    summary = get_cost_summary(days=days)

    if output == 'json':
        click.echo(json.dumps(summary, indent=2))
    else:
        click.echo(f"Cost Summary (Last {summary['days']} Days):")
        click.echo(f"  Total: ${summary['total_usd']:.4f}\n")

        if summary['by_operation']:
            click.echo("  By Operation:")
            for operation, cost in summary['by_operation'].items():
                click.echo(f"    {operation}: ${cost:.4f}")
            click.echo("")

        if summary['by_model']:
            click.echo("  By Model:")
            for model, cost in summary['by_model'].items():
                # Shorten model name for display
                model_short = model.replace('claude-', '').replace('-20241022', '').replace('-20250514', '')
                click.echo(f"    {model_short}: ${cost:.4f}")
            click.echo("")

        if summary['by_day']:
            click.echo("  Daily Breakdown:")
            for day_data in summary['by_day'][:10]:  # Show last 10 days
                click.echo(f"    {day_data['day']}: ${day_data['total']:.4f}")


@costs.command('budget')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def costs_budget(output: str):
    """
    Show current budget status.

    Example:
        kb costs budget
    """
    status = get_budget_status()

    if output == 'json':
        click.echo(json.dumps(status, indent=2))
    else:
        click.echo("Budget Status:")
        click.echo(f"  Daily Limit: ${status['daily_budget_usd']:.2f}")
        click.echo(f"  Today's Spending: ${status['today_spending_usd']:.4f}")
        click.echo(f"  Remaining: ${status['remaining_usd']:.4f}")
        click.echo(f"  Used: {status['used_percentage']:.1f}%")

        # Visual progress bar
        used_pct = min(100, status['used_percentage'])
        bar_width = 40
        filled = int(bar_width * used_pct / 100)
        bar = '█' * filled + '░' * (bar_width - filled)

        # Color coding (if terminal supports it)
        if used_pct < 50:
            color = 'green'
        elif used_pct < 80:
            color = 'yellow'
        else:
            color = 'red'

        click.echo(f"\n  [{bar}] {used_pct:.1f}%")

        # Warning if approaching limit
        if used_pct >= 80:
            click.echo(f"\n  ⚠️  Warning: Approaching daily budget limit")


def register(cli):
    """Register costs commands with main CLI"""
    cli.add_command(costs)
