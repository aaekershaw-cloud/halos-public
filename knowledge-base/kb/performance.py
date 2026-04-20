"""Performance optimizations and caching"""

import logging
import hashlib
import re
import time
from typing import Any, Optional, Callable, Dict, List
from functools import wraps
from datetime import datetime, timedelta
from kb.db import get_connection

logger = logging.getLogger(__name__)


class QueryCache:
    """Simple in-memory cache for query results"""

    def __init__(self, ttl_seconds: int = 300, max_size: int = 100):
        """
        Initialize cache.

        Args:
            ttl_seconds: Time-to-live for cached entries
            max_size: Maximum number of cached entries
        """
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0
        }

    def _make_key(self, func_name: str, args: tuple, kwargs: dict) -> str:
        """Generate cache key from function and arguments"""
        key_parts = [func_name]
        key_parts.extend(str(arg) for arg in args)
        key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        key_str = ":".join(key_parts)
        return hashlib.md5(key_str.encode()).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired"""
        if key not in self.cache:
            self.stats['misses'] += 1
            return None

        entry = self.cache[key]
        if datetime.now() > entry['expires_at']:
            # Expired
            del self.cache[key]
            self.stats['misses'] += 1
            return None

        self.stats['hits'] += 1
        return entry['value']

    def set(self, key: str, value: Any):
        """Set value in cache with TTL"""
        # Evict oldest if at capacity
        if len(self.cache) >= self.max_size:
            oldest_key = min(self.cache.keys(),
                           key=lambda k: self.cache[k]['created_at'])
            del self.cache[oldest_key]
            self.stats['evictions'] += 1

        self.cache[key] = {
            'value': value,
            'created_at': datetime.now(),
            'expires_at': datetime.now() + timedelta(seconds=self.ttl_seconds)
        }

    def clear(self):
        """Clear all cached entries"""
        self.cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        total_requests = self.stats['hits'] + self.stats['misses']
        hit_rate = (self.stats['hits'] / total_requests * 100) if total_requests > 0 else 0

        return {
            **self.stats,
            'size': len(self.cache),
            'hit_rate': round(hit_rate, 2),
            'total_requests': total_requests
        }


# Global cache instance
_query_cache = QueryCache(ttl_seconds=300, max_size=100)


def cached(ttl_seconds: int = 300):
    """
    Decorator to cache function results.

    Args:
        ttl_seconds: Time-to-live for cached result

    Example:
        @cached(ttl_seconds=60)
        def expensive_query(param):
            return some_slow_operation(param)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = _query_cache._make_key(func.__name__, args, kwargs)

            # Check cache
            cached_value = _query_cache.get(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache hit for {func.__name__}")
                return cached_value

            # Execute function
            logger.debug(f"Cache miss for {func.__name__}")
            result = func(*args, **kwargs)

            # Store in cache
            _query_cache.set(cache_key, result)

            return result

        return wrapper
    return decorator


def clear_cache():
    """Clear all cached query results"""
    _query_cache.clear()
    logger.info("Query cache cleared")


def get_cache_stats() -> Dict[str, Any]:
    """Get cache statistics"""
    return _query_cache.get_stats()


def optimize_fts_query(query: str) -> str:
    """
    Optimize FTS query for better performance.

    Args:
        query: Original search query

    Returns:
        Optimized FTS query
    """
    # Remove common stop words that slow down FTS
    stop_words = {'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for'}

    # Sanitize tokens: FTS5 treats ':' as a column qualifier, '"' as a phrase
    # delimiter, and '(', ')', '^' as operators. Any of these in a bare token
    # crashes the parser (e.g. "halos:" → "no such column: halos"). Strip all
    # punctuation that isn't alphanumeric, hyphen, dot, or underscore.
    raw_words = query.lower().split()
    filtered_words = []
    for w in raw_words:
        cleaned = re.sub(r'[^a-z0-9_.\-]', '', w)
        if cleaned and cleaned not in stop_words:
            filtered_words.append(cleaned)

    # If all words were stop-worded/stripped away, return a no-op phrase match
    # rather than the raw query (which would still crash FTS5 parser).
    if not filtered_words:
        return '""'

    # Quote terms containing hyphens or dots (FTS5 tokenizer-unfriendly chars)
    processed = []
    for word in filtered_words:
        if '-' in word or '.' in word:
            processed.append(f'"{word}"')
        elif len(word) > 2:
            processed.append(f"{word}*")
        else:
            processed.append(word)

    return ' '.join(processed)


def batch_insert(table: str, records: List[Dict[str, Any]],
                batch_size: int = 100) -> int:
    """
    Insert records in batches for better performance.

    Args:
        table: Table name
        records: List of records to insert
        batch_size: Number of records per batch

    Returns:
        Number of records inserted
    """
    if not records:
        return 0

    conn = get_connection()
    inserted = 0

    # Get column names from first record
    columns = list(records[0].keys())
    placeholders = ', '.join(['?'] * len(columns))
    column_names = ', '.join(columns)

    sql = f"INSERT INTO {table} ({column_names}) VALUES ({placeholders})"

    try:
        # Process in batches
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]

            conn.execute("BEGIN")
            try:
                for record in batch:
                    values = [record[col] for col in columns]
                    conn.execute(sql, values)

                conn.execute("COMMIT")
                inserted += len(batch)

            except Exception as e:
                conn.execute("ROLLBACK")
                logger.error(f"Batch insert failed: {e}")
                raise

        logger.info(f"Batch inserted {inserted} records into {table}")
        return inserted

    except Exception as e:
        logger.error(f"Batch insert failed: {e}")
        raise


