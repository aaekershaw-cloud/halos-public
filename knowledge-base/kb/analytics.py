"""Usage analytics and reporting"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from kb.db import get_connection
from kb.performance import cached

logger = logging.getLogger(__name__)


@cached(ttl_seconds=60)
def get_article_stats() -> Dict[str, Any]:
    """
    Get article statistics.

    Returns:
        Article statistics dictionary
    """
    conn = get_connection()

    # Total articles
    cursor = conn.execute("SELECT COUNT(*) as count FROM articles WHERE deleted_at IS NULL")
    total = cursor.fetchone()['count']

    # By classification
    cursor = conn.execute("""
        SELECT classification, COUNT(*) as count
        FROM articles
        WHERE deleted_at IS NULL
        GROUP BY classification
    """)
    by_classification = {row['classification']: row['count'] for row in cursor}

    # Articles by month
    cursor = conn.execute("""
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
        FROM articles
        WHERE deleted_at IS NULL
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """)
    by_month = [{'month': row['month'], 'count': row['count']} for row in cursor]

    return {
        'total': total,
        'by_classification': by_classification,
        'by_month': by_month
    }


@cached(ttl_seconds=60)
def get_compilation_stats(days: int = 30) -> Dict[str, Any]:
    """
    Get compilation statistics.

    Args:
        days: Number of days to analyze

    Returns:
        Compilation statistics
    """
    conn = get_connection()

    cutoff = datetime.now() - timedelta(days=days)

    # Total jobs
    cursor = conn.execute("""
        SELECT COUNT(*) as count
        FROM jobs
        WHERE job_type = 'compile' AND created_at >= ?
    """, (cutoff.isoformat(),))
    total = cursor.fetchone()['count']

    # By status
    cursor = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM jobs
        WHERE job_type = 'compile' AND created_at >= ?
        GROUP BY status
    """, (cutoff.isoformat(),))
    by_status = {row['status']: row['count'] for row in cursor}

    # Success rate
    completed = by_status.get('completed', 0)
    failed = by_status.get('failed', 0)
    success_rate = (completed / total * 100) if total > 0 else 0

    return {
        'days': days,
        'total': total,
        'by_status': by_status,
        'success_rate': round(success_rate, 2)
    }


def get_search_stats(days: int = 30) -> Dict[str, Any]:
    """
    Get search usage statistics.

    Note: Requires search logging to be implemented.
    Currently returns placeholder data.

    Args:
        days: Number of days to analyze

    Returns:
        Search statistics
    """
    # Placeholder until search logging is implemented
    return {
        'days': days,
        'total_searches': 0,
        'unique_queries': 0,
        'avg_results': 0,
        'note': 'Search logging not yet implemented'
    }


def get_review_stats() -> Dict[str, Any]:
    """
    Get review queue statistics.

    Returns:
        Review statistics
    """
    from kb.review import get_review_stats
    return get_review_stats()


def get_cost_analysis(days: int = 30) -> Dict[str, Any]:
    """
    Get cost analysis.

    Args:
        days: Number of days to analyze

    Returns:
        Cost analysis
    """
    from kb.costs import get_cost_summary
    return get_cost_summary(days=days)


@cached(ttl_seconds=120)
def get_system_health() -> Dict[str, Any]:
    """
    Get overall system health metrics.

    Returns:
        Health metrics
    """
    from kb.lint import run_all_checks, get_summary
    from kb.links import get_link_stats

    # Run integrity checks
    check_results = run_all_checks()
    check_summary = get_summary(check_results)

    # Get link stats
    link_stats = get_link_stats()

    # Get article stats
    article_stats = get_article_stats()

    return {
        'articles': article_stats['total'],
        'integrity': {
            'total_issues': check_summary['total_issues'],
            'errors': check_summary['by_severity']['error'],
            'warnings': check_summary['by_severity']['warning'],
            'fixable': check_summary['fixable']
        },
        'links': {
            'total': link_stats['total_links'],
            'articles_with_links': link_stats['articles_with_links']
        },
        'health_score': calculate_health_score(check_summary, article_stats)
    }


def calculate_health_score(check_summary: Dict, article_stats: Dict) -> int:
    """
    Calculate system health score (0-100).

    Args:
        check_summary: Integrity check summary
        article_stats: Article statistics

    Returns:
        Health score
    """
    score = 100

    # Deduct for errors
    errors = check_summary['by_severity']['error']
    score -= errors * 10  # -10 per error

    # Deduct for warnings
    warnings = check_summary['by_severity']['warning']
    score -= warnings * 2  # -2 per warning

    # Bonus for having articles
    if article_stats['total'] > 0:
        score = max(score, 20)  # Minimum 20 if you have content

    return max(0, min(100, score))


def generate_report(days: int = 7) -> Dict[str, Any]:
    """
    Generate comprehensive analytics report.

    Args:
        days: Number of days to include

    Returns:
        Complete analytics report
    """
    return {
        'generated_at': datetime.now().isoformat(),
        'period_days': days,
        'articles': get_article_stats(),
        'compilation': get_compilation_stats(days),
        'review': get_review_stats(),
        'costs': get_cost_analysis(days),
        'health': get_system_health()
    }
