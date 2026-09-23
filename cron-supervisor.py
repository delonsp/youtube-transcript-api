#!/usr/bin/env python3
"""Stop scheduling, drain existing cron children, then terminate with a bounded grace.

Linux only. A subreaper retains cron's orphaned jobs even when they start new sessions.
Never enumerates or signals processes outside its descendant tree.
"""
import argparse
import ctypes
import errno
import logging
import os
from pathlib import Path
import signal
import subprocess
import time


def children():
    rows = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            rows[int(path.parent.name)] = (int(fields[1]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    selected = {os.getpid()}
    while True:
        more = {pid for pid, (ppid, state) in rows.items() if ppid in selected and state != 'Z'}
        if more <= selected:
            return selected - {os.getpid()}
        selected |= more


def reap():
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return


def send(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--drain-seconds', type=float, default=45)
    parser.add_argument('--term-seconds', type=float, default=5)
    parser.add_argument('--log', default='/var/log/cron.log')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command or ['cron', '-f']
    if command[0] == '--':
        command = command[1:]
    if not 0 <= args.drain_seconds <= 45 or not 0 < args.term_seconds <= 5:
        raise SystemExit('Invalid shutdown bounds')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Unable to become child subreaper')
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler = subprocess.Popen(command)
    tail = subprocess.Popen(['tail', '-n', '+1', '-F', args.log])
    failure = False
    while not stopping:
        if scheduler.poll() is not None or tail.poll() is not None:
            logging.error('Scheduler or log follower exited unexpectedly')
            failure = True
            break
        time.sleep(0.1)
    # Stop the scheduler before waiting for its jobs. Keep the log follower alive.
    scheduler.terminate() if scheduler.poll() is None else None
    try:
        scheduler.wait(timeout=2)
    except subprocess.TimeoutExpired:
        logging.error('Scheduler ignored termination')
        scheduler.kill()
        scheduler.wait()
        failure = True
    deadline = time.monotonic() + args.drain_seconds
    while children() - {tail.pid} and time.monotonic() < deadline:
        reap()
        time.sleep(0.1)
    pending = children() - {tail.pid}
    if pending:
        logging.warning('Drain deadline reached; terminating %d active processes', len(pending))
        failure = True
        send(pending, signal.SIGTERM)
        deadline = time.monotonic() + args.term_seconds
        while children() - {tail.pid} and time.monotonic() < deadline:
            reap()
            time.sleep(0.1)
        remaining = children() - {tail.pid}
        if remaining:
            logging.error('Termination deadline reached; killing %d processes', len(remaining))
            send(remaining, signal.SIGKILL)
    if tail.poll() is None:
        tail.terminate()
    tail.wait(timeout=2)
    reap()
    logging.info('Cron supervisor stopped; drained=%s', not failure)
    return 1 if failure else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    raise SystemExit(main())