def batch_update(table: str, updates: List[Dict[str, Any]],
                id_column: str = 'id', batch_size: int = 100) -> int:
    """
    Update records in batches for better performance.

    Args:
        table: Table name
        updates: List of update dictionaries (must include id_column)
        id_column: Name of ID column
        batch_size: Number of records per batch

    Returns:
        Number of records updated
    """
    if not updates:
        return 0

    conn = get_connection()
    updated = 0

    try:
        # Process in batches
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]

            conn.execute("BEGIN")
            try:
                for record in batch:
                    # Extract ID
                    record_id = record[id_column]

                    # Build SET clause
                    set_columns = [f"{k} = ?" for k in record.keys() if k != id_column]
                    set_values = [v for k, v in record.items() if k != id_column]

                    sql = f"UPDATE {table} SET {', '.join(set_columns)} WHERE {id_column} = ?"
                    conn.execute(sql, set_values + [record_id])

                conn.execute("COMMIT")
                updated += len(batch)

            except Exception as e:
                conn.execute("ROLLBACK")
                logger.error(f"Batch update failed: {e}")
                raise

        logger.info(f"Batch updated {updated} records in {table}")
        return updated

    except Exception as e:
        logger.error(f"Batch update failed: {e}")
        raise


def benchmark(func: Callable) -> Callable:
    """
    Decorator to measure function execution time.

    Example:
        @benchmark
        def slow_function():
            # do work
            pass
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - start

        logger.info(f"{func.__name__} took {elapsed:.3f}s")

        return result

    return wrapper


def analyze_query_performance(sql: str) -> Dict[str, Any]:
    """
    Analyze query performance using EXPLAIN QUERY PLAN.

    Args:
        sql: SQL query to analyze

    Returns:
        Query plan information
    """
    conn = get_connection()

    cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}")
    plan = [dict(row) for row in cursor]

    return {
        'query': sql,
        'plan': plan
    }


def optimize_database():
    """
    Run database optimization commands.

    - VACUUM: Rebuilds database file, reclaiming space
    - ANALYZE: Updates query optimizer statistics
    """
    conn = get_connection()

    logger.info("Running VACUUM...")
    conn.execute("VACUUM")

    logger.info("Running ANALYZE...")
    conn.execute("ANALYZE")

    logger.info("Database optimization complete")


def get_database_stats() -> Dict[str, Any]:
    """
    Get database statistics.

    Returns:
        Database statistics
    """
    conn = get_connection()

    # Page count
    cursor = conn.execute("PRAGMA page_count")
    page_count = cursor.fetchone()[0]

    # Page size
    cursor = conn.execute("PRAGMA page_size")
    page_size = cursor.fetchone()[0]

    # Database size
    db_size = page_count * page_size

    # Table sizes
    cursor = conn.execute("""
        SELECT name, SUM(pgsize) as size
        FROM dbstat
        GROUP BY name
        ORDER BY size DESC
    """)
    table_sizes = [dict(row) for row in cursor]

    return {
        'page_count': page_count,
        'page_size': page_size,
        'size_bytes': db_size,
        'size_mb': round(db_size / 1024 / 1024, 2),
        'tables': table_sizes
    }
