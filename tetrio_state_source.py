import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app_paths import get_resource_path, get_runtime_data_dir, resolve_runtime_file


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
        "auto_launch_chromium": True,
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
    state_revision = payload.get("stateRevision")
    if state_revision is None:
        state_revision = payload.get("pieceCounter")
    try:
        state_revision = int(state_revision)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("stateRevision이 유효한 정수가 아닙니다.") from exc
    if state_revision < 0:
        raise SnapshotValidationError("stateRevision이 음수입니다.")

    piece_counter_raw = payload.get("pieceCounter")
    piece_counter = None
    if piece_counter_source != "derived-revision" or piece_counter_raw is not None:
        try:
            piece_counter = int(piece_counter_raw)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("pieceCounter가 유효한 정수가 아닙니다.") from exc
        if piece_counter < 0:
            raise SnapshotValidationError("pieceCounter가 음수입니다.")

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
            self.start()

    def _is_process_running(self):
        proc = self.proc
        return proc is not None and proc.poll() is None

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
            self._helper_started_at_ms = int(time.time() * 1000)
            self._last_snapshot_seen = False
            self.last_reason = "Waiting for game state"
            self.last_ready = False
            self.browser_connected = False
            self.last_valid_snapshot = None
            self._reset_sequence_guard()
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

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

        if not restart:
            self.browser_connected = False
            self.last_ready = False
        self.last_valid_snapshot = None
        self._reset_sequence_guard()

    def get_latest_result(self):
        snapshot = self.get_latest_valid_snapshot()
        if snapshot is None:
            return None
        return {
            "board": snapshot.board,
            "current": snapshot.current,
            "active_guess": snapshot.current,
            "hold": snapshot.hold or "",
            "queue": snapshot.queue,
            "pieces_count": snapshot.piece_counter,
            "pc_round": calculate_pc_round(snapshot.piece_counter),
            "snapshot": snapshot,
        }

    def get_latest_valid_snapshot(self):
        with self.lock:
            try:
                self._ensure_running()
            except Exception as exc:
                self._remember_error(str(exc))
                self.last_valid_snapshot = None
                return None
            payload = self._read_snapshot_payload()
            if payload is None:
                self.last_reason = "Waiting for game state"
                self.last_valid_snapshot = None
                return None

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
                and snapshot.piece_counter_source != self._last_piece_counter_source
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
            self.last_reason = "Ready"
            self._mark_helper_success("Ready")
            return snapshot

    def get_status(self):
        try:
            snapshot = self.get_latest_valid_snapshot()
        except Exception as exc:
            self._remember_error(f"상태 갱신 실패: {exc}")
            snapshot = None
        helper_running = self._is_process_running()
        browser_status = "Connected" if self.browser_connected else "Disconnected"
        game_state = "Ready"
        detail = self.last_reason

        if not helper_running:
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
        if not cdp_config.get("auto_launch_chromium", True):
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
                pid = getattr(proc, "pid", None)
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
                        self._reset_sequence_guard()

                if not intentional_close:
                    self._schedule_restart_after_exit()
                    self._remember_error(f"CDP helper가 종료되었습니다. exit code={exit_code}")

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
            with snapshot_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
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
        bundled = get_resource_path("tools", "node.exe")
        if bundled.exists():
            return str(bundled)
        return "node"
