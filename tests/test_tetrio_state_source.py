import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tetrio_state_source import (
    SnapshotValidationError,
    TetrioStateSource,
    normalize_snapshot_payload,
)


def make_board(height, filled=None):
    filled = filled or set()
    rows = []
    for row_index in range(height):
        row = []
        for col_index in range(10):
            row.append((row_index, col_index) in filled)
        rows.append(row)
    return rows


def make_payload(**overrides):
    now_ms = int(time.time() * 1000)
    payload = {
        "mode": "Solo",
        "source": "browser_cdp",
        "board": make_board(20),
        "current": "T",
        "hold": "I",
        "queue": ["L", "S", "O", "Z", "J"],
        "pieceCounter": 15,
        "pieceCounterSource": "stats.piecesPlaced",
        "stateRevision": 15,
        "gameId": "solo-1",
        "roundId": None,
        "sessionId": "session-1",
        "token": "session-1:15",
        "ready": True,
        "playing": True,
        "capturedAt": now_ms,
    }
    payload.update(overrides)
    if "stateRevision" not in overrides:
        payload["stateRevision"] = payload["pieceCounter"]
    return payload


class DummyProc:
    pid = 100

    def poll(self):
        return None


class ExitedProc:
    pid = 101

    def __init__(self, exit_code=1):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class BlockingProc:
    _pid_counter = 1000

    def __init__(self, stdout_lines=None, exit_code=None):
        BlockingProc._pid_counter += 1
        self.pid = BlockingProc._pid_counter
        self.stdout = iter(stdout_lines or [])
        self._exit_code = exit_code
        self._wait_event = threading.Event()
        self.wait_started = threading.Event()
        if exit_code is not None:
            self._wait_event.set()

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        self.wait_started.set()
        if self._exit_code is None:
            finished = self._wait_event.wait(timeout)
            if not finished:
                raise TimeoutError("wait timed out")
        return self._exit_code

    def set_exit_code(self, exit_code):
        self._exit_code = exit_code
        self._wait_event.set()

    def terminate(self):
        if self._exit_code is None:
            self.set_exit_code(0)

    def kill(self):
        if self._exit_code is None:
            self.set_exit_code(-9)


