"""CLI commands for analytics"""

import click
import json
from kb.analytics import generate_report


def cmd_analytics(days: int, output: str):
    """
    Generate analytics report.

    Args:
        days: Number of days to analyze
        output: Output format (text or json)
    """
    report = generate_report(days=days)

    if output == 'json':
        click.echo(json.dumps(report, indent=2))
    else:
        # Text format
        click.echo("=" * 70)
        click.echo(f"Knowledge Base Analytics Report")
        click.echo(f"Generated: {report['generated_at']}")
        click.echo(f"Period: Last {days} days")
        click.echo("=" * 70)

        # Articles
        click.echo("\nArticles:")
        click.echo(f"  Total: {report['articles']['total']}")
        click.echo(f"  By Classification:")
        for cls, count in report['articles']['by_classification'].items():
            click.echo(f"    {cls}: {count}")

        # Compilation
        click.echo("\nCompilation:")
        click.echo(f"  Total Jobs: {report['compilation']['total']}")
        click.echo(f"  Success Rate: {report['compilation']['success_rate']}%")
        click.echo(f"  By Status:")
        for status, count in report['compilation']['by_status'].items():
            click.echo(f"    {status}: {count}")

        # Review Queue
        click.echo("\nReview Queue:")
        click.echo(f"  Pending: {report['review']['pending']}")
        click.echo(f"  Approved: {report['review']['approved']}")
        click.echo(f"  Rejected: {report['review']['rejected']}")

        # Costs
        click.echo("\nCosts:")
        click.echo(f"  Total: ${report['costs']['total_usd']:.2f}")
        if report['costs'].get('by_operation'):
            for op, cost in report['costs']['by_operation'].items():
                click.echo(f"    {op}: ${cost:.4f}")

        # Health
        click.echo("\nSystem Health:")
        click.echo(f"  Health Score: {report['health']['health_score']}/100")
        click.echo(f"  Integrity Issues: {report['health']['integrity']['total_issues']}")
        click.echo(f"    Errors: {report['health']['integrity']['errors']}")
        click.echo(f"    Warnings: {report['health']['integrity']['warnings']}")
        click.echo(f"    Fixable: {report['health']['integrity']['fixable']}")
        click.echo(f"  Links: {report['health']['links']['total']}")

        click.echo("=" * 70)
