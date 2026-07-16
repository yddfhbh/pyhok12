import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
    def poll(self):
        return None


class ExitedProc:
    def __init__(self, exit_code=1):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


def make_state_source(snapshot_path, *, stale_after_ms=1000, required_queue_length=5):
    source = TetrioStateSource.__new__(TetrioStateSource)
    source.config_path = "config.json"
    source.config = {
        "tetrio_cdp": {
            "snapshot_path": str(snapshot_path),
            "stale_after_ms": stale_after_ms,
            "required_queue_length": required_queue_length,
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
    source.next_restart_allowed_at = 0.0
    source._last_session_id = None
    source._last_game_key = None
    source._last_piece_counter = None
    source._last_piece_counter_source = None
    source._last_captured_at = None
    source._last_token = None
    source._last_snapshot_seen = False
    source._ensure_running = lambda: None
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

    def test_get_status_does_not_raise_when_restart_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "live-snapshot.json"
            source = make_state_source(snapshot_path)
            source.proc = ExitedProc()
            source.browser_connected = False
            source.last_ready = False
            source.restart_times = [time.time()]
            source.next_restart_allowed_at = time.time() + 5
            source._ensure_running = TetrioStateSource._ensure_running.__get__(source, TetrioStateSource)
            source.start = TetrioStateSource.start.__get__(source, TetrioStateSource)

            status = source.get_status()

            self.assertEqual(status["browser_status"], "Reconnecting")
            self.assertEqual(status["game_state"], "Waiting")
            self.assertIn("재시작", status["detail"])


if __name__ == "__main__":
    unittest.main()
