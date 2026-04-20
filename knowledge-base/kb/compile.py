"""Compilation logic for converting raw files into wiki articles"""

import json
import logging
import os
from typing import Dict, Any, Optional
from datetime import datetime
from kb.db import get_connection
from kb.llm import call_llm, parse_llm_output
from kb.costs import record_cost, check_budget
from kb.search import update_article_fts
from kb.errors import TransientError, PermanentError

logger = logging.getLogger(__name__)


def load_prompt_template(template_name: str = 'compile') -> str:
    """
    Load prompt template from prompts/ directory.

    Args:
        template_name: Template name (without .md extension)

    Returns:
        Template content as string
    """
    # Get project root (parent of kb/ directory)
    kb_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(kb_dir)
    template_path = os.path.join(project_root, 'prompts', f'{template_name}.md')

    if not os.path.exists(template_path):
        raise PermanentError(f"Prompt template not found: {template_path}")

    with open(template_path, 'r') as f:
        return f.read()


def inject_raw_content(template: str, raw_content: str) -> str:
    """
    Inject raw content into prompt template.

    Args:
        template: Prompt template with {{RAW_CONTENT}} placeholder
        raw_content: Content to inject

    Returns:
        Final prompt with content injected
    """
    return template.replace('{{RAW_CONTENT}}', raw_content)


def detect_structural_changes(parsed_output: Dict[str, Any]) -> bool:
    """
    Check if LLM output indicates structural changes requiring review.

    Args:
        parsed_output: Parsed JSON from LLM

    Returns:
        True if human review required
    """
    structural_changes = parsed_output.get('structural_changes', {})
    return structural_changes.get('requires_review', False)


