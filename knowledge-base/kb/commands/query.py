"""CLI commands for question answering"""

import click
import json
from kb.query import query_knowledge_base, batch_query
from kb.errors import TransientError, PermanentError


@click.command()
@click.argument('question')
@click.option('--model', type=click.Choice(['haiku', 'sonnet', 'opus']),
              default='sonnet', help='LLM model to use')
@click.option('--articles', type=int, default=5, help='Max articles for context')
@click.option('--cost-limit', type=float, default=0.25, help='Max cost in USD')
@click.option('--output', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
def query(question: str, model: str, articles: int, cost_limit: float, output: str):
    """
    Ask a question against the knowledge base.

    Uses RAG (Retrieval-Augmented Generation) to search for relevant
    articles and answer questions based on knowledge base content.

    Example:
        kb query "What is machine learning?"
        kb query "Explain neural networks" --model haiku
    """
    try:
        result = query_knowledge_base(
            question=question,
            model=model,
            max_articles=articles,
            cost_limit=cost_limit
        )

        if output == 'json':
            click.echo(json.dumps(result, indent=2))
        else:
            # Text output
            click.echo("Answer:")
            click.echo("-" * 60)
            click.echo(result['answer'])
            click.echo("-" * 60)
            click.echo()

            if result['sources']:
                click.echo(f"Sources ({len(result['sources'])} articles):")
                for source in result['sources']:
                    click.echo(f"  • {source['title']} ({source['slug']})")
                click.echo()
            else:
                click.echo("Sources: No relevant articles found (answered from general knowledge)")
                click.echo()

            click.echo(f"Cost: ${result['cost_usd']:.4f}")
            click.echo(f"Model: {result['model']}")

    except PermanentError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()

    except TransientError as e:
        click.echo(f"✗ Transient error: {e}", err=True)
        click.echo("  Retry the operation", err=True)
        raise click.Abort()


def register(cli):
    """Register query command with main CLI"""
    cli.add_command(query)
