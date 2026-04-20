"""Kimi CLI integration for knowledge base operations.

Provides a simple interface for kimi to interact with the knowledge base
via subprocess calls, similar to how HalOS invokes other CLIs.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any


class KBClient:
    """Client for interacting with the knowledge base via kimi CLI."""

    def __init__(self, kb_dir: Optional[str] = None):
        """
        Initialize KB client.

        Args:
            kb_dir: Knowledge base directory (default: current directory)
        """
        self.kb_dir = kb_dir or str(Path.cwd())
        self.python = "python3"

    def _run_command(self, args: List[str], capture_json: bool = False) -> Dict[str, Any]:
        """
        Run a kb CLI command.

        Args:
            args: Command arguments (e.g., ["search", "query"])
            capture_json: If True, parse output as JSON

        Returns:
            Result dictionary with output, error, returncode
        """
        cmd = [self.python, "-m", "kb.cli"] + args

        # Set KB_DIR if specified
        env = None
        if self.kb_dir:
            import os
            env = os.environ.copy()
            env["KB_DIR"] = self.kb_dir

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.kb_dir,
                env=env,
                timeout=120,
            )

            output = result.stdout.strip()
            error = result.stderr.strip()

            if capture_json and result.returncode == 0:
                try:
                    data = json.loads(output)
                    return {"success": True, "data": data, "error": None}
                except json.JSONDecodeError:
                    return {"success": False, "data": None, "error": "Invalid JSON output"}

            return {
                "success": result.returncode == 0,
                "output": output,
                "error": error if result.returncode != 0 else None,
                "returncode": result.returncode,
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out after 120 seconds"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # === Search & Query ===

    def search(self, query: str, tag: Optional[str] = None,
               classification: Optional[str] = None,
               format: str = "json") -> Dict[str, Any]:
        """
        Search wiki articles.

        Args:
            query: Search query
            tag: Filter by tag
            classification: Filter by classification (public/internal/confidential)
            format: Output format (text or json)

        Returns:
            Search results
        """
        args = ["search", query, "--format", format]
        if tag:
            args.extend(["--tag", tag])
        if classification:
            args.extend(["--classification", classification])

        return self._run_command(args, capture_json=(format == "json"))

    def ask(self, question: str, max_articles: int = 5,
            cost_limit: float = 0.25) -> Dict[str, Any]:
        """
        Ask a question using RAG.

        Args:
            question: Question to ask
            max_articles: Max articles to include in context
            cost_limit: Max cost in USD

        Returns:
            Answer with sources
        """
        args = [
            "query", "ask", question,
            "--max-articles", str(max_articles),
            "--cost-limit", str(cost_limit),
            "--output", "json",
        ]

        return self._run_command(args, capture_json=True)

    def related_articles(self, article_id: str, limit: int = 5) -> Dict[str, Any]:
        """
        Find related articles.

        Args:
            article_id: Article UUID or slug
            limit: Max results

        Returns:
            Related articles
        """
        args = ["query", "related", article_id, "--limit", str(limit), "--output", "json"]
        return self._run_command(args, capture_json=True)

    # === Ingestion ===

    def ingest_file(self, filepath: str,
                    classification: str = "internal") -> Dict[str, Any]:
        """
        Ingest a local file.

        Args:
            filepath: Path to file
            classification: Classification level

        Returns:
            Ingest result with file ID
        """
        args = ["ingest", "file", filepath, "--classification", classification]
        return self._run_command(args)

    def ingest_url(self, url: str,
                   classification: str = "internal") -> Dict[str, Any]:
        """
        Ingest content from URL.

        Args:
            url: URL to ingest
            classification: Classification level

        Returns:
            Ingest result
        """
        args = ["ingest", "url", url, "--classification", classification]
        return self._run_command(args)

    # === Compilation ===

    def compile_file(self, file_id: str, model: str = "sonnet",
                     auto_approve: bool = False) -> Dict[str, Any]:
        """
        Compile a raw file to wiki article.

        Args:
            file_id: Raw file UUID
            model: LLM model (haiku/sonnet/opus)
            auto_approve: Skip review queue

        Returns:
            Compilation result
        """
        args = ["compile", "file", file_id, "--model", model]
        if auto_approve:
            args.append("--auto-approve")

        return self._run_command(args)

    def compile_all(self, model: str = "sonnet") -> Dict[str, Any]:
        """
        Compile all pending raw files.

        Args:
            model: LLM model

        Returns:
            Compilation results
        """
        args = ["compile", "all", "--model", model]
        return self._run_command(args)

    # === Review Queue ===

    def review_list(self, format: str = "json") -> Dict[str, Any]:
        """
        List pending reviews.

        Args:
            format: Output format

        Returns:
            List of pending reviews
        """
        args = ["review", "list", "--output", format]
        return self._run_command(args, capture_json=(format == "json"))

    def review_approve(self, review_id: str) -> Dict[str, Any]:
        """
        Approve a review.

        Args:
            review_id: Review UUID

        Returns:
            Approval result
        """
        args = ["review", "approve", review_id]
        return self._run_command(args)

    def review_reject(self, review_id: str, reason: str = "") -> Dict[str, Any]:
        """
        Reject a review.

        Args:
            review_id: Review UUID
            reason: Rejection reason

        Returns:
            Rejection result
        """
        args = ["review", "reject", review_id]
        if reason:
            args.extend(["--reason", reason])

        return self._run_command(args)

    # === Analytics & Health ===

    def analytics(self, days: int = 7, format: str = "json") -> Dict[str, Any]:
        """
        Generate analytics report.

        Args:
            days: Number of days to analyze
            format: Output format

        Returns:
            Analytics report
        """
        args = ["analytics", "--days", str(days), "--output", format]
        return self._run_command(args, capture_json=(format == "json"))

    def integrity_check(self) -> Dict[str, Any]:
        """
        Run integrity checks.

        Returns:
            Check results
        """
        args = ["lint", "--output", "json"]
        return self._run_command(args, capture_json=True)

    def auto_fix(self) -> Dict[str, Any]:
        """
        Auto-fix integrity issues.

        Returns:
            Fix results
        """
        args = ["lint", "--fix"]
        return self._run_command(args)

    # === PII Detection ===

    def scan_file_for_pii(self, filepath: str,
                          confidence: float = 0.7) -> Dict[str, Any]:
        """
        Scan file for PII.

        Args:
            filepath: Path to file
            confidence: Confidence threshold

        Returns:
            PII scan results
        """
        args = [
            "pii", "scan-file", filepath,
            "--confidence", str(confidence),
            "--output", "json",
        ]
        return self._run_command(args, capture_json=True)

    def scan_all_pii(self, confidence: float = 0.7) -> Dict[str, Any]:
        """
        Scan all articles for PII.

        Args:
            confidence: Confidence threshold

        Returns:
            Scan results
        """
        args = [
            "pii", "scan-all",
            "--confidence", str(confidence),
            "--output", "json",
        ]
        return self._run_command(args, capture_json=True)

    # === Retention ===

    def apply_retention(self, dry_run: bool = True) -> Dict[str, Any]:
        """
        Apply retention policies.

        Args:
            dry_run: Preview without applying

        Returns:
            Retention results
        """
        args = ["retention", "run", "--output", "json"]
        if dry_run:
            args.append("--dry-run")

        return self._run_command(args, capture_json=True)

    # === Worker ===

    def worker_status(self) -> Dict[str, Any]:
        """
        Get worker status.

        Returns:
            Worker status
        """
        args = ["worker", "status", "--output", "json"]
        return self._run_command(args, capture_json=True)

    def worker_start(self, daemon: bool = True) -> Dict[str, Any]:
        """
        Start background worker.

        Args:
            daemon: Run as daemon

        Returns:
            Start result
        """
        args = ["worker", "start"]
        if daemon:
            args.append("--daemon")

        return self._run_command(args)

    def worker_stop(self) -> Dict[str, Any]:
        """
        Stop background worker.

        Returns:
            Stop result
        """
        args = ["worker", "stop"]
        return self._run_command(args)

    # === Costs ===

    def get_costs(self, days: int = 7) -> Dict[str, Any]:
        """
        Get cost summary.

        Args:
            days: Number of days

        Returns:
            Cost summary
        """
        args = ["costs", "summary", "--days", str(days), "--output", "json"]
        return self._run_command(args, capture_json=True)
