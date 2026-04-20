"""RAG-based question answering interface"""

import logging
from typing import Dict, List, Any, Optional
import frontmatter
from kb.db import get_connection
from kb.search import search_articles
from kb.llm import call_llm, estimate_tokens
from kb.costs import record_cost, check_budget
from kb.errors import TransientError, PermanentError

logger = logging.getLogger(__name__)


def load_article_content(article_id: str) -> str:
    """
    Load full article content from file.

    Args:
        article_id: Article ID

    Returns:
        Article content (without frontmatter)
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT content_path FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        raise PermanentError(f"Article not found: {article_id}")

    content_path = row['content_path']

    try:
        with open(content_path, 'r') as f:
            doc = frontmatter.load(f)
            return doc.content
    except Exception as e:
        logger.error(f"Failed to load content for {article_id}: {e}")
        raise PermanentError(f"Failed to load article content: {e}") from e


def build_rag_context(
    question: str,
    max_articles: int = 5,
    max_tokens: int = 8000,
    agent_scope: Optional[str] = None
) -> tuple[List[Dict], str]:
    """
    Build RAG context by searching for relevant articles.

    Args:
        question: User question
        max_articles: Maximum articles to include
        max_tokens: Maximum tokens for context
        agent_scope: Filter by agent (NULL/shared articles always included)

    Returns:
        Tuple of (article list, context string)
    """
    # Search for relevant articles
    search_results = search_articles(question, limit=max_articles, agent_scope=agent_scope)

    if not search_results:
        return [], ""

    # Build context from search results
    context_parts = []
    total_tokens = 0
    articles_included = []

    for result in search_results:
        # Load article content
        try:
            content = load_article_content(result['id'])

            # Estimate tokens
            article_text = f"# {result['title']}\n\n{content}\n\n"
            article_tokens = estimate_tokens(article_text)

            # Check if we have room
            if total_tokens + article_tokens > max_tokens:
                logger.info(f"Reached token limit, stopping at {len(context_parts)} articles")
                break

            context_parts.append(article_text)
            total_tokens += article_tokens

            articles_included.append({
                'id': result['id'],
                'title': result['title'],
                'slug': result['slug'],
                'rank': result['rank']
            })

        except Exception as e:
            logger.warning(f"Failed to load article {result['id']}: {e}")
            continue

    context = "\n---\n\n".join(context_parts)

    return articles_included, context


def build_query_prompt(question: str, context: str) -> str:
    """
    Build prompt for LLM with question and context.

    Args:
        question: User question
        context: RAG context from articles

    Returns:
        Formatted prompt
    """
    if not context:
        # No context available
        return f"""You are a helpful assistant. Answer the following question to the best of your ability.

Question: {question}

If you don't have enough information to answer accurately, say so."""

    # With context
    return f"""You are a knowledge base assistant. Answer the question using ONLY the information provided in the context below. If the context doesn't contain enough information to answer the question, say so clearly.

Context from knowledge base:

{context}

Question: {question}

Instructions:
- Answer based only on the provided context
- Cite specific articles when making claims
- If the answer isn't in the context, say "I don't have enough information in the knowledge base to answer this question."
- Be concise but complete
"""


