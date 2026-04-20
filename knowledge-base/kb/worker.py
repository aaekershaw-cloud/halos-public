"""Background worker for automated tasks"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import threading

from kb.jobs import claim_next_job, execute_job_with_retry
from kb.compile import compile_raw_file
from kb.lint import run_all_checks
from kb.retention import apply_retention_policies, cleanup_expired_soft_deletes
from kb.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)


class Worker:
    """Background worker for processing jobs and scheduled tasks"""

    def __init__(self):
        self.running = False
        self.shutdown_requested = False
        self.job_poll_interval = 5  # seconds
        self.maintenance_interval = 3600  # 1 hour
        self.last_maintenance = None
        self.stats = {
            'jobs_processed': 0,
            'jobs_failed': 0,
            'maintenance_runs': 0,
            'started_at': None
        }

    def start(self, daemon: bool = False):
        """
        Start the worker process.

        Args:
            daemon: If True, run as background daemon
        """
        if self.running:
            logger.warning("Worker already running")
            return

        self.running = True
        self.shutdown_requested = False
        self.stats['started_at'] = datetime.now().isoformat()

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("Starting knowledge base worker...")

        if daemon:
            self._daemonize()
        else:
            # Foreground mode (e.g. under launchd): still publish PID file
            # so `kb worker status` reports correctly regardless of how we were started.
            pid_file = os.path.expanduser('~/.kb/worker.pid')
            os.makedirs(os.path.dirname(pid_file), exist_ok=True)
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))

        try:
            self._run_loop()
        except Exception as e:
            logger.error(f"Worker crashed: {e}", exc_info=True)
            self.running = False
            raise
        finally:
            pid_file = os.path.expanduser('~/.kb/worker.pid')
            try:
                if os.path.exists(pid_file):
                    with open(pid_file, 'r') as f:
                        if f.read().strip() == str(os.getpid()):
                            os.remove(pid_file)
            except OSError:
                pass

    def stop(self):
        """Request graceful shutdown"""
        if not self.running:
            logger.warning("Worker not running")
            return

        logger.info("Shutdown requested, finishing current tasks...")
        self.shutdown_requested = True

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()

    def _daemonize(self):
        """Daemonize the process (Unix double-fork)"""
        pid_file = os.path.expanduser('~/.kb/worker.pid')

        try:
            # Fork first child
            pid = os.fork()
            if pid > 0:
                # Original parent — wait for PID file, print it, exit
                for _ in range(20):
                    time.sleep(0.1)
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            daemon_pid = f.read().strip()
                        print(f"✓ Worker started (PID: {daemon_pid})")
                        break
                sys.exit(0)

            # Decouple from parent
            os.chdir('/')
            os.setsid()
            os.umask(0)

            # Fork second child
            pid = os.fork()
            if pid > 0:
                sys.exit(0)

            # Redirect standard file descriptors to a log file so the daemon
            # survives the parent TTY closing. Without this, the first log write
            # after fork hits a dead fd and the worker crashes silently.
            log_path = os.path.expanduser('~/.kb/worker.log')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            sys.stdout.flush()
            sys.stderr.flush()
            with open(os.devnull, 'r') as devnull_in:
                os.dup2(devnull_in.fileno(), sys.stdin.fileno())
            log_fd = open(log_path, 'a+', buffering=1)
            os.dup2(log_fd.fileno(), sys.stdout.fileno())
            os.dup2(log_fd.fileno(), sys.stderr.fileno())

            # Attach a file handler so logger output lands in the log file too
            file_handler = logging.FileHandler(log_path)
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s %(name)s: %(message)s'
            ))
            logging.getLogger().addHandler(file_handler)
            logging.getLogger().setLevel(logging.INFO)

            # Write PID file
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))

            logger.info(f"Worker daemonized (PID: {os.getpid()})")

        except OSError as e:
            logger.error(f"Failed to daemonize: {e}")
            sys.exit(1)

    def _run_loop(self):
        """Main worker loop"""
        logger.info("Worker running")

        while self.running and not self.shutdown_requested:
            try:
                # Process jobs
                self._process_jobs()

                # Run scheduled maintenance
                self._run_maintenance()

                # Sleep before next iteration
                time.sleep(self.job_poll_interval)

            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                time.sleep(self.job_poll_interval * 2)  # Back off on errors

        logger.info("Worker stopped")
        self.running = False

    def _process_jobs(self):
        """Process pending jobs from queue"""
        # Claim next job
        job = claim_next_job()

        if not job:
            # No jobs available
            return

        job_id = job['id']
        job_type = job['type']

        logger.info(f"Processing job {job_id} (type: {job_type})")

        try:
            # Execute job based on type
            if job_type == 'compile':
                result = self._execute_compile_job(job)
            else:
                logger.warning(f"Unknown job type: {job_type}")
                return

            self.stats['jobs_processed'] += 1
            logger.info(f"Job {job_id} completed successfully")

        except (PermanentError, TransientError) as e:
            self.stats['jobs_failed'] += 1
            logger.error(f"Job {job_id} failed: {e}")
        except Exception as e:
            self.stats['jobs_failed'] += 1
            logger.error(f"Job {job_id} failed unexpectedly: {e}", exc_info=True)

    def _execute_compile_job(self, job: Dict) -> Dict:
        """
        Execute a compilation job.

        Args:
            job: Job dictionary

        Returns:
            Job result
        """
        params = job['params']
        raw_file_id = params.get('raw_file_id')
        model = params.get('model', 'sonnet')
        auto_approve = params.get('auto_approve', False)

        if not raw_file_id:
            raise PermanentError("Missing raw_file_id parameter")

        # Execute compilation with retry
        def execute_func(job_dict):
            return compile_raw_file(
                raw_file_id=raw_file_id,
                model=model,
                job_id=job_dict['id'],
                auto_approve=auto_approve
            )

        return execute_job_with_retry(job['id'], execute_func, max_retries=3)

    def _run_maintenance(self):
        """Run scheduled maintenance tasks"""
        now = datetime.now()

        # Check if it's time for maintenance
        if self.last_maintenance:
            elapsed = (now - self.last_maintenance).total_seconds()
            if elapsed < self.maintenance_interval:
                return

        logger.info("Running scheduled maintenance...")

        try:
            # Run integrity checks
            logger.info("  Running integrity checks...")
            from kb.lint import run_all_checks, get_summary
            results = run_all_checks()
            summary = get_summary(results)
            logger.info(f"  Found {summary['total_issues']} integrity issues")

            # Auto-fix deterministic issues
            if summary['fixable'] > 0:
                logger.info(f"  Auto-fixing {summary['fixable']} issues...")
                from kb.lint import fix_checksum_mismatches, fix_broken_links
                fixed_checksums = fix_checksum_mismatches(dry_run=False)
                fixed_links = fix_broken_links(dry_run=False)
                logger.info(f"  Fixed {fixed_checksums + fixed_links} issues")

            # Cleanup expired soft deletes
            logger.info("  Cleaning up expired deletions...")
            deleted_count = cleanup_expired_soft_deletes(grace_period_days=30)
            logger.info(f"  Permanently deleted {deleted_count} articles")

            self.stats['maintenance_runs'] += 1
            self.last_maintenance = now

            logger.info("Maintenance completed")

        except Exception as e:
            logger.error(f"Maintenance failed: {e}", exc_info=True)

    def get_status(self) -> Dict[str, Any]:
        """
        Get worker status.

        Returns:
            Status dictionary
        """
        uptime = None
        if self.stats['started_at']:
            started = datetime.fromisoformat(self.stats['started_at'])
            uptime = str(datetime.now() - started)

        return {
            'running': self.running,
            'shutdown_requested': self.shutdown_requested,
            'stats': {
                **self.stats,
                'uptime': uptime
            },
            'config': {
                'job_poll_interval': self.job_poll_interval,
                'maintenance_interval': self.maintenance_interval
            }
        }


def start_worker(daemon: bool = False):
    """
    Start the background worker.

    Args:
        daemon: Run as daemon process
    """
    worker = Worker()
    worker.start(daemon=daemon)


def stop_worker():
    """Stop the background worker"""
    pid_file = os.path.expanduser('~/.kb/worker.pid')

    if not os.path.exists(pid_file):
        logger.warning("Worker PID file not found")
        return False

    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())

        # Send SIGTERM
        os.kill(pid, signal.SIGTERM)

        logger.info(f"Sent shutdown signal to worker (PID: {pid})")

        # Wait for shutdown
        for i in range(30):  # Wait up to 30 seconds
            try:
                os.kill(pid, 0)  # Check if process exists
                time.sleep(1)
            except OSError:
                # Process no longer exists
                break

        # Clean up PID file
        os.remove(pid_file)

        logger.info("Worker stopped successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to stop worker: {e}")
        return False


def get_worker_status() -> Optional[Dict]:
    """
    Get worker status.

    Returns:
        Status dict or None if not running
    """
    pid_file = os.path.expanduser('~/.kb/worker.pid')

    if not os.path.exists(pid_file):
        return None

    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())

        # Check if process is running
        try:
            os.kill(pid, 0)
            return {
                'running': True,
                'pid': pid
            }
        except OSError:
            # Process not running, clean up stale PID file
            os.remove(pid_file)
            return None

    except Exception as e:
        logger.error(f"Failed to get worker status: {e}")
        return None
