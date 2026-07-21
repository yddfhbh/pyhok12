import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from app_paths import get_resource_path, get_runtime_data_dir, resolve_node_executable, resolve_runtime_file


VALID_PIECES = set("IJLOSTZ")
VISIBLE_FIELD_PIECES = set("IJLOSTZ")
TETROMINO_BASE_COORDS = {
    "I": [(0, 0), (0, 1), (0, 2), (0, 3)],
    "J": [(0, 0), (1, 0), (1, 1), (1, 2)],
    "L": [(0, 2), (1, 0), (1, 1), (1, 2)],
    "O": [(0, 0), (0, 1), (1, 0), (1, 1)],
    "S": [(0, 1), (0, 2), (1, 0), (1, 1)],
    "T": [(0, 1), (1, 0), (1, 1), (1, 2)],
    "Z": [(0, 0), (0, 1), (1, 1), (1, 2)],
}

DEFAULT_CONFIG = {
    "tetrio_cdp": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9222,
        "url": "https://tetr.io/",
        "target": "TETR.IO",
        "snapshot_path": "runtime/tetrio-snapshot.json",
        "vs_object_snapshot_path": "runtime/tetrio-vs-object-snapshot.json",
        "vs_bridge_path": "runtime/vs-ws-bridge.json",
        "stale_after_ms": 1000,
        "auto_launch_chromium": False,
        "poll_ms": 20,
        "probe_page_state": True,
        "use_ribbon_websocket": True,
        "use_seed_simulation_fallback": False,
        "required_queue_length": 5,
        "max_restart_delay_sec": 5,
    }
}


class SnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedSnapshot:
    mode: str
    source: str
    session_id: str
    board: list
    current: str
    hold: str | None
    queue: list
    piece_counter: int | None
    piece_counter_source: str
    lines_cleared: int | None
    derived_placed_pieces: int | None
    state_revision: int
    game_id: str | None
    round_id: str | None
    token: str
    playing: bool
    ready: bool
    captured_at: int
    age_ms: int
    board_width: int
    board_height: int
    received_at: int = 0
    connection_generation: int = 0


def deep_merge(base, override):
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path="config.json"):
    config_path = Path(resolve_runtime_file(path))
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return deep_merge(DEFAULT_CONFIG, loaded)