def wait_for(predicate, timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_state_source(snapshot_path, *, stale_after_ms=1000, required_queue_length=5):
    source = TetrioStateSource.__new__(TetrioStateSource)
    source.config_path = "config.json"
    source.config = {
        "tetrio_cdp": {
            "snapshot_path": str(snapshot_path),
            "vs_object_snapshot_path": str(Path(snapshot_path).with_name("vs-object.json")),
            "vs_bridge_path": str(Path(snapshot_path).with_name("vs-bridge.json")),
            "port": 9222,
            "url": "https://tetr.io/",
            "target": "TETR.IO",
            "poll_ms": 20,
            "auto_launch_chromium": True,
            "probe_page_state": True,
            "use_ribbon_websocket": True,
            "use_seed_simulation_fallback": False,
            "stale_after_ms": stale_after_ms,
            "required_queue_length": required_queue_length,
            "max_restart_delay_sec": 5,
        }
    }
    source.lock = threading.RLock()
    source.proc = DummyProc()
    source.reader_thread = None
    source.stdout_queue = queue.Queue()
    source.last_log_line = ""
    source.last_error = ""
    source.last_error_at = 0
    source.last_reason = "Waiting for game state"
    source.last_ready = True
    source.browser_connected = True
    source.last_valid_snapshot = None
    source.restart_times = []
    source.restart_backoff_sec = 0.5
    source.next_restart_allowed_at = 0.0
    source._last_session_id = None
    source._last_game_key = None
    source._last_piece_counter = None
    source._last_piece_counter_source = None
    source._last_captured_at = None
    source._last_token = None
    source._last_snapshot_seen = False
    source._pending_restart_record = False
    source._helper_started_at_ms = 0
    source._closing_pids = set()
    source._last_pipe_warning = ""
    source._build_command = lambda: ["node", "browser-source/tetrio-cdp-source.mjs"]
    return source


def write_snapshot(snapshot_path, payload):
    Path(snapshot_path).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class SnapshotNormalizationTests(unittest.TestCase):
    def test_normalizes_bottom_up_field_to_visible_top_down_board(self):
        field = make_board(40, {(0, 0), (19, 9)})
        snapshot = normalize_snapshot_payload(
            make_payload(
                field=field,
                board=None,
            ),
            required_queue_length=5,
        )

        self.assertEqual(snapshot.board[-1][0], "X")
        self.assertEqual(snapshot.board[0][9], "X")
        self.assertEqual(snapshot.board_height, 20)
        self.assertEqual(snapshot.board_width, 10)

    def test_normalizes_top_down_40_row_board_to_last_visible_rows(self):
        board = make_board(40, {(20, 2), (39, 7)})
        snapshot = normalize_snapshot_payload(
            make_payload(
                mode="VS",
                board=board,
                current="L",
                hold=None,
                queue=["I", "T", "S", "Z", "O"],
                pieceCounter=8,
                roundId="round-8",
                gameId=None,
                sessionId="session-vs-1",
                token="session-vs-1:8",
            ),
            required_queue_length=5,
        )

        self.assertEqual(snapshot.board[0][2], "X")
        self.assertEqual(snapshot.board[-1][7], "X")

    def test_rejects_invalid_piece_character(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    current="Q",
                    hold=None,
                    queue=["I", "T", "S", "Z", "O"],
                    pieceCounter=1,
                    sessionId="session-1",
                    token="session-1:1",
                ),
                required_queue_length=5,
            )

    def test_rejects_invalid_board_width(self):
        bad_board = [[False for _ in range(9)] for _ in range(20)]
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    board=bad_board,
                    hold=None,
                    queue=["I", "T", "S", "Z", "O"],
                    pieceCounter=1,
                    token="session-1:1",
                ),
                required_queue_length=5,
            )

    def test_rejects_stale_snapshot(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    hold=None,
                    queue=["I", "T", "S", "Z", "O"],
                    pieceCounter=1,
                    token="session-1:1",
                    capturedAt=int(time.time() * 1000) - 5000,
                ),
                stale_after_ms=1000,
                required_queue_length=5,
            )

    def test_rejects_playing_false_snapshot(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    hold=None,
                    queue=["I", "T", "S", "Z", "O"],
                    pieceCounter=1,
                    token="session-1:1",
                    playing=False,
                ),
                required_queue_length=5,
            )

    def test_rejects_missing_session_id(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    hold=None,
                    queue=["I", "T", "S", "Z", "O"],
                    pieceCounter=9,
                    sessionId="",
                    token="session-1:9",
                ),
                required_queue_length=5,
            )

    def test_rejects_missing_piece_counter_source(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                make_payload(
                    pieceCounterSource="",
                ),
                required_queue_length=5,
            )

    def test_allows_missing_piece_counter_for_derived_revision(self):
        snapshot = normalize_snapshot_payload(
            make_payload(
                pieceCounter=None,
                pieceCounterSource="derived-revision",
                stateRevision=7,
                token="session-1:7",
            ),
            required_queue_length=5,
        )

        self.assertIsNone(snapshot.piece_counter)
        self.assertEqual(snapshot.state_revision, 7)


