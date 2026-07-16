import copy
import json
import os
import queue
import subprocess
import threading
import time

from app_paths import get_resource_path


VALID_PIECES = set("IJLOSTZ")
_SESSION_LOCK = threading.Lock()
_SESSION = None
_RESULT_CACHE = {}


class GomenError(Exception):
    pass


class GomenSession:
    def __init__(self):
        self.proc = None
        self.stdout_queue = queue.Queue()
        self.reader_thread = None
        self.lock = threading.Lock()
        self.last_reader_warning = ""
        self.last_reader_warning_at = 0.0

    def _log_reader_warning(self, message):
        now = time.monotonic()
        if message == self.last_reader_warning and now - self.last_reader_warning_at < 2.0:
            return
        self.last_reader_warning = message
        self.last_reader_warning_at = now
        print(f"[gomen reader] {message}")

    def ensure_started(self, timeout_sec=20):
        if self.proc is not None and self.proc.poll() is None:
            return

        script_path = get_gomen_solver_path()
        if not os.path.exists(script_path):
            raise GomenError(f"gomen_solver.js 없음: {script_path}")

        for asset_path in (get_gomen_js_path(), get_gomen_wasm_path(), get_legal_boards_path()):
            if not os.path.exists(asset_path):
                raise GomenError(f"gomen 자산 없음: {asset_path}")

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        self.proc = subprocess.Popen(
            [get_node_executable(), script_path],
            cwd=get_tools_dir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )
        self.stdout_queue = queue.Queue()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        ready = self._read_json(timeout_sec=max(10, timeout_sec))
        if ready.get("kind") != "ready":
            self.close()
            raise GomenError(f"gomen 시작 실패: {ready}")

    def _reader_loop(self):
        try:
            while self.proc is not None and self.proc.stdout is not None:
                try:
                    line = self.proc.stdout.readline()
                except Exception as exc:
                    self._log_reader_warning(f"stdout read failed: {exc!r}")
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

                if not line:
                    if self.proc.poll() is None:
                        self._log_reader_warning("stdout closed without process exit; retrying")
                        time.sleep(0.05)
                        continue
                    break

                try:
                    self.stdout_queue.put(line.rstrip("\r\n"))
                except Exception as exc:
                    self._log_reader_warning(f"stdout queue put failed: {exc!r}")
        finally:
            self.stdout_queue.put(None)

    def _read_json(self, timeout_sec=20):
        try:
            line = self.stdout_queue.get(timeout=timeout_sec)
        except queue.Empty as exc:
            raise GomenError("gomen 응답 시간 초과") from exc

        if line is None:
            if self.proc is not None and self.proc.poll() is not None:
                raise GomenError("gomen 프로세스가 종료됨")
            raise GomenError("gomen 출력 수신 실패")

        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise GomenError(f"gomen 응답 파싱 실패: {line}") from exc

    def solve(self, queue_text, garbage, hold=True, physics="SRS", limit=6, timeout_sec=20, target_queue=""):
        with self.lock:
            self.ensure_started(timeout_sec=timeout_sec)

            if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
                raise GomenError("gomen 세션이 종료됨")

            request = {
                "queue": queue_text,
                "target_queue": str(target_queue or ""),
                "garbage": str(int(garbage)),
                "hold": bool(hold),
                "physics": physics,
                "limit": int(limit),
            }

            self.proc.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            self.proc.stdin.flush()

            response = self._read_json(timeout_sec=timeout_sec)
            if not response.get("ok"):
                raise GomenError(response.get("error") or "gomen 계산 실패")

            return response

    def close(self):
        proc = self.proc
        self.proc = None

        if proc is None:
            return

        try:
            if proc.stdin is not None and proc.poll() is None:
                proc.stdin.close()
        except Exception:
            pass

        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def get_app_base_dir():
    return str(get_resource_path())


def get_tools_dir():
    return str(get_resource_path("tools"))


def get_node_executable():
    bundled = get_resource_path("tools", "node.exe")
    if bundled.exists():
        return str(bundled)
    return "node"


def get_gomen_solver_path():
    return os.path.join(get_tools_dir(), "gomen_solver.js")


def get_gomen_js_path():
    return os.path.join(get_tools_dir(), "gomen.js")


def get_gomen_wasm_path():
    return os.path.join(get_tools_dir(), "gomen_bg.wasm")


def get_legal_boards_path():
    return os.path.join(get_tools_dir(), "legal-boards.leb128")


def clean_piece_text(text):
    text = (text or "").upper().strip()
    return "".join(ch for ch in text if ch in VALID_PIECES)


def make_state_queue(active, queue, manual_see=""):
    manual_see = clean_piece_text(manual_see)
    if manual_see:
        return manual_see

    active = clean_piece_text(active)
    queue_text = "".join(piece for piece in (queue or []) if piece in VALID_PIECES)

    if not active:
        raise GomenError("ACTIVE 미노를 입력해야 함")

    return active[0] + queue_text


def make_gomen_queue(active, hold, queue, manual_see=""):
    state_queue = make_state_queue(active, queue, manual_see=manual_see)
    hold = clean_piece_text(hold)

    # gomen은 "초기 hold가 이미 차 있는 상태"를 직접 받지 못하므로,
    # hold + active + queue 를 empty-hold + can_hold 로 번역해 현재 상태를 근사한다.
    if hold and not clean_piece_text(manual_see):
        return hold[0] + state_queue

    return state_queue


def board_to_gomen_garbage(board):
    if not board or len(board) < 4:
        raise GomenError("보드 데이터가 부족함")

    value = 0
    bottom_rows = board[-4:]
    for row_offset, row in enumerate(reversed(bottom_rows)):
        for col_index, cell in enumerate((row or [])[:10]):
            if cell != ".":
                bit_index = row_offset * 10 + col_index
                value |= 1 << bit_index

    return value


def get_gomen_session():
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = GomenSession()
        return _SESSION


def close_gomen_sessions():
    global _SESSION
    with _SESSION_LOCK:
        session = _SESSION
        _SESSION = None

    if session is not None:
        session.close()


def warm_gomen_session(timeout_sec=20):
    session = get_gomen_session()
    session.ensure_started(timeout_sec=timeout_sec)


def run_gomen_solver(board, active, hold, queue, manual_see="", limit=6, physics="SRS", timeout_sec=20):
    state_queue = make_state_queue(active, queue, manual_see=manual_see)
    queue_text = make_gomen_queue(active, hold, queue, manual_see=manual_see)
    garbage = board_to_gomen_garbage(board)

    cache_key = (
        tuple("".join(row) for row in board[-4:]),
        queue_text,
        bool(True),
        physics,
        int(limit),
    )
    cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    session = get_gomen_session()
    result = session.solve(
        queue_text=queue_text,
        garbage=garbage,
        hold=True,
        physics=physics,
        limit=limit,
        timeout_sec=timeout_sec,
        target_queue=queue_text,
    )
    result["queue_text"] = queue_text
    result["state_queue"] = state_queue
    result["garbage"] = str(garbage)
    _RESULT_CACHE[cache_key] = copy.deepcopy(result)
    return result
