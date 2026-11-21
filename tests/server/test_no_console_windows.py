"""On Windows, the processes the portal starts get a console without a window.

A DETACHED_PROCESS has no console at all, so each console program it then runs (the runner's git
clone, add, commit and push) made Windows open a new terminal window on the desktop. The local
build runner was started that way, and the dev worker inherited no console when the portal had
none. Both now start with CREATE_NO_WINDOW."""

import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from twinlab2 import builds
from twinlab2.providers import local_runner


@unittest.skipUnless(sys.platform == "win32", "Windows console flags")
class NoConsoleWindowTest(unittest.TestCase):
    def flags_of(self, popen):
        self.assertTrue(popen.called)
        return popen.call_args.kwargs["creationflags"]

    def test_the_local_runner_gets_a_hidden_console(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(local_runner.subprocess, "Popen") as popen:
            runner = local_runner.LocalRunnerProvider({"token": "t"}, env={})
            runner._spawn(Path(folder))
        flags = self.flags_of(popen)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_the_dev_worker_gets_a_hidden_console(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(builds.subprocess, "Popen") as popen, \
                mock.patch.object(builds, "_dev_worker", None):
            popen.return_value.poll.return_value = None
            builds.start_dev_worker(Path(folder) / "catalog.db")
        flags = self.flags_of(popen)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)


if __name__ == "__main__":
    unittest.main()
