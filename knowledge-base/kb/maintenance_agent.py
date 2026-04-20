#!/usr/bin/env python3
"""
Knowledge Base Maintenance Agent

Runs automated maintenance tasks on the knowledge base.
Can be invoked via kimi CLI or run standalone.
"""

import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

# Add kb to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from kb.kimi_integration import KBClient


class MaintenanceAgent:
    """Automated maintenance agent for the knowledge base."""

    def __init__(self, kb_dir: str = None):
        self.kb = KBClient(kb_dir=kb_dir)
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "tasks": [],
            "summary": {
                "total_tasks": 0,
                "successful": 0,
                "failed": 0,
                "warnings": 0,
            }
        }

    def log_task(self, name: str, status: str, details: str = "", warning: bool = False):
        """Log a maintenance task result."""
        self.results["tasks"].append({
            "name": name,
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat(),
        })
        self.results["summary"]["total_tasks"] += 1
        if status == "success":
            self.results["summary"]["successful"] += 1
        elif status == "failed":
            self.results["summary"]["failed"] += 1
        if warning:
            self.results["summary"]["warnings"] += 1

    def check_health(self) -> Dict[str, Any]:
        """Check overall system health."""
        print("🏥 Checking system health...")

        result = self.kb.analytics(days=7, format="json")
        if not result["success"]:
            self.log_task("health_check", "failed", result.get("error", "Unknown error"))
            return {}

        data = result["data"]
        health = data["health"]
        health_score = health["health_score"]

        details = f"Health Score: {health_score}/100"
        warning = health_score < 80

        if warning:
            details += f"\n  Issues: {health['integrity']['total_issues']}"
            details += f"\n  Errors: {health['integrity']['errors']}"
            details += f"\n  Warnings: {health['integrity']['warnings']}"

        self.log_task("health_check", "success", details, warning=warning)

        print(f"  Health Score: {health_score}/100")
        if warning:
            print(f"  ⚠️  System health below 80")

        return health

    def run_integrity_checks(self) -> Dict[str, Any]:
        """Run integrity checks."""
        print("\n🔍 Running integrity checks...")

        result = self.kb.integrity_check()
        if not result["success"]:
            self.log_task("integrity_check", "failed", result.get("error", "Unknown error"))
            return {}

        data = result["data"]
        total_issues = data.get("total_issues", 0)

        details = f"Total Issues: {total_issues}"
        if total_issues > 0:
            details += f"\n  By Severity:"
            for severity, count in data["by_severity"].items():
                if count > 0:
                    details += f"\n    {severity}: {count}"

        warning = total_issues > 5
        self.log_task("integrity_check", "success", details, warning=warning)

        print(f"  Total Issues: {total_issues}")
        if total_issues > 0:
            print(f"  Fixable: {data.get('fixable', 0)}")

        return data

    def auto_fix_issues(self, integrity_data: Dict[str, Any]) -> int:
        """Auto-fix deterministic issues."""
        fixable = integrity_data.get("fixable", 0)

        if fixable == 0:
            print("\n✓ No auto-fixable issues")
            self.log_task("auto_fix", "success", "No issues to fix")
            return 0

        print(f"\n🔧 Auto-fixing {fixable} issue(s)...")

        result = self.kb.auto_fix()
        if result["success"]:
            details = f"Fixed {fixable} issue(s)"
            self.log_task("auto_fix", "success", details)
            print(f"  ✓ Fixed {fixable} issue(s)")
            return fixable
        else:
            self.log_task("auto_fix", "failed", result.get("error", "Unknown error"))
            print(f"  ✗ Auto-fix failed")
            return 0

    def scan_for_pii(self) -> Dict[str, Any]:
        """Scan all articles for PII."""
        print("\n🔒 Scanning for PII...")

        result = self.kb.scan_all_pii(confidence=0.8)
        if not result["success"]:
            self.log_task("pii_scan", "failed", result.get("error", "Unknown error"))
            return {}

        data = result["data"]
        articles_with_pii = data.get("articles_with_pii", 0)

        details = f"Articles with PII: {articles_with_pii}"
        warning = articles_with_pii > 0

        self.log_task("pii_scan", "success", details, warning=warning)

        print(f"  Articles with PII: {articles_with_pii}")
        if warning:
            print(f"  ⚠️  PII detected - review recommended")

        return data

    def check_costs(self) -> Dict[str, Any]:
        """Check LLM API costs."""
        print("\n💰 Checking costs (7 days)...")

        result = self.kb.get_costs(days=7)
        if not result["success"]:
            self.log_task("cost_check", "failed", result.get("error", "Unknown error"))
            return {}

        data = result["data"]
        total_usd = data.get("total_usd", 0)

        details = f"Total: ${total_usd:.4f}"
        if "by_operation" in data:
            details += "\n  By Operation:"
            for op, cost in data["by_operation"].items():
                details += f"\n    {op}: ${cost:.4f}"

        warning = total_usd > 1.0  # Warn if over $1
        self.log_task("cost_check", "success", details, warning=warning)

        print(f"  Total: ${total_usd:.4f}")
        if warning:
            print(f"  ⚠️  Costs exceed $1.00")

        return data

    def check_worker(self) -> Dict[str, Any]:
        """Check background worker status."""
        print("\n⚙️  Checking worker...")

        result = self.kb.worker_status()
        if not result["success"]:
            self.log_task("worker_check", "failed", result.get("error", "Unknown error"))
            return {}

        data = result["data"]
        is_running = data.get("running", False)

        details = f"Running: {is_running}"
        if is_running:
            details += f"\n  PID: {data.get('pid')}"

        warning = not is_running
        self.log_task("worker_check", "success", details, warning=warning)

        print(f"  Running: {is_running}")
        if warning:
            print(f"  ⚠️  Worker not running - automation disabled")

        return data

    def generate_summary(self) -> str:
        """Generate maintenance summary report."""
        summary = self.results["summary"]

        report = "\n" + "="*60 + "\n"
        report += "MAINTENANCE SUMMARY\n"
        report += "="*60 + "\n\n"
        report += f"Timestamp: {self.results['timestamp']}\n"
        report += f"Total Tasks: {summary['total_tasks']}\n"
        report += f"Successful: {summary['successful']}\n"
        report += f"Failed: {summary['failed']}\n"
        report += f"Warnings: {summary['warnings']}\n"

        if summary['warnings'] > 0:
            report += "\n⚠️  WARNINGS DETECTED - Review recommended\n"

        if summary['failed'] > 0:
            report += "\n✗ FAILURES DETECTED - Manual intervention required\n"
            report += "\nFailed Tasks:\n"
            for task in self.results["tasks"]:
                if task["status"] == "failed":
                    report += f"  - {task['name']}: {task['details']}\n"

        if summary['failed'] == 0 and summary['warnings'] == 0:
            report += "\n✓ All systems healthy!\n"

        report += "\n" + "="*60

        return report

    def run_full_maintenance(self, auto_fix: bool = True) -> Dict[str, Any]:
        """
        Run full maintenance cycle.

        Args:
            auto_fix: Whether to auto-fix issues

        Returns:
            Maintenance results
        """
        print("🤖 Knowledge Base Maintenance Agent\n")
        print("="*60)

        # 1. Health check
        health = self.check_health()

        # 2. Integrity checks
        integrity = self.run_integrity_checks()

        # 3. Auto-fix if enabled
        if auto_fix and integrity:
            self.auto_fix_issues(integrity)

        # 4. PII scan
        self.scan_for_pii()

        # 5. Cost check
        self.check_costs()

        # 6. Worker check
        self.check_worker()

        # Generate summary
        summary = self.generate_summary()
        print(summary)

        return self.results


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="KB Maintenance Agent")
    parser.add_argument("--kb-dir", help="Knowledge base directory")
    parser.add_argument("--no-auto-fix", action="store_true", help="Skip auto-fix")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    agent = MaintenanceAgent(kb_dir=args.kb_dir)
    results = agent.run_full_maintenance(auto_fix=not args.no_auto_fix)

    if args.json:
        print("\n" + json.dumps(results, indent=2))

    # Exit with error if failures
    if results["summary"]["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
