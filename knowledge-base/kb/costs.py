"""Cost tracking and budget management"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from kb.db import get_connection
from kb.config import load_config
from kb.errors import PermanentError

logger = logging.getLogger(__name__)


def record_cost(
    operation: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    job_id: Optional[str] = None,
    check_budget_first: bool = False
):
    """
    Record LLM API cost in database.

    Args:
        operation: Operation type (compile, query, review)
        model: Model ID used
        input_tokens: Input token count
        output_tokens: Output token count
        cost_usd: Cost in USD
        job_id: Optional job ID reference
        check_budget_first: If True, atomically check budget before recording
    """
    conn = get_connection()

    # BEGIN IMMEDIATE prevents concurrent writers from interleaving
    conn.execute("BEGIN IMMEDIATE")

    try:
        if check_budget_first:
            # Atomic budget check within the same transaction
            config = load_config()
            budget_config = config.get('knowledge_base', {}).get('compilation', {}).get('budget', {})
            daily_budget = budget_config.get('daily_usd', 5.00)

            cursor = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0.0) as total
                FROM costs
                WHERE timestamp >= date('now', 'start of day')
            """)
            today_spending = cursor.fetchone()['total']

            if today_spending + cost_usd > daily_budget:
                conn.execute("ROLLBACK")
                raise PermanentError(
                    f"Budget exceeded: ${today_spending + cost_usd:.2f} "
                    f"(limit: ${daily_budget:.2f}, current: ${today_spending:.2f})"
                )

        conn.execute("""
            INSERT INTO costs (operation, model, input_tokens, output_tokens, cost_usd, job_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (operation, model, input_tokens, output_tokens, cost_usd, job_id))

        conn.execute("COMMIT")

        logger.info(f"Recorded cost: ${cost_usd:.4f} for {operation} ({model})")

    except PermanentError:
        raise
    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def get_daily_spending(date: Optional[datetime] = None) -> float:
    """
    Get total spending for a specific day.

    Args:
        date: Date to check (default: today)

    Returns:
        Total spending in USD
    """
    if date is None:
        date = datetime.now()

    # Get start and end of day
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    conn = get_connection()
    cursor = conn.execute("""
        SELECT COALESCE(SUM(cost_usd), 0.0) as total
        FROM costs
        WHERE timestamp >= ? AND timestamp < ?
    """, (start_of_day.isoformat(), end_of_day.isoformat()))

    row = cursor.fetchone()
    return row['total'] if row else 0.0


def get_cost_summary(days: int = 30) -> Dict[str, any]:
    """
    Get cost summary for last N days.

    Args:
        days: Number of days to summarize

    Returns:
        {
            'total_usd': float,
            'by_operation': dict,
            'by_model': dict,
            'by_day': list
        }
    """
    conn = get_connection()

    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    # Total cost
    cursor = conn.execute("""
        SELECT COALESCE(SUM(cost_usd), 0.0) as total
        FROM costs
        WHERE timestamp >= ?
    """, (start_date.isoformat(),))
    total_usd = cursor.fetchone()['total']

    # By operation
    cursor = conn.execute("""
        SELECT operation, COALESCE(SUM(cost_usd), 0.0) as total
        FROM costs
        WHERE timestamp >= ?
        GROUP BY operation
        ORDER BY total DESC
    """, (start_date.isoformat(),))
    by_operation = {row['operation']: row['total'] for row in cursor}

    # By model
    cursor = conn.execute("""
        SELECT model, COALESCE(SUM(cost_usd), 0.0) as total
        FROM costs
        WHERE timestamp >= ?
        GROUP BY model
        ORDER BY total DESC
    """, (start_date.isoformat(),))
    by_model = {row['model']: row['total'] for row in cursor}

    # By day
    cursor = conn.execute("""
        SELECT DATE(timestamp) as day, COALESCE(SUM(cost_usd), 0.0) as total
        FROM costs
        WHERE timestamp >= ?
        GROUP BY DATE(timestamp)
        ORDER BY day DESC
        LIMIT 30
    """, (start_date.isoformat(),))
    by_day = [{'day': row['day'], 'total': row['total']} for row in cursor]

    return {
        'total_usd': total_usd,
        'days': days,
        'by_operation': by_operation,
        'by_model': by_model,
        'by_day': by_day
    }


def check_budget(cost_usd: float, hard_limit: bool = True) -> bool:
    """
    Check if operation would exceed budget.

    Args:
        cost_usd: Estimated cost for operation
        hard_limit: If True, enforce budget strictly

    Returns:
        True if within budget, False otherwise

    Raises:
        PermanentError: If hard_limit=True and budget exceeded
    """
    config = load_config()
    budget_config = config.get('knowledge_base', {}).get('compilation', {}).get('budget', {})

    daily_budget = budget_config.get('daily_usd', 5.00)
    alert_threshold = budget_config.get('alert_threshold_usd', 0.50)

    # Get today's spending
    today_spending = get_daily_spending()

    # Check if this operation would exceed budget
    projected_spending = today_spending + cost_usd

    if projected_spending > daily_budget:
        message = (
            f"Budget exceeded: ${projected_spending:.2f} "
            f"(limit: ${daily_budget:.2f}, current: ${today_spending:.2f})"
        )

        if hard_limit:
            logger.error(message)
            raise PermanentError(message)
        else:
            logger.warning(message)
            return False

    # Check alert threshold
    if cost_usd > alert_threshold:
        logger.warning(
            f"High cost operation: ${cost_usd:.2f} "
            f"(alert threshold: ${alert_threshold:.2f})"
        )

    return True


def get_budget_status() -> Dict[str, any]:
    """
    Get current budget status.

    Returns:
        {
            'daily_budget_usd': float,
            'today_spending_usd': float,
            'remaining_usd': float,
            'used_percentage': float
        }
    """
    config = load_config()
    budget_config = config.get('knowledge_base', {}).get('compilation', {}).get('budget', {})

    daily_budget = budget_config.get('daily_usd', 5.00)
    today_spending = get_daily_spending()
    remaining = max(0, daily_budget - today_spending)
    used_percentage = (today_spending / daily_budget * 100) if daily_budget > 0 else 0

    return {
        'daily_budget_usd': daily_budget,
        'today_spending_usd': today_spending,
        'remaining_usd': remaining,
        'used_percentage': used_percentage
    }