class TetrioStateSourceTests(unittest.TestCase):
    def test_rejects_piece_counter_regression_in_same_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = lambda: None

            first = make_payload(
                pieceCounter=10,
                token="session-1:10",
                capturedAt=int(time.time() * 1000),
            )
            write_snapshot(snapshot_path, first)
            snapshot = source.get_latest_valid_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.piece_counter, 10)

            second = make_payload(
                pieceCounter=9,
                token="session-1:9",
                capturedAt=first["capturedAt"] + 20,
            )
            write_snapshot(snapshot_path, second)
            self.assertIsNone(source.get_latest_valid_snapshot())
            self.assertIn("stateRevision", source.last_reason)

    def test_allows_new_session_after_tombstone_even_with_same_game_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = lambda: None

            first = make_payload(
                sessionId="session-1",
                gameId="solo-synthetic",
                pieceCounter=12,
                token="session-1:12",
                capturedAt=int(time.time() * 1000),
            )
            write_snapshot(snapshot_path, first)
            snapshot = source.get_latest_valid_snapshot()
            self.assertEqual(snapshot.session_id, "session-1")

            write_snapshot(
                snapshot_path,
                {
                    "ok": False,
                    "mode": "Solo",
                    "ready": False,
                    "playing": False,
                    "reason": "TETR.IO game ended",
                    "capturedAt": first["capturedAt"] + 10,
                },
            )
            self.assertIsNone(source.get_latest_valid_snapshot())

            second = make_payload(
                sessionId="session-2",
                gameId="solo-synthetic",
                pieceCounter=0,
                token="session-2:0",
                capturedAt=first["capturedAt"] + 20,
            )
            write_snapshot(snapshot_path, second)
            snapshot = source.get_latest_valid_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.session_id, "session-2")
            self.assertEqual(snapshot.piece_counter, 0)

    def test_missing_snapshot_resets_sequence_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = lambda: None

            first = make_payload(
                sessionId="session-1",
                pieceCounter=4,
                token="session-1:4",
                capturedAt=int(time.time() * 1000),
            )
            write_snapshot(snapshot_path, first)
            self.assertIsNotNone(source.get_latest_valid_snapshot())

            snapshot_path.unlink()
            self.assertIsNone(source.get_latest_valid_snapshot())

            second = make_payload(
                sessionId="session-2",
                pieceCounter=0,
                token="session-2:0",
                capturedAt=first["capturedAt"] + 20,
            )
            write_snapshot(snapshot_path, second)
            snapshot = source.get_latest_valid_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.session_id, "session-2")

    def test_derived_revision_monotonicity_is_checked_per_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = lambda: None

            first = make_payload(
                pieceCounter=None,
                pieceCounterSource="derived-revision",
                stateRevision=3,
                token="session-1:3",
            )
            write_snapshot(snapshot_path, first)
            self.assertIsNotNone(source.get_latest_valid_snapshot())

            second = make_payload(
                pieceCounter=None,
                pieceCounterSource="derived-revision",
                stateRevision=2,
                token="session-1:2",
                capturedAt=first["capturedAt"] + 20,
            )
            write_snapshot(snapshot_path, second)
            self.assertIsNone(source.get_latest_valid_snapshot())
            self.assertIn("stateRevision", source.last_reason)

    def test_stdout_eof_with_live_process_keeps_proc_and_browser_connected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)
            source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)
            source.browser_connected = True
            source.last_ready = True
            source.proc = BlockingProc(stdout_lines=[])

            reader = threading.Thread(target=source._reader_loop, daemon=True)
            reader.start()

            self.assertTrue(wait_for(lambda: source.proc.wait_started.is_set()))
            self.assertIs(source.proc, source.proc)
            self.assertTrue(source.browser_connected)
            self.assertTrue(source.last_ready)
            self.assertEqual(source.restart_times, [])
            self.assertIn("[CDP PIPE EOF]", source.last_log_line)

            source.proc.set_exit_code(0)
            reader.join(1.0)

    def test_stdout_eof_reproduction_does_not_spawn_new_helper_or_reconnecting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = BlockingProc(stdout_lines=['{"type":"ready","ok":true}\n'])
            source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)

            start_calls = 0

            def forbidden_start():
                nonlocal start_calls
                start_calls += 1

            source.start = forbidden_start

            reader = threading.Thread(target=source._reader_loop, daemon=True)
            reader.start()
            self.assertTrue(wait_for(lambda: source.proc.wait_started.is_set()))

            for _ in range(100):
                status = source.get_status()
                self.assertNotEqual(status["browser_status"], "Reconnecting")

            self.assertEqual(start_calls, 0)
            self.assertIsNotNone(source.proc)
            self.assertEqual(source.restart_times, [])

            source.proc.set_exit_code(0)
            reader.join(1.0)

    def test_stdout_eof_then_actual_exit_clears_proc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            proc = BlockingProc(stdout_lines=[])
            source.proc = proc
            source.browser_connected = True
            source.last_ready = True

            reader = threading.Thread(target=source._reader_loop, daemon=True)
            reader.start()
            self.assertTrue(wait_for(lambda: proc.wait_started.is_set()))

            proc.set_exit_code(1)
            reader.join(1.0)

            self.assertIsNone(source.proc)
            self.assertFalse(source.browser_connected)
            self.assertFalse(source.last_ready)

    def test_actual_exit_restarts_helper_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = BlockingProc(stdout_lines=[])

            reader = threading.Thread(target=source._reader_loop, daemon=True)
            reader.start()
            self.assertTrue(wait_for(lambda: source.proc.wait_started.is_set()))
            source.proc.set_exit_code(1)
            reader.join(1.0)

            source.next_restart_allowed_at = time.time() - 0.1
            spawned = BlockingProc(stdout_lines=[])
            with mock.patch("subprocess.Popen", return_value=spawned) as popen:
                source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)
                source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)
                status = source.get_status()

            self.assertEqual(popen.call_count, 1)
            self.assertEqual(len(source.restart_times), 1)
            self.assertEqual(status["browser_status"], "Connecting")
            spawned.set_exit_code(0)

    def test_concurrent_get_status_starts_single_helper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = None
            source.browser_connected = False
            source.last_ready = False
            source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)
            source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)

            spawned = BlockingProc(stdout_lines=[])
            results = []

            def call_status():
                results.append(source.get_status())

            with mock.patch("subprocess.Popen", return_value=spawned) as popen:
                threads = [threading.Thread(target=call_status) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(popen.call_count, 1)
            self.assertEqual(len(results), 8)
            spawned.set_exit_code(0)

    def test_initial_start_does_not_count_as_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = None
            source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)

            spawned = BlockingProc(stdout_lines=[])
            with mock.patch("subprocess.Popen", return_value=spawned):
                source.start()

            self.assertEqual(source.restart_times, [])
            spawned.set_exit_code(0)

    def test_ready_signal_resets_restart_backoff_and_clears_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.restart_times = [time.time()]
            source.next_restart_allowed_at = time.time() + 5
            source.restart_backoff_sec = 4.0
            source.last_error = "old error"
            source.last_error_at = time.time()
            source.proc = BlockingProc(stdout_lines=['{"type":"ready","ok":true}\n'])

            reader = threading.Thread(target=source._reader_loop, daemon=True)
            reader.start()
            self.assertTrue(wait_for(lambda: source.proc.wait_started.is_set()))

            self.assertEqual(source.restart_times, [])
            self.assertEqual(source.next_restart_allowed_at, 0.0)
            self.assertEqual(source.restart_backoff_sec, 0.5)
            self.assertEqual(source.last_error, "")
            self.assertTrue(source.browser_connected)
            self.assertTrue(source.last_ready)

            source.proc.set_exit_code(0)
            reader.join(1.0)

    def test_valid_snapshot_resets_restart_backoff_and_clears_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source._ensure_running = lambda: None
            source.restart_times = [time.time()]
            source.next_restart_allowed_at = time.time() + 5
            source.restart_backoff_sec = 4.0
            source.last_error = "old error"
            source.last_error_at = time.time()

            write_snapshot(snapshot_path, make_payload(pieceCounter=0, token="session-1:0", stateRevision=0))
            snapshot = source.get_latest_valid_snapshot()

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.piece_counter, 0)
            self.assertEqual(source.restart_times, [])
            self.assertEqual(source.next_restart_allowed_at, 0.0)
            self.assertEqual(source.restart_backoff_sec, 0.5)
            self.assertEqual(source.last_error, "")

    def test_restart_backoff_caps_at_five_seconds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)

            waits = []
            for _ in range(20):
                source._schedule_restart_after_exit()
                waits.append(max(0.0, source.next_restart_allowed_at - time.time()))

            self.assertTrue(all(wait <= 5.05 for wait in waits))
            self.assertLessEqual(source.restart_backoff_sec, 5.0)

    def test_previous_run_stale_snapshot_is_treated_as_waiting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path, stale_after_ms=1000)
            source._ensure_running = lambda: None
            source._helper_started_at_ms = int(time.time() * 1000)
            source.last_error = ""
            source.last_reason = "Waiting for game state"

            write_snapshot(
                snapshot_path,
                make_payload(capturedAt=source._helper_started_at_ms - 1000),
            )

            snapshot = source.get_latest_valid_snapshot()
            status = source.get_status()

            self.assertIsNone(snapshot)
            self.assertEqual(source.last_error, "")
            self.assertEqual(status["game_state"], "Waiting")
            self.assertEqual(status["detail"], "Waiting for game state")

    def test_get_status_while_restart_backoff_pending_reports_waiting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = ExitedProc()
            source.browser_connected = False
            source.last_ready = False
            source._pending_restart_record = True
            source.next_restart_allowed_at = time.time() + 1
            source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)
            source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)

            status = source.get_status()

            self.assertEqual(status["browser_status"], "Reconnecting")
            self.assertEqual(status["game_state"], "Waiting")
            self.assertIn("재시작 대기", status["detail"])


if __name__ == "__main__":
    unittest.main()
