"""Phase 2 Integration Tests

Tests for LLM compilation, review queue, jobs, and cost tracking.
"""

import os
import sys
import tempfile
import shutil
import json
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.db import init_database, get_connection
from kb.llm import map_model_name, calculate_cost, estimate_tokens
from kb.costs import record_cost, get_daily_spending, get_budget_status, get_cost_summary
from kb.jobs import create_job, claim_next_job, update_job_status, get_job_status, list_jobs
from kb.review import list_pending_reviews, get_review_details, approve_review, reject_review, get_review_stats


def test_llm_functions():
    """Test LLM helper functions (no API calls)"""
    print("Testing LLM helper functions...")

    # Test model mapping
    assert map_model_name('haiku') == 'claude-3-5-haiku-20241022'
    assert map_model_name('sonnet') == 'claude-3-5-sonnet-20241022'
    assert map_model_name('opus') == 'claude-opus-4-20250514'

    # Test cost calculation
    cost = calculate_cost('claude-3-5-sonnet-20241022', 1000, 500)
    assert cost > 0
    expected = (1000 / 1_000_000) * 3.00 + (500 / 1_000_000) * 15.00
    assert abs(cost - expected) < 0.0001

    # Test token estimation
    text = "This is a test string"
    tokens = estimate_tokens(text)
    assert tokens == len(text) // 4

    print("✓ LLM functions working\n")


def test_cost_tracking():
    """Test cost tracking and budget management"""
    print("Testing cost tracking...")

    # Record some test costs
    record_cost(
        operation='compile',
        model='claude-3-5-sonnet-20241022',
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.0105
    )

    record_cost(
        operation='query',
        model='claude-3-5-haiku-20241022',
        input_tokens=500,
        output_tokens=200,
        cost_usd=0.0015
    )

    # Check today's spending
    spending = get_daily_spending()
    assert spending >= 0.012  # Should include our test costs

    # Check budget status
    budget = get_budget_status()
    assert 'daily_budget_usd' in budget
    assert 'today_spending_usd' in budget
    assert 'remaining_usd' in budget
    assert budget['today_spending_usd'] >= 0.012

    # Check cost summary
    summary = get_cost_summary(days=1)
    assert 'total_usd' in summary
    assert 'by_operation' in summary
    assert 'by_model' in summary
    assert summary['total_usd'] >= 0.012

    print(f"✓ Cost tracking working (today's spending: ${spending:.4f})\n")


def test_job_queue():
    """Test job queue management"""
    print("Testing job queue...")

    # Create a test job
    job_id = create_job('compile', {
        'raw_file_id': 'test-123',
        'model': 'sonnet'
    })
    assert job_id is not None

    # Check job status
    job = get_job_status(job_id)
    assert job is not None
    assert job['status'] == 'pending'
    assert job['type'] == 'compile'
    assert job['params']['raw_file_id'] == 'test-123'

    # Claim the job
    claimed = claim_next_job()
    assert claimed is not None
    assert claimed['id'] == job_id
    assert claimed['type'] == 'compile'

    # Update job status
    update_job_status(job_id, 'completed', result={'article_id': 'test-article'})

    # Verify update
    job = get_job_status(job_id)
    assert job['status'] == 'completed'
    assert job['result']['article_id'] == 'test-article'

    # List jobs
    jobs = list_jobs(limit=10)
    assert len(jobs) > 0
    assert any(j['id'] == job_id for j in jobs)

    print(f"✓ Job queue working (created job {job_id})\n")