def create_review_entry(
    raw_file_id: str,
    compilation_result: Dict[str, Any],
    job_id: Optional[str] = None
) -> str:
    """
    Create review queue entry for structural changes.

    Args:
        raw_file_id: Source raw file ID
        compilation_result: Parsed LLM output
        job_id: Optional job ID reference

    Returns:
        Review entry ID
    """
    import uuid
    conn = get_connection()

    # Generate review ID
    review_id = str(uuid.uuid4())

    structural_changes = compilation_result.get('structural_changes', {})
    reason = structural_changes.get('reason', 'Structural changes detected')

    # Extract relevant data for review
    review_data = {
        'raw_file_id': raw_file_id,
        'title': compilation_result['title'],
        'slug': compilation_result['slug'],
        'summary': compilation_result['summary'],
        'structural_changes': structural_changes,
        'full_output': compilation_result
    }

    conn.execute("BEGIN")

    try:
        conn.execute("""
            INSERT INTO review_queue (id, action_type, raw_file_id, changes_summary, proposed_changes, job_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (review_id, 'compile', raw_file_id, reason, json.dumps(review_data), job_id))

        conn.execute("COMMIT")

        logger.info(f"Created review entry: {review_id} for raw file {raw_file_id}")

        return review_id

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def apply_compilation_result(
    raw_file_id: str,
    compilation_result: Dict[str, Any],
    auto_approve: bool = False
) -> str:
    """
    Apply compilation result to create or update wiki article.

    Args:
        raw_file_id: Source raw file ID
        compilation_result: Parsed LLM output
        auto_approve: If True, skip review and directly apply

    Returns:
        Article ID
    """
    conn = get_connection()

    # Get raw file info for classification
    cursor = conn.execute("""
        SELECT classification FROM raw_files WHERE id = ?
    """, (raw_file_id,))
    row = cursor.fetchone()
    if not row:
        raise PermanentError(f"Raw file not found: {raw_file_id}")

    classification = row['classification']

    # Extract article data
    title = compilation_result['title']
    slug = compilation_result['slug']
    summary = compilation_result.get('summary', '')
    tags = compilation_result.get('tags', [])
    content = compilation_result['content']

    # Determine article ID (check if slug exists)
    cursor = conn.execute("""
        SELECT id FROM articles WHERE slug = ?
    """, (slug,))
    existing = cursor.fetchone()

    if existing:
        article_id = existing['id']
        logger.info(f"Updating existing article: {article_id} ({slug})")
    else:
        import uuid
        article_id = str(uuid.uuid4())
        logger.info(f"Creating new article: {article_id} ({slug})")

    # Prepare wiki file content
    kb_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(kb_dir)
    wiki_dir = os.path.join(project_root, 'wiki', classification)
    os.makedirs(wiki_dir, exist_ok=True)

    content_path = os.path.join(wiki_dir, f'{slug}.md')

    import frontmatter
    doc = frontmatter.Post(content)
    doc.metadata = {
        'id': article_id,
        'title': title,
        'slug': slug,
        'summary': summary,
        'tags': tags,
        'classification': classification,
        'source_file': raw_file_id,
        'compiled_at': datetime.now().isoformat()
    }

    import hashlib
    checksum = hashlib.sha256(content.encode()).hexdigest()
    file_content = frontmatter.dumps(doc)

    # Write to temp file first (same dir for atomic rename)
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=wiki_dir, suffix='.md.tmp')

    try:
        with os.fdopen(tmp_fd, 'w') as f:
            f.write(file_content)

        # Upsert article in database
        conn.execute("BEGIN IMMEDIATE")

        if existing:
            conn.execute("""
                UPDATE articles
                SET title = ?, content_path = ?, classification = ?,
                    tags = ?, checksum = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (title, content_path, classification, json.dumps(tags), checksum, article_id))
        else:
            conn.execute("""
                INSERT INTO articles (id, title, slug, content_path, classification, tags, checksum, last_confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (article_id, title, slug, content_path, classification, json.dumps(tags), checksum))

        # Atomic rename temp → final (prevents TOCTOU race)
        os.replace(tmp_path, content_path)
        tmp_path = None  # Mark as moved

        # Update FTS index (does not commit)
        update_article_fts(conn, article_id)

        conn.execute("COMMIT")

        logger.info(f"Applied compilation result for article: {article_id}")
        return article_id

    except Exception as e:
        conn.execute("ROLLBACK")
        # Clean up temp file if rename didn't happen
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def compile_raw_file(
    raw_file_id: str,
    model: str = 'sonnet',
    job_id: Optional[str] = None,
    auto_approve: bool = False
) -> Dict[str, Any]:
    """
    Compile a raw file into a wiki article using LLM.

    Workflow:
    1. Load raw file content
    2. Check budget
    3. Call LLM with compilation prompt
    4. Parse JSON response
    5. Record cost
    6. Check if structural changes require review
    7. If review needed: create review entry
    8. If auto-approve or no review needed: apply changes

    Args:
        raw_file_id: Raw file ID to compile
        model: LLM model to use (haiku/sonnet/opus)
        job_id: Optional job ID for cost tracking
        auto_approve: If True, skip review queue

    Returns:
        {
            'article_id': str (if applied),
            'review_id': str (if awaiting review),
            'status': 'applied' | 'awaiting_review',
            'cost_usd': float
        }

    Raises:
        TransientError: For retryable failures
        PermanentError: For permanent failures
    """
    conn = get_connection()

    # Load raw file
    cursor = conn.execute("""
        SELECT path, classification FROM raw_files WHERE id = ?
    """, (raw_file_id,))
    row = cursor.fetchone()

    if not row:
        raise PermanentError(f"Raw file not found: {raw_file_id}")

    file_path = row['path']

    if not os.path.exists(file_path):
        raise PermanentError(f"Raw file content not found: {file_path}")

    with open(file_path, 'r') as f:
        raw_content = f.read()

    # Load prompt template and inject content
    template = load_prompt_template('compile')
    prompt = inject_raw_content(template, raw_content)

    # Estimate cost (rough)
    from kb.llm import estimate_tokens, calculate_cost, map_model_name
    estimated_input_tokens = estimate_tokens(prompt)
    estimated_output_tokens = 2000  # Rough estimate for JSON output
    model_id = map_model_name(model)
    estimated_cost = calculate_cost(model_id, estimated_input_tokens, estimated_output_tokens)

    # Check budget before calling LLM
    check_budget(estimated_cost, hard_limit=True)

    logger.info(
        f"Compiling raw file {raw_file_id} with {model} "
        f"(estimated cost: ${estimated_cost:.4f})"
    )

    # Call LLM
    llm_response = call_llm(
        prompt=prompt,
        model=model,
        max_tokens=4096,
        temperature=0.0
    )

    # Parse JSON output
    parsed_output = parse_llm_output(llm_response['content'], expected_format='json')

    # Record cost
    record_cost(
        operation='compile',
        model=llm_response['model'],
        input_tokens=llm_response['input_tokens'],
        output_tokens=llm_response['output_tokens'],
        cost_usd=llm_response['cost_usd'],
        job_id=job_id
    )

    # Check if structural changes require review
    requires_review = detect_structural_changes(parsed_output)

    if requires_review and not auto_approve:
        # Create review entry
        review_id = create_review_entry(raw_file_id, parsed_output, job_id)

        logger.info(
            f"Compilation requires review: {review_id} "
            f"(cost: ${llm_response['cost_usd']:.4f})"
        )

        return {
            'review_id': review_id,
            'status': 'awaiting_review',
            'cost_usd': llm_response['cost_usd']
        }
    else:
        # Apply changes directly
        article_id = apply_compilation_result(raw_file_id, parsed_output, auto_approve)

        logger.info(
            f"Compilation applied: {article_id} "
            f"(cost: ${llm_response['cost_usd']:.4f})"
        )

        return {
            'article_id': article_id,
            'status': 'applied',
            'cost_usd': llm_response['cost_usd']
        }


def compile_all_pending(model: str = 'sonnet', limit: int = 10) -> Dict[str, Any]:
    """
    Compile all raw files that haven't been compiled yet.

    Args:
        model: LLM model to use
        limit: Maximum number of files to compile in one run

    Returns:
        {
            'compiled': int,
            'awaiting_review': int,
            'failed': int,
            'total_cost_usd': float
        }
    """
    conn = get_connection()

    # Find raw files without corresponding articles
    cursor = conn.execute("""
        SELECT rf.id
        FROM raw_files rf
        LEFT JOIN articles a ON a.source_file = rf.id
        WHERE a.id IS NULL
        LIMIT ?
    """, (limit,))

    pending_files = [row['id'] for row in cursor]

    logger.info(f"Found {len(pending_files)} raw files to compile")

    compiled = 0
    awaiting_review = 0
    failed = 0
    total_cost = 0.0

    for raw_file_id in pending_files:
        try:
            result = compile_raw_file(raw_file_id, model=model)
            total_cost += result['cost_usd']

            if result['status'] == 'applied':
                compiled += 1
            elif result['status'] == 'awaiting_review':
                awaiting_review += 1

        except PermanentError as e:
            logger.error(f"Failed to compile {raw_file_id}: {e}")
            # Clean up orphaned raw_files record — compilation will never succeed
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM raw_files WHERE id = ?", (raw_file_id,))
                conn.execute("COMMIT")
                logger.info(f"Cleaned up orphaned raw_files record: {raw_file_id}")
            except Exception as cleanup_err:
                conn.execute("ROLLBACK")
                logger.warning(f"Failed to clean up raw_files {raw_file_id}: {cleanup_err}")
            failed += 1
        except TransientError as e:
            logger.warning(f"Transient error compiling {raw_file_id}: {e}")
            failed += 1

    summary = {
        'compiled': compiled,
        'awaiting_review': awaiting_review,
        'failed': failed,
        'total_cost_usd': total_cost
    }

    logger.info(
        f"Compilation batch complete: "
        f"{compiled} applied, {awaiting_review} awaiting review, "
        f"{failed} failed (${total_cost:.4f})"
    )

    return summary
