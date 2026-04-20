#!/usr/bin/env python3
"""
Example: Using the Knowledge Base from kimi/HalOS

This script demonstrates how kimi agents can interact with the knowledge base.
Copy this pattern to your agent's working directory or reference directly.
"""

import sys
from pathlib import Path

# Add knowledge base to path
kb_path = Path(__file__).parent.parent
sys.path.insert(0, str(kb_path))

from kb.kimi_integration import KBClient


def example_search():
    """Example: Search for articles"""
    print("=== Example: Search ===\n")

    kb = KBClient()
    result = kb.search("knowledge base", format="json")

    if result["success"] and result.get("data"):
        articles = result["data"]
        print(f"Found {len(articles)} articles:")
        for article in articles[:5]:
            print(f"  • {article['title']} (slug: {article['slug']})")
    else:
        print("No articles found (index may be empty)")


def example_ask():
    """Example: Ask a question with RAG"""
    print("\n=== Example: Ask Question ===\n")

    kb = KBClient()
    result = kb.ask("What are the key features of the knowledge base system?")

    if result["success"]:
        data = result["data"]
        print(f"Answer: {data['answer']}\n")
        print("Sources:")
        for source in data.get("sources", []):
            print(f"  • {source['title']}")
        print(f"\nCost: ${data['cost']:.4f}")
    else:
        print(f"Error: {result.get('error', 'Unknown error')}")


def example_analytics():
    """Example: Get system health"""
    print("\n=== Example: Analytics ===\n")

    kb = KBClient()
    result = kb.analytics(days=7, format="json")

    if result["success"]:
        data = result["data"]
        health = data["health"]

        print(f"Health Score: {health['health_score']}/100")
        print(f"Total Articles: {health['articles']}")
        print(f"\nIntegrity:")
        print(f"  Total Issues: {health['integrity']['total_issues']}")
        print(f"  Errors: {health['integrity']['errors']}")
        print(f"  Warnings: {health['integrity']['warnings']}")
        print(f"  Auto-fixable: {health['integrity']['fixable']}")

        costs = data["costs"]
        print(f"\nCosts (7 days):")
        print(f"  Total: ${costs.get('total_usd', 0):.2f}")
        if "by_operation" in costs:
            print(f"  By Operation:")
            for op, cost in costs["by_operation"].items():
                print(f"    {op}: ${cost:.4f}")
    else:
        print(f"Error: {result.get('error', 'Unknown error')}")


def example_ingest():
    """Example: Ingest a file"""
    print("\n=== Example: Ingest File ===\n")

    # Create a temporary file to ingest
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write("# Test Document\n\nThis is a test document for ingestion.\n")
        temp_path = f.name

    kb = KBClient()
    result = kb.ingest_file(temp_path, classification="public")

    if result["success"]:
        print("✓ File ingested successfully")
        print(result["output"])
    else:
        print(f"✗ Ingestion failed: {result.get('error')}")

    # Clean up
    import os
    os.unlink(temp_path)


def example_workflow():
    """Example: Complete workflow"""
    print("\n=== Example: Complete Workflow ===\n")

    kb = KBClient()

    # 1. Check system health
    print("1. Checking system health...")
    health = kb.analytics(days=7)
    if health["success"]:
        score = health["data"]["health"]["health_score"]
        print(f"   Health score: {score}/100")

    # 2. Search for documentation
    print("\n2. Searching for documentation...")
    results = kb.search("authentication", format="json")
    if results["success"] and results.get("data"):
        print(f"   Found {len(results['data'])} articles")
    else:
        print("   No articles found")

    # 3. Check for PII in all articles
    print("\n3. Scanning for PII...")
    pii_scan = kb.scan_all_pii(confidence=0.8)
    if pii_scan["success"]:
        articles_with_pii = pii_scan["data"].get("articles_with_pii", 0)
        print(f"   Articles with PII: {articles_with_pii}")

    # 4. Check worker status
    print("\n4. Checking worker status...")
    worker = kb.worker_status()
    if worker["success"]:
        status = worker["data"]
        is_running = status.get("running", False)
        print(f"   Worker running: {is_running}")
        if is_running:
            print(f"   PID: {status.get('pid')}")

    print("\n✓ Workflow complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="KB integration examples")
    parser.add_argument("--search", action="store_true", help="Run search example")
    parser.add_argument("--ask", action="store_true", help="Run Q&A example (requires API key)")
    parser.add_argument("--analytics", action="store_true", help="Run analytics example")
    parser.add_argument("--ingest", action="store_true", help="Run ingestion example")
    parser.add_argument("--workflow", action="store_true", help="Run complete workflow")
    parser.add_argument("--all", action="store_true", help="Run all examples")

    args = parser.parse_args()

    if args.all or not any([args.search, args.ask, args.analytics, args.ingest, args.workflow]):
        # Run all if no specific example selected
        example_search()
        # example_ask()  # Requires API key
        example_analytics()
        example_ingest()
        example_workflow()
    else:
        if args.search:
            example_search()
        if args.ask:
            example_ask()
        if args.analytics:
            example_analytics()
        if args.ingest:
            example_ingest()
        if args.workflow:
            example_workflow()
