import os
import unittest
from unittest.mock import Mock, patch

from rq import Retry
from rq.scheduler import RQScheduler

from app.core.queues import JOB_RETRY, ai_queue, gmail_queue
from app.jobs.ai import tag_recent_untagged_emails_job
from app.jobs.gmail import run_initial_gmail_sync_job
from app.worker import ai, gmail, scheduler as scheduler_worker


class WorkerStartupTests(unittest.TestCase):
    @patch("app.worker.gmail.Worker")
    def test_gmail_worker_runs_without_a_scheduler(self, worker_class):
        gmail.main()

        worker_class.assert_called_once_with(
            [gmail_queue],
            connection=gmail.redis_connection,
        )
        worker_class.return_value.work.assert_called_once_with(
            with_scheduler=False,
            logging_level="INFO",
        )

    @patch("app.worker.ai.Worker")
    def test_ai_worker_runs_without_a_scheduler(self, worker_class):
        ai.main()

        worker_class.assert_called_once_with(
            [ai_queue],
            connection=ai.redis_connection,
        )
        worker_class.return_value.work.assert_called_once_with(
            with_scheduler=False,
            logging_level="INFO",
        )

    @patch("app.api.routes.emails.run_initial_gmail_sync")
    def test_gmail_job_executes_initial_sync(self, run_sync):
        run_initial_gmail_sync_job("account-1")

        run_sync.assert_called_once_with("account-1")

    @patch("app.api.routes.emails.tag_recent_untagged_emails")
    def test_ai_job_executes_tagging(self, tag_emails):
        tag_recent_untagged_emails_job("account-1", limit=3)

        tag_emails.assert_called_once_with("account-1", 3)


class SchedulerWorkerTests(unittest.TestCase):
    def test_default_interval_is_90_seconds(self):
        with patch.dict(
            os.environ,
            {"RQ_SCHEDULER_INTERVAL_SECONDS": "90"},
        ):
            self.assertEqual(scheduler_worker._scheduler_interval_seconds(), 90)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(scheduler_worker._scheduler_interval_seconds(), 90)

    def test_invalid_scheduler_intervals_are_rejected(self):
        for value in ("invalid", "0", "-1"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"RQ_SCHEDULER_INTERVAL_SECONDS": value},
            ):
                with self.assertRaises(RuntimeError):
                    scheduler_worker._scheduler_interval_seconds()

    def test_scheduler_manages_both_queues_at_configured_interval(self):
        with patch.dict(
            "os.environ",
            {"RQ_SCHEDULER_INTERVAL_SECONDS": "120"},
        ), patch("app.worker.scheduler.RQScheduler") as scheduler_class:
            scheduler_class.return_value.name = "test-scheduler"
            scheduler_class.return_value.interval = 120
            with self.assertLogs(scheduler_worker.logger, level="INFO") as logs:
                scheduler_worker.main()

        scheduler_class.assert_called_once_with(
            (gmail_queue, ai_queue),
            connection=scheduler_worker.redis_connection,
            interval=120,
            logging_level=scheduler_worker.logging.INFO,
        )
        scheduler_class.return_value.work.assert_called_once_with()
        self.assertIn("queues=gmail,ai", logs.output[0])
        self.assertIn("interval_seconds=120", logs.output[0])

    def test_scheduler_restarts_by_constructing_a_fresh_rq_scheduler(self):
        first_scheduler = Mock()
        second_scheduler = Mock()
        first_scheduler.work.side_effect = RuntimeError(
            "scheduler stopped unexpectedly"
        )
        with patch("app.worker.scheduler.RQScheduler") as scheduler_class:
            scheduler_class.side_effect = [first_scheduler, second_scheduler]

            with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly"):
                scheduler_worker.main()

            scheduler_worker.main()

        self.assertEqual(scheduler_class.call_count, 2)
        first_scheduler.stop.assert_called_once_with()
        first_scheduler.work.assert_called_once_with()
        second_scheduler.work.assert_called_once_with()

    def test_retry_configuration_keeps_delayed_retries_enabled(self):
        self.assertIsInstance(JOB_RETRY, Retry)
        self.assertEqual(JOB_RETRY.max, 2)
        self.assertEqual(JOB_RETRY.intervals, [15, 60])

    def test_scheduler_promotes_a_due_retry_from_each_managed_queue(self):
        gmail_registry = self._scheduled_registry("gmail", "gmail-job")
        ai_registry = self._scheduled_registry("ai", "ai-job")
        scheduler = RQScheduler.__new__(RQScheduler)
        scheduler._status = RQScheduler.Status.STOPPED
        scheduler._scheduled_job_registries = [gmail_registry, ai_registry]
        scheduler._acquired_locks = {"gmail", "ai"}
        scheduler._connection = object()
        scheduler.serializer = None

        gmail_job = self._job("gmail-job")
        ai_job = self._job("ai-job")
        pipeline = _Pipeline()
        connection = _PipelineConnection(pipeline)
        scheduler._connection = connection

        gmail_queue_instance = Mock()
        ai_queue_instance = Mock()
        with (
            patch("rq.scheduler.Job.fetch_many", side_effect=[[gmail_job], [ai_job]]),
            patch(
                "rq.scheduler.Queue",
                side_effect=[gmail_queue_instance, ai_queue_instance],
            ) as queue_class,
        ):
            scheduler.enqueue_scheduled_jobs()

        self.assertEqual(gmail_registry.removed, ["gmail-job"])
        self.assertEqual(ai_registry.removed, ["ai-job"])
        self.assertEqual(queue_class.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in queue_class.call_args_list],
            ["gmail", "ai"],
        )
        gmail_queue_instance._enqueue_job.assert_called_once_with(
            gmail_job,
            pipeline=pipeline,
            at_front=False,
        )
        ai_queue_instance._enqueue_job.assert_called_once_with(
            ai_job,
            pipeline=pipeline,
            at_front=False,
        )
        self.assertEqual(pipeline.execute_count, 2)

    @staticmethod
    def _scheduled_registry(name, job_id):
        registry = Mock()
        registry.name = name
        registry.get_jobs_to_schedule.return_value = [job_id]
        registry.remove.side_effect = lambda value, pipeline=None: registry.removed.append(value)
        registry.removed = []
        return registry

    @staticmethod
    def _job(job_id):
        job = Mock()
        job.id = job_id
        job.has_rate_limit = False
        job.should_enqueue_at_front.return_value = False
        return job


class _Pipeline:
    def __init__(self):
        self.execute_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self):
        self.execute_count += 1


class _PipelineConnection:
    def __init__(self, pipeline):
        self.pipeline_instance = pipeline

    def pipeline(self):
        return self.pipeline_instance


if __name__ == "__main__":
    unittest.main()
