"""Job queue management for background compilation"""

import json
import time
import logging
import uuid
from datetime import datetime
from typing import Dict, Optional, Any, List
from kb.db import get_connection
from kb.errors import TransientError, PermanentError

logger = logging.getLogger(__name__)


def create_job(job_type: str, params: Dict[str, Any]) -> str:
    """
    Create a new job in the queue.

    Args:
        job_type: Type of job (compile, query, etc.)
        params: Job parameters as dictionary

    Returns:
        Job ID (UUID)
    """
    job_id = str(uuid.uuid4())
    conn = get_connection()

    conn.execute("BEGIN")

    try:
        conn.execute("""
            INSERT INTO jobs (id, job_type, status, params)
            VALUES (?, ?, 'pending', ?)
        """, (job_id, job_type, json.dumps(params)))

        conn.execute("COMMIT")

        logger.info(f"Created job: {job_id} ({job_type})")
        return job_id

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def claim_next_job(conn=None) -> Optional[Dict[str, Any]]:
    """
    Claim next pending job (portable SQLite pattern).

    Uses BEGIN IMMEDIATE + SELECT + UPDATE pattern for portability.
    Does NOT use UPDATE ... LIMIT 1 which is non-standard.

    Args:
        conn: Optional connection (creates new if None)

    Returns:
        Job dict or None if no pending jobs
    """
    if conn is None:
        conn = get_connection()

    conn.execute("BEGIN IMMEDIATE")  # Acquire write lock

    try:
        # Find oldest pending job
        cursor = conn.execute("""
            SELECT id, job_type, params, retry_count
            FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT 1
        """)

        row = cursor.fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None

        job_id = row['id']
        job_type = row['job_type']
        params = json.loads(row['params']) if row['params'] else {}
        retry_count = row['retry_count']

        # Claim it
        conn.execute("""
            UPDATE jobs
            SET status = 'running',
                started_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (job_id,))

        conn.execute("COMMIT")

        logger.info(f"Claimed job: {job_id} ({job_type})")

        return {
            'id': job_id,
            'type': job_type,
            'params': params,
            'retry_count': retry_count
        }

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def update_job_status(
    job_id: str,
    status: str,
    result: Optional[Dict] = None,
    error: Optional[str] = None
):
    """
    Update job status.

    Args:
        job_id: Job ID
        status: New status (running, awaiting_review, approved, completed, failed)
        result: Optional result data
        error: Optional error message
    """
    conn = get_connection()

    conn.execute("BEGIN")

    try:
        # Build update query
        updates = ["status = ?"]
        params = [status]

        if status == 'completed':
            updates.append("completed_at = CURRENT_TIMESTAMP")

        if result:
            updates.append("result = ?")
            params.append(json.dumps(result))

        if error:
            updates.append("error = ?")
            updates.append("last_error = ?")
            params.extend([error, error])

        params.append(job_id)

        sql = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?"

        conn.execute(sql, params)
        conn.execute("COMMIT")

        logger.info(f"Job {job_id} status: {status}")

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def mark_job_awaiting_review(job_id: str, review_id: str, message: str):
    """
    Mark job as awaiting human review.

    Args:
        job_id: Job ID
        review_id: Review queue entry ID
        message: Description of what needs review
    """
    conn = get_connection()

    conn.execute("BEGIN")

    try:
        conn.execute("""
            UPDATE jobs
            SET status = 'awaiting_review',
                review_id = ?,
                params = json_set(params, '$.review_message', ?)
            WHERE id = ?
        """, (review_id, message, job_id))

        conn.execute("COMMIT")

        logger.info(f"Job {job_id} awaiting review: {review_id}")

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def requeue_approved_job(job_id: str, approved_changes: Dict):
    """
    Re-queue job after review approval.

    Transitions: awaiting_review → approved → pending

    Args:
        job_id: Job ID
        approved_changes: Approved changes from review
    """
    conn = get_connection()

    conn.execute("BEGIN")

    try:
        # First set to approved
        conn.execute("""
            UPDATE jobs
            SET status = 'approved',
                params = json_set(params, '$.approved_changes', ?)
            WHERE id = ?
        """, (json.dumps(approved_changes), job_id))

        # Then set to pending (worker will pick up again)
        conn.execute("""
            UPDATE jobs
            SET status = 'pending',
                params = json_set(params, '$.requeued_at', CURRENT_TIMESTAMP)
            WHERE id = ? AND status = 'approved'
        """, (job_id,))

        conn.execute("COMMIT")

        logger.info(f"Re-queued job after approval: {job_id}")

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def execute_job_with_retry(job_id: str, execute_func, max_retries: int = 3):
    """
    Execute job with exponential backoff retry for transient errors.

    Args:
        job_id: Job ID
        execute_func: Function to execute job (takes job dict, returns result)
        max_retries: Maximum number of retries

    Returns:
        Job result

    Raises:
        PermanentError: After max retries exhausted or on permanent error
    """
    conn = get_connection()

    # Load job
    cursor = conn.execute("""
        SELECT id, job_type, params, retry_count
        FROM jobs
        WHERE id = ?
    """, (job_id,))

    row = cursor.fetchone()
    if not row:
        raise PermanentError(f"Job not found: {job_id}")

    job = {
        'id': row['id'],
        'type': row['job_type'],
        'params': json.loads(row['params']) if row['params'] else {},
        'retry_count': row['retry_count']
    }

    retry_count = job['retry_count']
    base_delay = 5  # seconds

    while retry_count <= max_retries:
        try:
            # Execute job
            result = execute_func(job)

            # Mark completed
            update_job_status(job_id, 'completed', result=result)

            return result

        except TransientError as e:
            # Retryable error (rate limit, timeout, network)
            retry_count += 1

            if retry_count > max_retries:
                # Exhausted retries - mark as failed
                update_job_status(
                    job_id,
                    'failed',
                    error=f"Max retries exceeded: {str(e)}"
                )
                raise PermanentError(
                    f"Job {job_id} failed after {max_retries} retries: {e}"
                ) from e

            # Exponential backoff: 5s, 10s, 20s
            delay = base_delay * (2 ** (retry_count - 1))
            logger.warning(
                f"Job {job_id} transient error "
                f"(retry {retry_count}/{max_retries} in {delay}s): {e}"
            )

            # Update retry count
            conn.execute("BEGIN")
            conn.execute("""
                UPDATE jobs
                SET retry_count = ?,
                    last_error = ?
                WHERE id = ?
            """, (retry_count, str(e), job_id))
            conn.execute("COMMIT")

            time.sleep(delay)

        except PermanentError as e:
            # Non-retryable error
            update_job_status(
                job_id,
                'failed',
                error=f"Permanent error: {str(e)}"
            )
            logger.error(f"Job {job_id} permanent error: {e}")
            raise

        except Exception as e:
            # Unknown error - treat as permanent
            update_job_status(
                job_id,
                'failed',
                error=f"Unknown error: {str(e)}"
            )
            logger.error(f"Job {job_id} unknown error: {e}", exc_info=True)
            raise PermanentError(f"Unexpected error in job {job_id}: {e}") from e


def list_jobs(status: Optional[str] = None, limit: int = 20) -> List[Dict]:
    """
    List jobs with optional status filter.

    Args:
        status: Optional status filter (pending, running, completed, failed)
        limit: Maximum number of jobs to return

    Returns:
        List of job dictionaries
    """
    conn = get_connection()

    if status:
        cursor = conn.execute("""
            SELECT id, job_type, status, created_at, started_at, completed_at, error
            FROM jobs
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (status, limit))
    else:
        cursor = conn.execute("""
            SELECT id, job_type, status, created_at, started_at, completed_at, error
            FROM jobs
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))

    jobs = []
    for row in cursor:
        jobs.append({
            'id': row['id'],
            'type': row['job_type'],
            'status': row['status'],
            'created_at': row['created_at'],
            'started_at': row['started_at'],
            'completed_at': row['completed_at'],
            'error': row['error']
        })

    return jobs


def get_job_status(job_id: str) -> Optional[Dict]:
    """
    Get detailed job status.

    Args:
        job_id: Job ID

    Returns:
        Job status dict or None if not found
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, job_type, status, params, result, error,
               created_at, started_at, completed_at, retry_count, review_id
        FROM jobs
        WHERE id = ?
    """, (job_id,))

    row = cursor.fetchone()
    if not row:
        return None

    return {
        'id': row['id'],
        'type': row['job_type'],
        'status': row['status'],
        'params': json.loads(row['params']) if row['params'] else {},
        'result': json.loads(row['result']) if row['result'] else None,
        'error': row['error'],
        'created_at': row['created_at'],
        'started_at': row['started_at'],
        'completed_at': row['completed_at'],
        'retry_count': row['retry_count'],
        'review_id': row['review_id']
    }


def cancel_job(job_id: str) -> bool:
    """
    Cancel a running or pending job.

    Args:
        job_id: Job ID

    Returns:
        True if cancelled, False if job not found or already completed
    """
    conn = get_connection()

    conn.execute("BEGIN")

    try:
        cursor = conn.execute("""
            SELECT status FROM jobs WHERE id = ?
        """, (job_id,))

        row = cursor.fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return False

        status = row['status']

        if status in ('completed', 'failed', 'cancelled'):
            conn.execute("ROLLBACK")
            return False

        conn.execute("""
            UPDATE jobs
            SET status = 'cancelled',
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (job_id,))

        conn.execute("COMMIT")

        logger.info(f"Cancelled job: {job_id}")
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        raise
