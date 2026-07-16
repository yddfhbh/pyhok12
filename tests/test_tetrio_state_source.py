import time
import unittest

from tetrio_state_source import SnapshotValidationError, normalize_snapshot_payload


def make_board(height, filled=None):
    filled = filled or set()
    rows = []
    for row_index in range(height):
        row = []
        for col_index in range(10):
            row.append((row_index, col_index) in filled)
        rows.append(row)
    return rows


class SnapshotNormalizationTests(unittest.TestCase):
    def test_normalizes_bottom_up_field_to_visible_top_down_board(self):
        field = make_board(40, {(0, 0), (19, 9)})
        snapshot = normalize_snapshot_payload(
            {
                "mode": "Solo",
                "field": field,
                "current": "T",
                "hold": "I",
                "queue": ["L", "S", "O", "Z", "J"],
                "pieceCounter": 15,
                "gameId": "solo-1",
                "token": "solo-1:15",
                "playing": True,
                "capturedAt": int(time.time() * 1000),
            },
            required_queue_length=5,
        )

        self.assertEqual(snapshot.board[-1][0], "X")
        self.assertEqual(snapshot.board[0][9], "X")
        self.assertEqual(snapshot.board_height, 20)
        self.assertEqual(snapshot.board_width, 10)

    def test_normalizes_top_down_40_row_board_to_last_visible_rows(self):
        board = make_board(40, {(20, 2), (39, 7)})
        snapshot = normalize_snapshot_payload(
            {
                "mode": "VS",
                "board": board,
                "current": "L",
                "hold": None,
                "queue": ["I", "T", "S", "Z", "O"],
                "pieceCounter": 8,
                "roundId": "round-8",
                "token": "round-8:8",
                "playing": True,
                "capturedAt": int(time.time() * 1000),
            },
            required_queue_length=5,
        )

        self.assertEqual(snapshot.board[0][2], "X")
        self.assertEqual(snapshot.board[-1][7], "X")

    def test_rejects_invalid_piece_character(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                {
                    "board": make_board(20),
                    "current": "Q",
                    "hold": None,
                    "queue": ["I", "T", "S", "Z", "O"],
                    "pieceCounter": 1,
                    "gameId": "solo-1",
                    "token": "solo-1:1",
                    "playing": True,
                    "capturedAt": int(time.time() * 1000),
                },
                required_queue_length=5,
            )

    def test_rejects_invalid_board_width(self):
        bad_board = [[False for _ in range(9)] for _ in range(20)]
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                {
                    "board": bad_board,
                    "current": "T",
                    "hold": None,
                    "queue": ["I", "T", "S", "Z", "O"],
                    "pieceCounter": 1,
                    "gameId": "solo-1",
                    "token": "solo-1:1",
                    "playing": True,
                    "capturedAt": int(time.time() * 1000),
                },
                required_queue_length=5,
            )

    def test_rejects_stale_snapshot(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                {
                    "board": make_board(20),
                    "current": "T",
                    "hold": None,
                    "queue": ["I", "T", "S", "Z", "O"],
                    "pieceCounter": 1,
                    "gameId": "solo-1",
                    "token": "solo-1:1",
                    "playing": True,
                    "capturedAt": int(time.time() * 1000) - 5000,
                },
                stale_after_ms=1000,
                required_queue_length=5,
            )

    def test_rejects_playing_false_snapshot(self):
        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                {
                    "board": make_board(20),
                    "current": "T",
                    "hold": None,
                    "queue": ["I", "T", "S", "Z", "O"],
                    "pieceCounter": 1,
                    "gameId": "solo-1",
                    "token": "solo-1:1",
                    "playing": False,
                    "capturedAt": int(time.time() * 1000),
                },
                required_queue_length=5,
            )

    def test_rejects_piece_counter_regression_in_same_game(self):
        previous = normalize_snapshot_payload(
            {
                "board": make_board(20),
                "current": "T",
                "hold": None,
                "queue": ["I", "T", "S", "Z", "O"],
                "pieceCounter": 10,
                "gameId": "solo-1",
                "token": "solo-1:10",
                "playing": True,
                "capturedAt": int(time.time() * 1000),
            },
            required_queue_length=5,
        )

        with self.assertRaises(SnapshotValidationError):
            normalize_snapshot_payload(
                {
                    "board": make_board(20),
                    "current": "L",
                    "hold": None,
                    "queue": ["I", "T", "S", "Z", "O"],
                    "pieceCounter": 9,
                    "gameId": "solo-1",
                    "token": "solo-1:9",
                    "playing": True,
                    "capturedAt": previous.captured_at + 20,
                },
                required_queue_length=5,
                previous_snapshot=previous,
            )

    def test_allows_piece_counter_reset_on_new_game_identity(self):
        previous = normalize_snapshot_payload(
            {
                "board": make_board(20),
                "current": "T",
                "hold": None,
                "queue": ["I", "T", "S", "Z", "O"],
                "pieceCounter": 10,
                "gameId": "solo-1",
                "token": "solo-1:10",
                "playing": True,
                "capturedAt": int(time.time() * 1000),
            },
            required_queue_length=5,
        )

        current = normalize_snapshot_payload(
            {
                "board": make_board(20),
                "current": "I",
                "hold": None,
                "queue": ["L", "T", "S", "Z", "O"],
                "pieceCounter": 0,
                "gameId": "solo-2",
                "token": "solo-2:0",
                "playing": True,
                "capturedAt": previous.captured_at + 20,
            },
            required_queue_length=5,
            previous_snapshot=previous,
        )

        self.assertEqual(current.game_id, "solo-2")
        self.assertEqual(current.piece_counter, 0)


if __name__ == "__main__":
    unittest.main()
