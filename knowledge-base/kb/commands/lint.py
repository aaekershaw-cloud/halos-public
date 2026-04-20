"""CLI commands for integrity checks and linting"""

import click
import json
from kb.lint import (
    run_all_checks,
    get_summary,
    fix_orphaned_raw_files,
    fix_invalid_frontmatter as fix_frontmatter,
    fix_checksum_mismatches,
    fix_broken_links,
    rebuild_fts_index,
    check_orphaned_raw_files,
    check_missing_content_files,
    check_checksum_mismatches,
    check_duplicate_slugs,
    check_invalid_frontmatter,
    check_broken_links,
    check_fts_index_sync
)


@click.command()
@click.option('--fix', is_flag=True, help='Auto-fix deterministic issues')
@click.option('--check', type=click.Choice([
    'orphaned_raw_files',
    'missing_content_files',
    'checksum_mismatches',
    'duplicate_slugs',
    'invalid_frontmatter',
    'broken_links',
    'fts_index_sync'
]), help='Run specific check')
@click.option('--rebuild-fts', is_flag=True, help='Rebuild FTS index')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def lint(fix: bool, check: str, rebuild_fts: bool, output: str):
    """
    Run integrity checks on knowledge base.

    Example:
        kb lint                          # Run all checks
        kb lint --fix                    # Run and auto-fix
        kb lint --check broken_links     # Run specific check
        kb lint --rebuild-fts            # Rebuild FTS index
    """
    # Handle FTS rebuild
    if rebuild_fts:
        try:
            count = rebuild_fts_index()
            click.echo(f"✓ Rebuilt FTS index for {count} articles")
            return
        except Exception as e:
            click.echo(f"✗ Failed to rebuild FTS index: {e}", err=True)
            raise click.Abort()

    # Run checks
    results = None
    if check:
        # Run specific check
        check_funcs = {
            'orphaned_raw_files': check_orphaned_raw_files,
            'missing_content_files': check_missing_content_files,
            'checksum_mismatches': check_checksum_mismatches,
            'duplicate_slugs': check_duplicate_slugs,
            'invalid_frontmatter': check_invalid_frontmatter,
            'broken_links': check_broken_links,
            'fts_index_sync': check_fts_index_sync
        }

        check_func = check_funcs[check]
        issues = check_func()

        if output == 'json':
            output_data = [
                {
                    'check_type': i.check_type,
                    'severity': i.severity,
                    'message': i.message,
                    'details': i.details,
                    'fixable': i.fixable
                }
                for i in issues
            ]
            click.echo(json.dumps(output_data, indent=2))
        else:
            if not issues:
                click.echo(f"✓ No issues found ({check})")
            else:
                click.echo(f"Check: {check}")
                click.echo(f"Found {len(issues)} issue(s):\n")

                for i, issue in enumerate(issues, 1):
                    severity_icon = {'error': '✗', 'warning': '⚠', 'info': 'ℹ'}
                    icon = severity_icon.get(issue.severity, '•')

                    click.echo(f"{i}. {icon} [{issue.severity.upper()}] {issue.message}")

                    if issue.details:
                        for key, value in issue.details.items():
                            if isinstance(value, (list, dict)):
                                value = json.dumps(value)
                            click.echo(f"   {key}: {value}")

                    if issue.fixable:
                        click.echo(f"   (fixable)")

                    click.echo()

    else:
        # Run all checks
        results = run_all_checks()
        summary = get_summary(results)

        if output == 'json':
            output_data = {
                'summary': summary,
                'results': {
                    check_name: [
                        {
                            'check_type': i.check_type,
                            'severity': i.severity,
                            'message': i.message,
                            'details': i.details,
                            'fixable': i.fixable
                        }
                        for i in issues
                    ]
                    for check_name, issues in results.items()
                }
            }
            click.echo(json.dumps(output_data, indent=2))
        else:
            # Print summary
            click.echo("Knowledge Base Integrity Check")
            click.echo("=" * 60)
            click.echo()

            click.echo(f"Checks run: {summary['checks_run']}")
            click.echo(f"Total issues: {summary['total_issues']}")
            click.echo(f"  Errors: {summary['by_severity']['error']}")
            click.echo(f"  Warnings: {summary['by_severity']['warning']}")
            click.echo(f"  Fixable: {summary['fixable']}")
            click.echo()

            # Print issues by check
            for check_name, issues in results.items():
                if issues:
                    click.echo(f"Check: {check_name}")
                    click.echo(f"  Found {len(issues)} issue(s)")

                    for issue in issues[:3]:  # Show first 3
                        severity_icon = {'error': '✗', 'warning': '⚠', 'info': 'ℹ'}
                        icon = severity_icon.get(issue.severity, '•')
                        click.echo(f"  {icon} {issue.message}")

                    if len(issues) > 3:
                        click.echo(f"  ... and {len(issues) - 3} more")

                    click.echo()

    # Auto-fix if requested
    if fix:
        click.echo()
        click.echo("Auto-fixing issues...")

        total_fixed = 0

        # Fix orphaned raw files
        fixed_orphans = fix_orphaned_raw_files(dry_run=False)
        if fixed_orphans > 0:
            click.echo(f"  Removed {fixed_orphans} orphaned raw file(s)")
            total_fixed += fixed_orphans

        # Fix invalid frontmatter
        fixed_fm = fix_frontmatter(dry_run=False)
        if fixed_fm > 0:
            click.echo(f"  Fixed frontmatter on {fixed_fm} article(s)")
            total_fixed += fixed_fm

        # Fix checksums
        fixed_checksums = fix_checksum_mismatches(dry_run=False)
        if fixed_checksums > 0:
            click.echo(f"  Fixed {fixed_checksums} checksum mismatch(es)")
            total_fixed += fixed_checksums

        # Fix broken links
        fixed_links = fix_broken_links(dry_run=False)
        if fixed_links > 0:
            click.echo(f"  Removed {fixed_links} broken link(s)")
            total_fixed += fixed_links

        # Rebuild FTS if it was out of sync
        if results and results.get('fts_index_sync'):
            fts_count = rebuild_fts_index()
            click.echo(f"  Rebuilt FTS index ({fts_count} articles)")
            total_fixed += 1

        if total_fixed == 0:
            click.echo("  No auto-fixable issues found")


def register(cli):
    """Register lint command with main CLI"""
    cli.add_command(lint)