def calculate_pc_round(pieces_count):
    if pieces_count is None or pieces_count < 0:
        return None
    return (int(pieces_count) // 10) % 7 + 1


def _get_nested_value(payload, path):
    current = payload
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current.get(key)
    return current


def _extract_nonnegative_int(payload, candidate_paths):
    for path in candidate_paths:
        value = _get_nested_value(payload, path)
        if value is None:
            continue
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized >= 0:
            return normalized
    return None


def count_fixed_board_cells(board):
    return sum(1 for row in (board or []) for cell in row if cell != ".")


def is_piece_counter_source_transition_allowed(previous_source, current_source):
    previous = str(previous_source or "").strip()
    current = str(current_source or "").strip()
    if not current:
        return False
    if not previous or previous == current:
        return True
    if previous == "derived-revision" and current != "derived-revision":
        return True
    return False


def resolve_effective_piece_progress_details(snapshot):
    details = {
        "pieces_count": None,
        "source": None,
        "failure_reason": None,
        "fixed_cells": count_fixed_board_cells(snapshot.board) if snapshot is not None else 0,
        "lines_cleared": snapshot.lines_cleared if snapshot is not None else None,
        "derived_placed_pieces": snapshot.derived_placed_pieces if snapshot is not None else None,
    }
    if snapshot is None:
        details["failure_reason"] = "pieceCounter/linesCleared 없음"
        return details

    if snapshot.piece_counter is not None:
        details["pieces_count"] = int(snapshot.piece_counter)
        details["source"] = "piece-counter"
        return details

    if str(snapshot.mode or "").strip().lower() != "solo":
        details["failure_reason"] = "pieceCounter/linesCleared 없음"
        return details

    if snapshot.lines_cleared is not None:
        numerator = details["fixed_cells"] + int(snapshot.lines_cleared) * 10
        if numerator >= 0 and numerator % 4 == 0:
            details["pieces_count"] = numerator // 4
            details["source"] = "board-lines"
            return details
        details["failure_reason"] = "진행값 불일치"

    if snapshot.derived_placed_pieces is not None:
        details["pieces_count"] = int(snapshot.derived_placed_pieces)
        details["source"] = "derived-spawn-counter"
        details["failure_reason"] = None
        return details

    if details["failure_reason"] is None:
        details["failure_reason"] = "pieceCounter/linesCleared 없음"
    return details


def resolve_effective_piece_progress(snapshot):
    return resolve_effective_piece_progress_details(snapshot)["pieces_count"]


def _normalize_piece(value, allow_none=False):
    if value is None:
        return None if allow_none else ""
    text = str(value).strip().upper()
    if not text:
        return None if allow_none else ""
    if text not in VALID_PIECES:
        raise SnapshotValidationError(f"유효하지 않은 미노 문자: {text}")
    return text


def _coerce_filled_cell(cell):
    if cell in (None, False, 0, "", ".", "0", "empty"):
        return False
    if isinstance(cell, str):
        return cell.strip().lower() not in {"", ".", "0", "empty", "false", "null"}
    if isinstance(cell, dict):
        if "empty" in cell:
            return not bool(cell["empty"])
        if "type" in cell:
            return _coerce_filled_cell(cell["type"])
        if "mino" in cell:
            return _coerce_filled_cell(cell["mino"])
    return bool(cell)


def _validate_raw_board(raw_board):
    if not isinstance(raw_board, list) or not raw_board:
        raise SnapshotValidationError("board가 비어 있거나 배열이 아닙니다.")
    if len(raw_board) not in (20, 40):
        raise SnapshotValidationError(f"지원하지 않는 board 높이: {len(raw_board)}")
    for row in raw_board:
        if not isinstance(row, list):
            raise SnapshotValidationError("board row가 배열이 아닙니다.")
        if len(row) != 10:
            raise SnapshotValidationError(f"board row 길이가 10이 아닙니다: {len(row)}")


def _normalize_board_from_top_down(raw_board):
    _validate_raw_board(raw_board)
    rows = raw_board[-20:] if len(raw_board) == 40 else raw_board
    return [["X" if _coerce_filled_cell(cell) else "." for cell in row] for row in rows]


def _normalize_board_from_bottom_up(raw_board):
    _validate_raw_board(raw_board)
    visible_bottom_up = raw_board[:20]
    rows = list(reversed(visible_bottom_up))
    return [["X" if _coerce_filled_cell(cell) else "." for cell in row] for row in rows]


def normalize_snapshot_payload(
    payload,
    *,
    now_ms=None,
    stale_after_ms=1000,
    required_queue_length=1,
):
    if not isinstance(payload, dict):
        raise SnapshotValidationError("snapshot JSON이 객체가 아닙니다.")

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    ready = payload.get("ready")
    if ready is not True:
        raise SnapshotValidationError("ready=false 상태라서 solver에 전달할 수 없습니다.")
    playing = payload.get("playing")
    if playing is not True:
        raise SnapshotValidationError("playing=false 상태라서 solver에 전달할 수 없습니다.")

    captured_at = int(payload.get("capturedAt") or 0)
    if captured_at <= 0:
        raise SnapshotValidationError("capturedAt이 없거나 잘못되었습니다.")

    age_ms = max(0, now_ms - captured_at)
    if age_ms > int(stale_after_ms):
        raise SnapshotValidationError(f"snapshot이 stale 상태입니다. age={age_ms}ms")

    raw_board = payload.get("board")
    if isinstance(raw_board, list):
        board = _normalize_board_from_top_down(raw_board)
    elif isinstance(payload.get("field"), list):
        board = _normalize_board_from_bottom_up(payload["field"])
    else:
        raise SnapshotValidationError("board 또는 field가 없습니다.")

    board_height = len(board)
    board_width = len(board[0]) if board else 0
    if board_width != 10:
        raise SnapshotValidationError(f"board 너비가 10이 아닙니다: {board_width}")

    current = _normalize_piece(payload.get("current"))
    hold = _normalize_piece(payload.get("hold"), allow_none=True)
    queue = [_normalize_piece(piece) for piece in (payload.get("queue") or []) if piece is not None]
    if not isinstance(payload.get("queue"), list):
        raise SnapshotValidationError("queue가 배열이 아닙니다.")
    if len(queue) < int(required_queue_length):
        raise SnapshotValidationError(
            f"queue 길이가 부족합니다. required={required_queue_length} actual={len(queue)}"
        )

    piece_counter_source = str(payload.get("pieceCounterSource") or "").strip()
    if not piece_counter_source:
        raise SnapshotValidationError("pieceCounterSource가 비어 있습니다.")
    lines_cleared = _extract_nonnegative_int(
        payload,
        (
            "linesCleared",
            "lines_cleared",
            "lines",
            "stats.lines",
            "stats.linesCleared",
            "stats.lines_cleared",
            "ejectState.linesCleared",
            "ejectState.lines_cleared",
            "ejectState.lines",
            "ejectState.stats.lines",
            "ejectState.stats.linesCleared",
            "ejectState.stats.lines_cleared",
            "gameState.linesCleared",
            "gameState.lines_cleared",
            "gameState.lines",
            "gameState.stats.lines",
            "gameState.stats.linesCleared",
            "gameState.stats.lines_cleared",
        ),
    )
    state_revision = payload.get("stateRevision")
    if state_revision is None:
        state_revision = payload.get("pieceCounter")
    try:
        state_revision = int(state_revision)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("stateRevision이 유효한 정수가 아닙니다.") from exc
    if state_revision < 0:
        raise SnapshotValidationError("stateRevision이 음수입니다.")

    piece_counter_raw = _extract_nonnegative_int(
        payload,
        (
            "pieceCounter",
            "piecesPlaced",
            "piecesplaced",
            "piececount",
            "stats.pieceCounter",
            "stats.piecesPlaced",
            "stats.pieces",
            "ejectState.pieceCounter",
            "ejectState.piecesPlaced",
            "ejectState.piecesplaced",
            "ejectState.piececount",
            "ejectState.stats.pieceCounter",
            "ejectState.stats.piecesPlaced",
            "ejectState.stats.pieces",
            "ejectBoardState.pieceCounter",
            "gameState.pieceCounter",
            "gameState.piecesPlaced",
            "gameState.piecesplaced",
            "gameState.piececount",
            "gameState.stats.pieceCounter",
            "gameState.stats.piecesPlaced",
            "gameState.stats.pieces",
            "boardState.pieceCounter",
        ),
    )
    piece_counter = piece_counter_raw
    derived_placed_pieces = _extract_nonnegative_int(
        payload,
        (
            "derivedPlacedPieces",
            "derived_placed_pieces",
            "derived.piecesPlaced",
            "state.derivedPlacedPieces",
            "gameState.derivedPlacedPieces",
        ),
    )

    game_id = str(payload.get("gameId")).strip() if payload.get("gameId") is not None else None
    round_id = str(payload.get("roundId")).strip() if payload.get("roundId") is not None else None
    session_id = str(payload.get("sessionId") or "").strip()
    if not session_id:
        raise SnapshotValidationError("sessionId가 비어 있습니다.")

    token = str(payload.get("token") or f"{session_id}:{state_revision}").strip()
    if not token:
        raise SnapshotValidationError("snapshot token이 비어 있습니다.")

    mode = str(payload.get("mode") or ("VS" if round_id else "Solo")).strip() or "Unknown"
    source = str(payload.get("source") or "browser_cdp").strip() or "browser_cdp"

    return NormalizedSnapshot(
        mode=mode,
        source=source,
        session_id=session_id,
        board=board,
        current=current,
        hold=hold,
        queue=queue,
        piece_counter=piece_counter,
        piece_counter_source=piece_counter_source,
        lines_cleared=lines_cleared,
        derived_placed_pieces=derived_placed_pieces,
        state_revision=state_revision,
        game_id=game_id,
        round_id=round_id,
        token=token,
        playing=True,
        ready=ready,
        captured_at=captured_at,
        age_ms=age_ms,
        board_width=board_width,
        board_height=board_height,
    )


class TetrioStateSource:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.lock = threading.RLock()
        self.proc = None
        self.reader_thread = None
        self.stdout_queue = queue.Queue()
        self.last_log_line = ""
        self.last_error = ""
        self.last_error_at = 0
        self.last_reason = "Waiting for game state"
        self.last_ready = False
        self.browser_connected = False
        self.last_valid_snapshot = None
        self.restart_times = []
        self.restart_backoff_sec = 0.5
        self.next_restart_allowed_at = 0.0
        self._last_session_id = None
        self._last_game_key = None
        self._last_piece_counter = None
        self._last_piece_counter_source = None
        self._last_captured_at = None
        self._last_token = None
        self._last_snapshot_seen = False
        self._pending_restart_record = False
        self._helper_started_at_ms = 0
        self._closing_pids = set()
        self._last_pipe_warning = ""
        self.connection_generation = 0
        self.last_snapshot_generation = 0
        self.last_snapshot_received_at_ms = 0
        self.last_snapshot_captured_at_ms = 0
        self.stdout_closed_at_ms = 0
        self.reader_exited_at_ms = 0
        self.helper_exit_code = None

    def _log(self, message):
        print(message)

    def _reset_sequence_guard(self) -> None:
        self._last_session_id = None
        self._last_game_key = None
        self._last_piece_counter = None
        self._last_piece_counter_source = None
        self._last_captured_at = None
        self._last_token = None

    def reload_config(self):
        with self.lock:
            self.config = load_config(self.config_path)
            self.close(restart=False)

    def _is_process_running(self):
        proc = self.proc
        return proc is not None and proc.poll() is None

    def _is_reader_thread_alive_locked(self):
        thread = self.reader_thread
        return bool(thread is not None and thread.is_alive())

    def is_process_alive(self):
        with self.lock:
            return self._is_process_running()

    def is_reader_alive(self):
        with self.lock:
            return self._is_reader_thread_alive_locked() and self.stdout_closed_at_ms <= 0

    def has_stdout_reader_closed(self):
        with self.lock:
            return self.stdout_closed_at_ms > 0

    def _snapshot_matches_action_locked(
        self,
        snapshot,
        *,
        max_age_sec=1.0,
        min_received_at_ms=None,
        generation=None,
    ):
        if snapshot is None:
            return False
        if generation is not None and snapshot.connection_generation != int(generation):
            return False
        if min_received_at_ms is not None and snapshot.received_at < int(min_received_at_ms):
            return False
        if not snapshot.ready or not snapshot.playing:
            return False
        if not snapshot.current or len(snapshot.queue or []) < int(self.config["tetrio_cdp"]["required_queue_length"]):
            return False
        age_ms = max(0, int(time.time() * 1000) - int(snapshot.captured_at))
        return age_ms <= int(max(100, float(max_age_sec) * 1000))

    def has_fresh_snapshot(
        self,
        *,
        max_age_sec=1.0,
        min_received_at_ms=None,
        generation=None,
    ):
        snapshot = self.get_latest_valid_snapshot(allow_start=False)
        with self.lock:
            return self._snapshot_matches_action_locked(
                snapshot,
                max_age_sec=max_age_sec,
                min_received_at_ms=min_received_at_ms,
                generation=generation,
            )

    def is_ready_for_action(
        self,
        *,
        max_age_sec=1.0,
        min_received_at_ms=None,
        generation=None,
    ):
        with self.lock:
            if not self._is_process_running():
                return False
            if not self.browser_connected or not self.last_ready:
                return False
            if not self._is_reader_thread_alive_locked():
                return False
            if self.stdout_closed_at_ms > 0:
                return False
        return self.has_fresh_snapshot(
            max_age_sec=max_age_sec,
            min_received_at_ms=min_received_at_ms,
            generation=generation,
        )

    def _calculate_idle_seconds_locked(self):
        now_ms = int(time.time() * 1000)
        reference_ms = self.last_snapshot_received_at_ms or self.last_snapshot_captured_at_ms or self._helper_started_at_ms
        if reference_ms <= 0:
            return 0.0
        return max(0.0, (now_ms - reference_ms) / 1000.0)

    def _get_max_restart_delay_sec(self):
        raw_value = self.config.get("tetrio_cdp", {}).get("max_restart_delay_sec", 5)
        try:
            return max(0.5, min(5.0, float(raw_value)))
        except (TypeError, ValueError):
            return 5.0

    def _clear_restart_backoff(self):
        with self.lock:
            self.restart_times.clear()
            self.next_restart_allowed_at = 0.0
            self.restart_backoff_sec = 0.5
            self._pending_restart_record = False

    def _mark_helper_success(self, reason=None):
        with self.lock:
            self.last_error = ""
            self.last_error_at = 0
            if reason:
                self.last_reason = reason
        self._clear_restart_backoff()

    def _remember_pipe_warning(self, text):
        with self.lock:
            if text == self._last_pipe_warning:
                return
            self._last_pipe_warning = text
            self.last_log_line = text

    def _schedule_restart_after_exit(self):
        with self.lock:
            self._pending_restart_record = True
            delay_sec = min(self._get_max_restart_delay_sec(), max(0.5, self.restart_backoff_sec))
            self.next_restart_allowed_at = time.time() + delay_sec
            self.restart_backoff_sec = min(self._get_max_restart_delay_sec(), max(0.5, delay_sec * 2.0))

    def start(self):
        with self.lock:
            if self._is_process_running():
                return
            if self.proc is not None and self.proc.poll() is not None:
                self.proc = None
            now = time.time()
            if self._pending_restart_record and now < self.next_restart_allowed_at:
                wait_sec = max(0.1, self.next_restart_allowed_at - now)
                raise RuntimeError(f"CDP source 재시작 대기 중입니다. {wait_sec:.1f}초 후 다시 시도합니다.")
            if self._pending_restart_record:
                self.restart_times.append(now)
                self._pending_restart_record = False
            self.next_restart_allowed_at = 0.0
            command = self._build_command()
            env = os.environ.copy()
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                command,
                cwd=str(get_resource_path()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )
            self.connection_generation += 1
            self.helper_exit_code = None
            self.stdout_closed_at_ms = 0
            self.reader_exited_at_ms = 0
            self._helper_started_at_ms = int(time.time() * 1000)
            self._last_snapshot_seen = False
            self.last_reason = "Waiting for game state"
            self.last_ready = False
            self.browser_connected = False
            self.last_valid_snapshot = None
            self.last_snapshot_generation = 0
            self.last_snapshot_received_at_ms = 0
            self.last_snapshot_captured_at_ms = 0
            self._reset_sequence_guard()
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

    def is_running(self):
        with self.lock:
            return self._is_process_running()

    def close(self, restart=False):
        with self.lock:
            proc = self.proc
            self.proc = None
            if proc is not None and getattr(proc, "pid", None) is not None:
                self._closing_pids.add(proc.pid)

        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        with self.lock:
            self.browser_connected = False
            self.last_ready = False
            self.last_valid_snapshot = None
            self.last_snapshot_generation = 0
            self.last_snapshot_received_at_ms = 0
            self.last_snapshot_captured_at_ms = 0
            self.stdout_closed_at_ms = 0
            self._reset_sequence_guard()
            if not restart and self.last_reason == "Browser connected":
                self.last_reason = "브라우저 연결 끊김"

    def wait_until_connected(self, timeout_sec=10.0):
        deadline = time.time() + max(0.1, float(timeout_sec))
        while time.time() < deadline:
            with self.lock:
                if (
                    self.browser_connected
                    and self.last_ready
                    and self._is_process_running()
                    and self._is_reader_thread_alive_locked()
                    and self.stdout_closed_at_ms <= 0
                ):
                    return True
                if self.proc is not None and self.proc.poll() is not None:
                    break
            time.sleep(0.05)
        with self.lock:
            detail = self.last_error or self.last_reason or "브라우저 연결 대기 중"
        raise RuntimeError(detail)

    def ensure_connected(self, timeout_sec=10.0):
        needs_reconnect = False
        with self.lock:
            needs_reconnect = (
                not self._is_process_running()
                or not self._is_reader_thread_alive_locked()
                or self.stdout_closed_at_ms > 0
                or not self.browser_connected
                or not self.last_ready
            )
        if needs_reconnect:
            self.reconnect()
        return self.wait_until_connected(timeout_sec=timeout_sec)

    def reconnect(self):
        self.close(restart=True)
        self.start()

    def mark_browser_closed(self, reason="브라우저 열기를 먼저 눌러주세요."):
        self.close(restart=False)
        with self.lock:
            self.last_reason = reason
            self.last_error = ""
            self.last_error_at = 0

    def get_latest_result(self, allow_start=False):
        snapshot = self.get_latest_valid_snapshot(allow_start=allow_start)
        if snapshot is None:
            return None
        return self._build_result_from_snapshot(snapshot)

    def _build_result_from_snapshot(self, snapshot):
        progress_details = resolve_effective_piece_progress_details(snapshot)
        pieces_count = progress_details["pieces_count"]
        return {
            "board": snapshot.board,
            "current": snapshot.current,
            "active_guess": snapshot.current,
            "hold": snapshot.hold or "",
            "queue": snapshot.queue,
            "pieces_count": pieces_count,
            "pc_round": calculate_pc_round(pieces_count),
            "piece_counter": snapshot.piece_counter,
            "piece_counter_source": snapshot.piece_counter_source,
            "lines_cleared": snapshot.lines_cleared,
            "derived_placed_pieces": snapshot.derived_placed_pieces,
            "fixed_cells": progress_details["fixed_cells"],
            "piece_progress_source": progress_details["source"],
            "pc_failure_reason": progress_details["failure_reason"],
            "state_revision": snapshot.state_revision,
            "snapshot": snapshot,
        }

    def prepare_result_for_action(
        self,
        action_name,
        *,
        action_started_at_ms,
        timeout_sec=8.0,
        snapshot_max_age_sec=1.0,
    ):
        deadline = time.time() + max(0.1, float(timeout_sec))
        action_started_at_ms = int(action_started_at_ms)
        self._log(f"[CDP ACTION] action={action_name}")

        reconnect_attempted = False
        while time.time() < deadline:
            with self.lock:
                generation = self.connection_generation
                process_alive = self._is_process_running()
                reader_alive = self._is_reader_thread_alive_locked()
                stdout_closed = self.stdout_closed_at_ms > 0
                last_ready = self.last_ready
                browser_connected = self.browser_connected

            self._log(f"[CDP ACTION] reader_alive={str(reader_alive and not stdout_closed).lower()}")

            snapshot = self.get_latest_valid_snapshot(allow_start=False)
            with self.lock:
                fresh_snapshot_ready = self._snapshot_matches_action_locked(
                    snapshot,
                    max_age_sec=snapshot_max_age_sec,
                    min_received_at_ms=action_started_at_ms,
                    generation=generation,
                )

            if process_alive and reader_alive and not stdout_closed and browser_connected and last_ready and fresh_snapshot_ready:
                self._log(f"[CDP ACTION] generation={generation}")
                self._log("[CDP ACTION] fresh_snapshot_ready=true")
                self._log(f"[CDP ACTION] running={action_name}")
                return self._build_result_from_snapshot(snapshot)

            if (not process_alive) or (not reader_alive) or stdout_closed or (not browser_connected) or (not last_ready):
                self._log(f"[CDP ACTION] reconnecting={str(True).lower()}")
                reconnect_attempted = True
                self.reconnect()
                self.wait_until_connected(timeout_sec=max(0.1, deadline - time.time()))
                continue

            time.sleep(0.05)

        if reconnect_attempted:
            self._log("[CDP ACTION] fresh_snapshot_ready=false")
        raise RuntimeError("TETR.IO 게임 상태를 읽지 못했습니다.")

    def get_latest_valid_snapshot(self, allow_start=False):
        with self.lock:
            if allow_start:
                try:
                    self._ensure_running()
                except Exception as exc:
                    self._remember_error(str(exc))
                    self.last_valid_snapshot = None
                    return None
            elif not self._is_process_running():
                self.last_valid_snapshot = None
                return None
            payload_info = self._read_snapshot_payload()
            if payload_info is None:
                self.last_reason = "Waiting for game state"
                self.last_valid_snapshot = None
                return None
            payload, received_at_ms = payload_info

            if not isinstance(payload, dict):
                self._remember_error("snapshot JSON이 객체가 아닙니다.")
                self.last_valid_snapshot = None
                return None

            try:
                captured_at = int(payload.get("capturedAt") or 0)
            except (TypeError, ValueError):
                captured_at = 0
            if (
                self._helper_started_at_ms > 0
                and captured_at > 0
                and captured_at < self._helper_started_at_ms
            ):
                self.last_reason = "Waiting for game state"
                self.last_valid_snapshot = None
                return None
            if (
                self._helper_started_at_ms > 0
                and received_at_ms > 0
                and received_at_ms < self._helper_started_at_ms
            ):
                self.last_reason = "Waiting for game state"
                self.last_valid_snapshot = None
                return None

            if payload.get("ready") is not True or payload.get("playing") is not True:
                self._reset_sequence_guard()
                self.last_valid_snapshot = None
                self.last_reason = "Waiting for game state"
                return None

            try:
                snapshot = normalize_snapshot_payload(
                    payload,
                    stale_after_ms=self.config["tetrio_cdp"]["stale_after_ms"],
                    required_queue_length=self.config["tetrio_cdp"]["required_queue_length"],
                )
            except SnapshotValidationError as exc:
                self.last_reason = str(exc)
                self._remember_error(str(exc))
                self.last_valid_snapshot = None
                return None

            snapshot = replace(
                snapshot,
                received_at=received_at_ms,
                connection_generation=self.connection_generation,
            )
            if self._last_session_id != snapshot.session_id:
                self._reset_sequence_guard()
                self._last_session_id = snapshot.session_id

            if self._last_captured_at is not None and snapshot.captured_at < self._last_captured_at:
                self.last_reason = "snapshot 시간이 역행했습니다."
                self._remember_error(self.last_reason)
                self.last_valid_snapshot = None
                return None

            if (
                self._last_piece_counter_source is not None
                and not is_piece_counter_source_transition_allowed(
                    self._last_piece_counter_source,
                    snapshot.piece_counter_source,
                )
            ):
                self.last_reason = "같은 세션에서 pieceCounter source가 변경되었습니다."
                self._remember_error(self.last_reason)
                self.last_valid_snapshot = None
                return None

            if (
                self._last_piece_counter is not None
                and snapshot.state_revision < self._last_piece_counter
            ):
                self.last_reason = "같은 세션에서 stateRevision이 감소했습니다."
                self._remember_error(self.last_reason)
                self.last_valid_snapshot = None
                return None

            self.last_valid_snapshot = snapshot
            self._last_game_key = snapshot.round_id or snapshot.game_id
            self._last_piece_counter = snapshot.state_revision
            self._last_piece_counter_source = snapshot.piece_counter_source
            self._last_captured_at = snapshot.captured_at
            self._last_token = snapshot.token
            self.last_snapshot_generation = snapshot.connection_generation
            self.last_snapshot_received_at_ms = snapshot.received_at
            self.last_snapshot_captured_at_ms = snapshot.captured_at
            self.last_reason = "Ready"
            self._mark_helper_success("Ready")
            return snapshot

    def get_status(self, allow_start=False):
        try:
            snapshot = self.get_latest_valid_snapshot(allow_start=allow_start)
        except Exception as exc:
            self._remember_error(f"상태 갱신 실패: {exc}")
            snapshot = None
        helper_running = self.is_process_alive()
        reader_alive = self.is_reader_alive()
        stdout_closed = self.has_stdout_reader_closed()
        browser_status = "Connected" if self.browser_connected else "Disconnected"
        game_state = "Ready"
        detail = self.last_reason or "브라우저 열기를 눌러주세요"

        if not helper_running:
            browser_status = "Disconnected"
            game_state = "Waiting"
            if self.last_error:
                detail = self.last_error
        elif stdout_closed or not reader_alive:
            browser_status = "Reconnecting"
            game_state = "Waiting"
            if self.last_error:
                detail = self.last_error
        elif not self.last_ready:
            browser_status = "Connecting"
            game_state = "Waiting"
        elif snapshot is None:
            reason = (self.last_reason or "").lower()
            if "stale" in reason:
                game_state = "Stale"
            elif "invalid" in reason or "유효" in reason or "queue" in reason or "piececounter" in reason:
                game_state = "Invalid"
            else:
                game_state = "Waiting"

        return {
            "browser_status": browser_status,
            "game_state": game_state,
            "mode": snapshot.mode if snapshot else "Unknown",
            "game_id": snapshot.game_id if snapshot else "",
            "round_id": snapshot.round_id if snapshot else "",
            "piece_counter": snapshot.piece_counter if snapshot else None,
            "board_size": f"{snapshot.board_width}x{snapshot.board_height}" if snapshot else "-",
            "current": snapshot.current if snapshot else "-",
            "hold": snapshot.hold if snapshot and snapshot.hold else "-",
            "queue": ",".join(snapshot.queue[:5]) if snapshot else "-",
            "last_update_age_ms": snapshot.age_ms if snapshot else None,
            "detail": detail,
            "last_log_line": self.last_log_line,
            "reader_alive": reader_alive,
            "process_alive": helper_running,
            "stdout_closed": stdout_closed,
            "connection_generation": self.connection_generation,
            "snapshot_generation": snapshot.connection_generation if snapshot else None,
            "received_at": snapshot.received_at if snapshot else None,
        }

    def _build_command(self):
        cdp_config = self.config["tetrio_cdp"]
        command = [
            self._get_node_executable(),
            str(get_resource_path("browser-source", "tetrio-cdp-source.mjs")),
            "--snapshot-path",
            str(self._resolve_runtime_path(cdp_config["snapshot_path"])),
            "--vs-object-snapshot-path",
            str(self._resolve_runtime_path(cdp_config["vs_object_snapshot_path"])),
            "--bridge-path",
            str(self._resolve_runtime_path(cdp_config["vs_bridge_path"])),
            "--port",
            str(cdp_config["port"]),
            "--url",
            str(cdp_config["url"]),
            "--target",
            str(cdp_config["target"]),
            "--poll-ms",
            str(cdp_config["poll_ms"]),
        ]
        command.extend(["--connect-only", "1"])
        if not cdp_config.get("probe_page_state", True):
            command.extend(["--probe-page-state", "0"])
        if not cdp_config.get("use_ribbon_websocket", True):
            command.extend(["--use-ribbon-websocket", "0"])
        if not cdp_config.get("use_seed_simulation_fallback", False):
            command.extend(["--use-seed-simulation-fallback", "0"])
        return command

    def _ensure_running(self):
        with self.lock:
            if self._is_process_running():
                return
            self.browser_connected = False
            self.last_ready = False
            self._reset_sequence_guard()
        try:
            self.start()
        except Exception as exc:
            self._remember_error(str(exc))
            raise

    def _reader_loop(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return

        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                self.last_log_line = line
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        payload = None
                    if payload and payload.get("type") == "ready":
                        self.last_ready = True
                        self.browser_connected = True
                        self._mark_helper_success("Browser connected")
                        continue

                lowered = line.lower()
                if "[browser] connected" in lowered:
                    self.browser_connected = True
                    self.last_ready = True
                    self._mark_helper_success("Browser connected")
                elif "[browser] fatal:" in lowered:
                    self._remember_error(line)
                elif "[browser]" in lowered:
                    self.last_reason = line.replace("[browser]", "").strip()
        except Exception as exc:
            self._remember_error(f"helper stdout reader failed: {exc!r}")
        finally:
            now_ms = int(time.time() * 1000)
            pid = getattr(proc, "pid", None)
            with self.lock:
                self.stdout_closed_at_ms = now_ms
            idle_seconds = 0.0
            with self.lock:
                idle_seconds = self._calculate_idle_seconds_locked()
            self._log(
                f"[CDP READER] stdout closed pid={pid if pid is not None else '?'} idle_seconds={idle_seconds:.2f}"
            )
            exit_code = proc.poll()
            if exit_code is None:
                self._remember_pipe_warning(
                    f"[CDP PIPE EOF] stdout closed while process is still alive pid={getattr(proc, 'pid', '?')}"
                )
                try:
                    exit_code = proc.wait()
                except Exception as exc:
                    self._remember_error(f"CDP helper wait 실패: {exc!r}")
                    exit_code = None

            if exit_code is not None:
                intentional_close = False
                with self.lock:
                    if pid is not None and pid in self._closing_pids:
                        self._closing_pids.discard(pid)
                        intentional_close = True

                    if self.proc is proc:
                        self.proc = None

                    if not intentional_close:
                        self.browser_connected = False
                        self.last_ready = False
                        self.last_valid_snapshot = None
                        self.last_snapshot_generation = 0
                        self.last_snapshot_received_at_ms = 0
                        self.last_snapshot_captured_at_ms = 0
                        self._reset_sequence_guard()
                        self.last_reason = "브라우저 연결 끊김"
                    self.helper_exit_code = exit_code
                    self.reader_exited_at_ms = int(time.time() * 1000)
                    if self.reader_thread is not None and self.reader_thread is threading.current_thread():
                        self.reader_thread = None

                self._log(f"[CDP READER] helper exited code={exit_code}")
                if not intentional_close:
                    self._remember_error(f"브라우저 연결 끊김 (helper exit code={exit_code})")

    def _read_snapshot_payload(self):
        snapshot_path = self._resolve_runtime_path(self.config["tetrio_cdp"]["snapshot_path"])
        if not snapshot_path.exists():
            if self._last_snapshot_seen:
                self._reset_sequence_guard()
                self.last_valid_snapshot = None
            self._last_snapshot_seen = False
            return None
        self._last_snapshot_seen = True
        try:
            stat = snapshot_path.stat()
            with snapshot_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            received_at_ms = int(stat.st_mtime_ns // 1_000_000)
            return payload, received_at_ms
        except json.JSONDecodeError as exc:
            self._remember_error(f"snapshot JSON 파싱 실패: {exc}")
            return None
        except OSError as exc:
            self._remember_error(f"snapshot 파일 읽기 실패: {exc}")
            return None

    def _resolve_runtime_path(self, raw_path):
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return get_runtime_data_dir() / path

    def _remember_error(self, text):
        now = time.time()
        if text != self.last_error or now - self.last_error_at >= 2.0:
            self.last_error = text
            self.last_error_at = now
        self.last_reason = text

    @staticmethod
    def _get_node_executable():
        return resolve_node_executable()
