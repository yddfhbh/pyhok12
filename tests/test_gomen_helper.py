import os
import shutil
import threading
import unittest
from unittest import mock

from gomen_helper import GomenSession, get_gomen_solver_path, get_node_executable


class FakeProcess:
    _pid = 2000

    def __init__(self, stdout_lines=None, stderr_lines=None, exit_code=None, wait_hook=None):
        FakeProcess._pid += 1
        self.pid = FakeProcess._pid
        self.stdout = iter(stdout_lines or [])
        self.stderr = iter(stderr_lines or [])
        self.stdin = mock.Mock()
        self._exit_code = exit_code
        self.wait_hook = wait_hook
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        if self.wait_hook is not None:
            self.wait_hook(self)
            self.wait_hook = None
        if self._exit_code is None:
            self._exit_code = 0
        return self._exit_code

    def terminate(self):
        self.terminate_calls += 1
        if self._exit_code is None:
            self._exit_code = 0

    def kill(self):
        self.kill_calls += 1
        if self._exit_code is None:
            self._exit_code = -9


class GomenSessionTests(unittest.TestCase):
    def test_stdout_eof_alive_process_does_not_retry_forever(self):
        session = GomenSession()
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        reader = threading.Thread(target=session._reader_loop, args=(proc,), daemon=True)
        reader.start()
        reader.join(1.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertIsNone(session.proc)
        self.assertEqual(session.last_exit_code, 0)

    def test_stdout_eof_logs_once(self):
        session = GomenSession()
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        with mock.patch("builtins.print") as print_mock:
            session._reader_loop(proc)

        messages = [args[0] for args, _ in print_mock.call_args_list if args]
        self.assertEqual(
            sum("stdout closed while process is still alive" in message for message in messages),
            1,
        )

    def test_old_reader_does_not_clear_new_process_identity(self):
        session = GomenSession()
        replacement = FakeProcess(stdout_lines=["{\"kind\":\"ready\"}\n"], exit_code=None)

        def replace_current(_proc):
            session.proc = replacement

        old_proc = FakeProcess(stdout_lines=[], exit_code=None, wait_hook=replace_current)
        session.proc = old_proc

        session._reader_loop(old_proc)

        self.assertIs(session.proc, replacement)

    def test_next_ensure_started_restarts_helper_once_after_eof(self):
        session = GomenSession()
        old_proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = old_proc
        session._reader_loop(old_proc)

        new_proc = FakeProcess(stdout_lines=["{\"kind\":\"ready\"}\n"], exit_code=None)
        with (
            mock.patch("gomen_helper.get_gomen_solver_path", return_value="solver.js"),
            mock.patch("gomen_helper.get_gomen_js_path", return_value="gomen.js"),
            mock.patch("gomen_helper.get_gomen_wasm_path", return_value="gomen.wasm"),
            mock.patch("gomen_helper.get_legal_boards_path", return_value="legal.leb128"),
            mock.patch("gomen_helper.get_node_executable", return_value="node"),
            mock.patch("gomen_helper.get_tools_dir", return_value="."),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("subprocess.Popen", return_value=new_proc) as popen_mock,
            mock.patch.object(session, "_read_json", return_value={"kind": "ready"}),
        ):
            session.ensure_started(timeout_sec=1)

        self.assertEqual(popen_mock.call_count, 1)
        self.assertIs(session.proc, new_proc)

    def test_app_close_eof_is_quiet(self):
        session = GomenSession()
        session.closing = True
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        with mock.patch("builtins.print") as print_mock:
            session._reader_loop(proc)

        self.assertEqual(print_mock.call_count, 0)
        self.assertEqual(session.last_reader_warning, "")


def has_node_runtime():
    executable = get_node_executable()
    if os.path.isabs(executable):
        return os.path.exists(executable)
    return shutil.which(executable) is not None


@unittest.skipUnless(
    os.path.exists(get_gomen_solver_path()) and has_node_runtime(),
    "node runtime or gomen solver assets are unavailable",
)
class GomenSessionIntegrationTests(unittest.TestCase):
    def test_real_solver_handles_two_requests_on_same_process(self):
        session = GomenSession()
        try:
            response1 = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )
            first_proc = session.proc

            self.assertIsNotNone(first_proc)
            self.assertIsNone(first_proc.poll())
            self.assertTrue(response1["ok"])
            self.assertIn("solutions", response1)

            response2 = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )

            self.assertTrue(response2["ok"])
            self.assertIs(session.proc, first_proc)
            self.assertIsNone(first_proc.poll())
        finally:
            session.close()

    def test_next_solve_restarts_after_forced_process_exit(self):
        session = GomenSession()
        try:
            session.ensure_started(timeout_sec=30)
            first_proc = session.proc

            self.assertIsNotNone(first_proc)
            first_proc.kill()
            first_proc.wait(timeout=5)

            if session.reader_thread is not None:
                session.reader_thread.join(timeout=5)

            response = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )

            self.assertTrue(response["ok"])
            self.assertIsNotNone(session.proc)
            self.assertIsNot(session.proc, first_proc)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
