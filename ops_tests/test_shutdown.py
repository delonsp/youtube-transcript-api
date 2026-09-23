import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SUPERVISOR = Path(__file__).resolve().parents[1] / 'cron-supervisor.py'

class CronShutdown(unittest.TestCase):
    def exercise(self, job='', drain='2', scheduler_exit=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'cron.log').touch()
            if job:
                (root/'job.py').write_text("from pathlib import Path\nimport os,signal,time\nr=Path(__file__).parent\n(r/'job.pid').write_text(str(os.getpid()))\n" + job)
            script = "from pathlib import Path\nimport subprocess,sys,time\nr=Path(__file__).parent\n"
            if job:
                script += "p=subprocess.Popen([sys.executable,str(r/'job.py')],start_new_session=True)\nwhile not (r/'job.pid').exists():time.sleep(.01)\n"
            script += "(r/'ready').touch()\n"
            script += 'sys.exit(7)\n' if scheduler_exit else 'while True:time.sleep(.05)\n'
            (root/'scheduler.py').write_text(script)
            p = subprocess.Popen([sys.executable, str(SUPERVISOR), '--drain-seconds', drain,
                                  '--term-seconds', '1', '--log', str(root/'cron.log'), '--',
                                  sys.executable, str(root/'scheduler.py')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                until = time.monotonic()+5
                while not (root/'ready').exists() and time.monotonic()<until:
                    time.sleep(.02)
                self.assertTrue((root/'ready').exists())
                time.sleep(.1)
                if not scheduler_exit:
                    p.send_signal(signal.SIGTERM)
                output, errors = p.communicate(timeout=8)
                return p.returncode, {x.name:x.read_text() for x in root.iterdir()}, errors.decode()
            finally:
                if p.poll() is None:
                    p.terminate()
                    p.communicate(timeout=8)

    def test_idle_stop(self):
        code, files, logs = self.exercise()
        self.assertEqual(code, 0, logs)

    def test_adopt_and_drain_job_in_separate_session(self):
        code, files, logs = self.exercise("time.sleep(.6)\n(r/'done').touch()\n")
        self.assertEqual(code, 0, logs)
        self.assertIn('done', files)

    def test_expired_job_receives_term(self):
        code, files, logs = self.exercise("signal.signal(signal.SIGTERM,lambda *args:((r/'term').touch(),exit(0)))\ntime.sleep(30)\n", drain='.2')
        self.assertEqual(code, 1, logs)
        self.assertIn('term', files)
        self.assertIn('Drain deadline reached', logs)
        self.assertNotIn('killing', logs)

    def test_unresponsive_job_is_bounded(self):
        code, files, logs = self.exercise("signal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n", drain='.2')
        self.assertEqual(code, 1, logs)
        self.assertIn('killing', logs)

    def test_scheduler_failure_is_not_reported_as_success(self):
        code, files, logs = self.exercise(scheduler_exit=True)
        self.assertEqual(code, 1, logs)
        self.assertIn('exited unexpectedly', logs)

    def test_bounds_reject_before_scheduler(self):
        result = subprocess.run([sys.executable,str(SUPERVISOR),'--drain-seconds','90'],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'Invalid shutdown bounds',result.stderr)


if __name__ == '__main__':
    unittest.main()