def test_review_queue():
    """Test review queue management"""
    print("Testing review queue...")

    # Insert a test raw file first
    conn = get_connection()

    import uuid
    raw_file_id = str(uuid.uuid4())

    # Create a test raw file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write("# Test Article\n\nThis is a test.")
        test_file_path = f.name

    try:
        # Get just the filename for the filename column
        import os
        filename = os.path.basename(test_file_path)

        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO raw_files (id, filename, path, classification, checksum)
            VALUES (?, ?, ?, 'internal', 'test-checksum')
        """, (raw_file_id, filename, test_file_path))
        conn.execute("COMMIT")

        # Create a test review entry
        test_data = {
            'full_output': {
                'title': 'Test Article',
                'slug': 'test-article',
                'summary': 'A test article',
                'tags': ['test'],
                'content': '# Test\n\nContent here',
                'structural_changes': {
                    'requires_review': True,
                    'reason': 'New article',
                    'new_article': True
                }
            }
        }

        # Generate review ID
        review_id = str(uuid.uuid4())

        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO review_queue (id, action_type, raw_file_id, changes_summary, proposed_changes)
            VALUES (?, ?, ?, ?, ?)
        """, (review_id, 'compile', raw_file_id, 'Test review', json.dumps(test_data)))
        conn.execute("COMMIT")

        # List reviews
        reviews = list_pending_reviews()
        print(f"DEBUG: Created review_id: {review_id} (type: {type(review_id)})")
        print(f"DEBUG: Found {len(reviews)} reviews")
        if reviews:
            print(f"DEBUG: First review id: {reviews[0]['id']} (type: {type(reviews[0]['id'])})")
        assert len(reviews) > 0, f"No reviews found. Created review_id: {review_id}"
        assert any(r['id'] == review_id for r in reviews), f"Review {review_id} not found in: {[r['id'] for r in reviews]}"

        # Get review details
        review = get_review_details(review_id)
        assert review is not None
        assert review['status'] == 'pending'
        assert review['raw_file_id'] == raw_file_id

        # Get review stats
        stats = get_review_stats()
        assert stats['pending'] > 0

        # Test rejection
        success = reject_review(review_id, reason='Test rejection')
        assert success

        review = get_review_details(review_id)
        assert review['status'] == 'rejected'

        print(f"✓ Review queue working (created review {review_id})\n")

    finally:
        # Clean up test file
        if os.path.exists(test_file_path):
            os.unlink(test_file_path)


def test_cli_imports():
    """Test that all CLI modules can be imported"""
    print("Testing CLI module imports...")

    try:
        from kb.commands.compile import compile
        from kb.commands.review import review
        from kb.commands.jobs import jobs
        from kb.commands.costs import costs
        print("✓ All CLI command modules import successfully\n")
    except ImportError as e:
        print(f"✗ Import error: {e}\n")
        raise


def test_compile_infrastructure():
    """Test compilation infrastructure (without API call)"""
    print("Testing compilation infrastructure...")

    from kb.compile import load_prompt_template, inject_raw_content, detect_structural_changes

    # Test prompt template loading
    template = load_prompt_template('compile')
    assert '{{RAW_CONTENT}}' in template
    assert 'Knowledge Base Compilation Prompt' in template

    # Test content injection
    injected = inject_raw_content(template, 'Test content here')
    assert '{{RAW_CONTENT}}' not in injected
    assert 'Test content here' in injected

    # Test structural change detection
    test_output_with_review = {
        'structural_changes': {
            'requires_review': True,
            'new_article': True
        }
    }
    assert detect_structural_changes(test_output_with_review) == True

    test_output_no_review = {
        'structural_changes': {
            'requires_review': False
        }
    }
    assert detect_structural_changes(test_output_no_review) == False

    print("✓ Compilation infrastructure working\n")


def run_all_tests():
    """Run all Phase 2 tests"""
    print("=" * 60)
    print("PHASE 2 INTEGRATION TESTS")
    print("=" * 60)
    print()

    # Set up test database in temp directory
    test_dir = tempfile.mkdtemp(prefix='kb_test_phase2_')
    os.environ['KB_DIR'] = os.path.join(test_dir, '.kb')

    try:
        print(f"Test database: {os.environ['KB_DIR']}\n")

        # Initialize database
        print("Initializing test database...")
        init_database()
        print("✓ Database initialized\n")

        # Run migrations to get latest schema
        from kb.db import apply_migrations
        apply_migrations()
        print("✓ Migrations applied\n")

        # Run tests
        test_llm_functions()
        test_cost_tracking()
        test_job_queue()
        test_review_queue()
        test_cli_imports()
        test_compile_infrastructure()

        print("=" * 60)
        print("ALL PHASE 2 TESTS PASSED ✓")
        print("=" * 60)
        print()
        print("NOTE: Full end-to-end compilation test requires:")
        print("  1. Set ANTHROPIC_API_KEY environment variable")
        print("  2. Run: kb compile file <raw_file_id>")
        print()

        return True

    except Exception as e:
        print()
        print("=" * 60)
        print("TEST FAILED ✗")
        print("=" * 60)
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Clean up test database
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
        if 'KB_DIR' in os.environ:
            del os.environ['KB_DIR']


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
