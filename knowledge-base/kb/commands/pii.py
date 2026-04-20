"""CLI commands for PII scanning"""

import click
import json
from kb.pii import scan_file, scan_article, scan_all_articles, get_pii_summary, PIIType


@click.group()
def pii():
    """Scan for PII (Personally Identifiable Information)"""
    pass


@pii.command('scan-file')
@click.argument('filepath', type=click.Path(exists=True))
@click.option('--confidence', type=float, default=0.7,
              help='Minimum confidence threshold (0.0-1.0)')
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def pii_scan_file(filepath: str, confidence: float, output: str):
    """
    Scan a file for PII.

    Example:
        kb pii scan-file /path/to/file.md
    """
    result = scan_file(filepath)

    if output == 'json':
        # Convert PIIMatch objects to dicts
        matches_dict = [
            {
                'type': m.pii_type.value,
                'value': m.value,
                'start': m.start,
                'end': m.end,
                'confidence': m.confidence
            }
            for m in result['matches']
            if m.confidence >= confidence
        ]
        output_data = {
            'file_path': result['file_path'],
            'has_pii': result['has_pii'],
            'matches': matches_dict
        }
        click.echo(json.dumps(output_data, indent=2))
    else:
        click.echo(f"File: {filepath}")
        click.echo(f"PII Detected: {'Yes' if result['has_pii'] else 'No'}")

        if result['has_pii']:
            high_conf_matches = [m for m in result['matches'] if m.confidence >= confidence]

            if high_conf_matches:
                click.echo(f"\nFound {len(high_conf_matches)} PII instance(s):\n")

                for i, match in enumerate(high_conf_matches, 1):
                    click.echo(f"{i}. {match.pii_type.value.upper()}")
                    click.echo(f"   Value: {match.value}")
                    click.echo(f"   Confidence: {match.confidence:.0%}")
                    click.echo()


@pii.command('scan-article')
@click.argument('article_id')
@click.option('--confidence', type=float, default=0.7)
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def pii_scan_article(article_id: str, confidence: float, output: str):
    """
    Scan a compiled article for PII.

    Example:
        kb pii scan-article <article-id>
    """
    result = scan_article(article_id)

    if 'error' in result:
        click.echo(f"✗ Error: {result['error']}", err=True)
        raise click.Abort()

    if output == 'json':
        matches_dict = [
            {
                'type': m.pii_type.value,
                'value': m.value,
                'confidence': m.confidence
            }
            for m in result.get('matches', [])
            if m.confidence >= confidence
        ]
        click.echo(json.dumps({'article_id': article_id, 'matches': matches_dict}, indent=2))
    else:
        click.echo(f"Article: {result.get('title', article_id)}")
        click.echo(f"PII Detected: {'Yes' if result['has_pii'] else 'No'}")

        if result['has_pii']:
            high_conf = [m for m in result['matches'] if m.confidence >= confidence]

            if high_conf:
                summary = get_pii_summary(high_conf)
                click.echo(f"\nSummary:")
                for pii_type, count in summary['by_type'].items():
                    click.echo(f"  {pii_type}: {count}")


@pii.command('scan-all')
@click.option('--confidence', type=float, default=0.7)
@click.option('--output', type=click.Choice(['text', 'json']), default='text')
def pii_scan_all(confidence: float, output: str):
    """
    Scan all articles for PII.

    Example:
        kb pii scan-all
    """
    results = scan_all_articles(confidence_threshold=confidence)

    if output == 'json':
        click.echo(json.dumps({'articles_with_pii': len(results)}, indent=2))
    else:
        if not results:
            click.echo("✓ No PII detected in any articles")
        else:
            click.echo(f"⚠️  Found PII in {len(results)} article(s):\n")

            for result in results[:10]:  # Show first 10
                click.echo(f"  • {result['title']} ({result['article_id']})")
                summary = get_pii_summary(result['matches'])
                for pii_type, count in summary['by_type'].items():
                    click.echo(f"    - {pii_type}: {count}")

            if len(results) > 10:
                click.echo(f"\n  ... and {len(results) - 10} more")


def register(cli):
    """Register PII commands with main CLI"""
    cli.add_command(pii)