def query_knowledge_base(
    question: str,
    model: str = 'sonnet',
    max_articles: int = 5,
    cost_limit: float = 0.25,
    agent_scope: Optional[str] = None
) -> Dict[str, Any]:
    """
    Query knowledge base with RAG-based question answering.

    Workflow:
    1. Search for relevant articles
    2. Build context from top articles
    3. Check budget
    4. Call LLM with question + context
    5. Return answer with source citations

    Args:
        question: User question
        model: LLM model to use (haiku/sonnet/opus)
        max_articles: Maximum articles for context
        cost_limit: Maximum cost in USD

    Returns:
        {
            'answer': str,
            'sources': [{'id': ..., 'title': ..., 'slug': ...}],
            'cost_usd': float,
            'model': str,
            'context_used': bool
        }

    Raises:
        TransientError: For retryable failures
        PermanentError: For permanent failures
    """
    # Validate parameters
    max_articles = max(1, min(50, max_articles))
    cost_limit = max(0.01, min(10.0, cost_limit))

    logger.info(f"Query: {question}")

    # Build RAG context
    articles, context = build_rag_context(
        question,
        max_articles=max_articles,
        max_tokens=8000,
        agent_scope=agent_scope
    )

    # Build prompt
    prompt = build_query_prompt(question, context)

    # Estimate cost
    from kb.llm import estimate_tokens, calculate_cost, map_model_name
    estimated_input_tokens = estimate_tokens(prompt)
    estimated_output_tokens = 500  # Reasonable answer length
    model_id = map_model_name(model)
    estimated_cost = calculate_cost(model_id, estimated_input_tokens, estimated_output_tokens)

    # Check cost limit
    if estimated_cost > cost_limit:
        raise PermanentError(
            f"Estimated cost ${estimated_cost:.4f} exceeds limit ${cost_limit:.2f}. "
            f"Use --cost-limit to increase."
        )

    # Check budget
    check_budget(estimated_cost, hard_limit=True)

    logger.info(
        f"Querying with {model}, {len(articles)} articles, "
        f"~{estimated_input_tokens} tokens (est. ${estimated_cost:.4f})"
    )

    # Call LLM
    llm_response = call_llm(
        prompt=prompt,
        model=model,
        max_tokens=1024,
        temperature=0.0
    )

    # Record cost
    record_cost(
        operation='query',
        model=llm_response['model'],
        input_tokens=llm_response['input_tokens'],
        output_tokens=llm_response['output_tokens'],
        cost_usd=llm_response['cost_usd']
    )

    logger.info(
        f"Query complete: {llm_response['output_tokens']} tokens, "
        f"${llm_response['cost_usd']:.4f}"
    )

    return {
        'answer': llm_response['content'],
        'sources': articles,
        'cost_usd': llm_response['cost_usd'],
        'model': llm_response['model'],
        'context_used': len(articles) > 0
    }


def batch_query(
    questions: List[str],
    model: str = 'haiku',
    max_articles: int = 3
) -> List[Dict[str, Any]]:
    """
    Process multiple queries in batch.

    Args:
        questions: List of questions
        model: LLM model to use
        max_articles: Articles per query

    Returns:
        List of query results
    """
    results = []
    total_cost = 0.0

    for i, question in enumerate(questions, 1):
        logger.info(f"Processing query {i}/{len(questions)}")

        try:
            result = query_knowledge_base(
                question=question,
                model=model,
                max_articles=max_articles,
                cost_limit=0.10  # Lower limit for batch
            )

            results.append({
                'question': question,
                'result': result
            })

            total_cost += result['cost_usd']

        except Exception as e:
            logger.error(f"Query failed: {question}: {e}")
            results.append({
                'question': question,
                'error': str(e)
            })

    logger.info(f"Batch complete: {len(questions)} queries, ${total_cost:.4f}")

    return results


def suggest_questions(article_id: str, count: int = 5) -> List[str]:
    """
    Suggest questions that can be answered from an article.

    Args:
        article_id: Article ID
        count: Number of suggestions

    Returns:
        List of suggested questions
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT title, tags FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        return []

    import json
    tags = []
    if row['tags']:
        try:
            tags = json.loads(row['tags'])
        except:
            pass

    # Generate questions based on title and tags
    suggestions = []

    # "What is X?" question
    suggestions.append(f"What is {row['title']}?")

    # Tag-based questions
    for tag in tags[:3]:
        suggestions.append(f"How does {row['title']} relate to {tag}?")

    # Generic questions
    suggestions.append(f"What are the key concepts in {row['title']}?")
    suggestions.append(f"Can you explain {row['title']}?")

    return suggestions[:count]
