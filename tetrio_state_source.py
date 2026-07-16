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
        "max_restart_attempts": 3,
        "restart_window_sec": 60,
    }
}


class SnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedSnapshot:
    mode: str
    source: str
    board: list
    current: str
    hold: str | None
    queue: list
    piece_counter: int
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
    previous_snapshot=None,
):
    if not isinstance(payload, dict):
        raise SnapshotValidationError("snapshot JSON이 객체가 아닙니다.")

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
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

    piece_counter = payload.get("pieceCounter")
    try:
        piece_counter = int(piece_counter)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("pieceCounter가 유효한 정수가 아닙니다.") from exc
    if piece_counter < 0:
        raise SnapshotValidationError("pieceCounter가 음수입니다.")

    game_id = str(payload.get("gameId")).strip() if payload.get("gameId") is not None else None
    round_id = str(payload.get("roundId")).strip() if payload.get("roundId") is not None else None
    identity = round_id or game_id
    if not identity:
        raise SnapshotValidationError("gameId 또는 roundId가 없습니다.")

    token = str(payload.get("token") or f"{identity}:{piece_counter}").strip()
    if not token:
        raise SnapshotValidationError("snapshot token이 비어 있습니다.")

    if previous_snapshot is not None:
        if captured_at < previous_snapshot.captured_at:
            raise SnapshotValidationError("snapshot 시간이 역행했습니다.")
        if identity == (previous_snapshot.round_id or previous_snapshot.game_id):
            if piece_counter < previous_snapshot.piece_counter:
                raise SnapshotValidationError("같은 게임에서 pieceCounter가 감소했습니다.")

    mode = str(payload.get("mode") or ("VS" if round_id else "Solo")).strip() or "Unknown"
    source = str(payload.get("source") or "browser_cdp").strip() or "browser_cdp"
    ready = bool(payload.get("ready", True))

    return NormalizedSnapshot(
        mode=mode,
        source=source,
        board=board,
        current=current,
        hold=hold,
        queue=queue,
        piece_counter=piece_counter,
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

    def reload_config(self):
        with self.lock:
            self.config = load_config(self.config_path)
            self.close(restart=False)
            self.start()

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._prune_restarts()
            cdp_config = self.config["tetrio_cdp"]
            if len(self.restart_times) >= int(cdp_config.get("max_restart_attempts", 3)):
                raise RuntimeError("CDP source 재시작 한도를 초과했습니다.")

            self.restart_times.append(time.time())
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
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )
            self.last_ready = False
            self.browser_connected = False
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

    def close(self, restart=False):
        with self.lock:
            proc = self.proc
            self.proc = None

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
            self._ensure_running()
            payload = self._read_snapshot_payload()
            if payload is None:
                self.last_reason = "Waiting for game state"
                return None

            try:
                snapshot = normalize_snapshot_payload(
                    payload,
                    stale_after_ms=self.config["tetrio_cdp"]["stale_after_ms"],
                    required_queue_length=self.config["tetrio_cdp"]["required_queue_length"],
                    previous_snapshot=self.last_valid_snapshot,
                )
            except SnapshotValidationError as exc:
                self.last_reason = str(exc)
                self._remember_error(str(exc))
                return None

            self.last_valid_snapshot = snapshot
            self.last_reason = "Ready"
            return snapshot

    def get_status(self):
        snapshot = self.get_latest_valid_snapshot()
        browser_status = "Connected" if self.browser_connected else "Disconnected"
        game_state = "Ready"
        detail = self.last_reason

        if self.proc is None or self.proc.poll() is not None:
            browser_status = "Disconnected"
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
        if self.proc is not None and self.proc.poll() is None:
            return
        self.start()

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
                        self.last_reason = "Browser connected"
                        continue

                lowered = line.lower()
                if "[browser] connected" in lowered:
                    self.browser_connected = True
                    self.last_ready = True
                elif "[browser] fatal:" in lowered:
                    self._remember_error(line)
                elif "[browser]" in lowered:
                    self.last_reason = line.replace("[browser]", "").strip()
        finally:
            if proc.poll() is not None:
                self.browser_connected = False
                self.last_ready = False

    def _read_snapshot_payload(self):
        snapshot_path = self._resolve_runtime_path(self.config["tetrio_cdp"]["snapshot_path"])
        if not snapshot_path.exists():
            return None
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

    def _prune_restarts(self):
        window_sec = int(self.config["tetrio_cdp"].get("restart_window_sec", 60))
        cutoff = time.time() - max(1, window_sec)
        self.restart_times = [item for item in self.restart_times if item >= cutoff]

    @staticmethod
    def _get_node_executable():
        bundled = get_resource_path("tools", "node.exe")
        if bundled.exists():
            return str(bundled)
        return "node"
