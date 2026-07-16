import copy
from collections import deque
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
        self.stderr_thread = None
        self.lock = threading.RLock()
        self.closing = False
        self.last_reader_warning = ""
        self.last_reader_warning_at = 0.0
        self.last_exit_code = None
        self.last_command = []
        self.last_cwd = ""
        self.last_stdout_lines = deque(maxlen=30)
        self.last_stderr_lines = deque(maxlen=30)

    def _log_reader_warning(self, message):
        now = time.monotonic()
        if message == self.last_reader_warning and now - self.last_reader_warning_at < 2.0:
            return
        self.last_reader_warning = message
        self.last_reader_warning_at = now
        print(f"[gomen reader] {message}")

    def _remember_stdout_line(self, line):
        text = str(line.rstrip("\r\n"))
        if text:
            self.last_stdout_lines.append(text)

    def _remember_stderr_line(self, line):
        text = str(line.rstrip("\r\n"))
        if text:
            self.last_stderr_lines.append(text)

    def _format_process_debug(self):
        command_text = " ".join(str(part) for part in (self.last_command or [])) or "-"
        stdout_lines = list(self.last_stdout_lines)
        stderr_lines = list(self.last_stderr_lines)
        stdout_text = "\n".join(stdout_lines) if stdout_lines else "-"
        stderr_text = "\n".join(stderr_lines) if stderr_lines else "-"
        return (
            f"exit_code={self.last_exit_code!r}\n"
            f"command={command_text}\n"
            f"cwd={self.last_cwd or '-'}\n"
            f"last_stdout_30=\n{stdout_text}\n"
            f"last_stderr_30=\n{stderr_text}"
        )

    def _shutdown_process_for_eof(self, proc, *, quiet=False):
        if proc is None:
            return None
        exit_code = proc.poll()
        if exit_code is not None:
            return exit_code

        if not quiet:
            self._log_reader_warning(
                f"stdout closed while process is still alive pid={getattr(proc, 'pid', '?')}"
            )

        try:
            proc.terminate()
        except Exception:
            pass

        try:
            exit_code = proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                exit_code = proc.wait(timeout=1.0)
            except Exception:
                exit_code = proc.poll()
        return exit_code

    def _stderr_loop(self, proc):
        stderr = proc.stderr
        if stderr is None:
            return
        try:
            for raw_line in stderr:
                self._remember_stderr_line(raw_line)
        except Exception:
            pass

    def ensure_started(self, timeout_sec=20):
        with self.lock:
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
            command = [get_node_executable(), script_path]
            cwd = get_tools_dir()

            self.proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )
            proc = self.proc
            self.closing = False
            self.last_exit_code = None
            self.last_command = list(command)
            self.last_cwd = cwd
            self.last_stdout_lines.clear()
            self.last_stderr_lines.clear()
            self.stdout_queue = queue.Queue()
            self.reader_thread = threading.Thread(target=self._reader_loop, args=(proc,), daemon=True)
            self.reader_thread.start()
            self.stderr_thread = threading.Thread(target=self._stderr_loop, args=(proc,), daemon=True)
            self.stderr_thread.start()

        ready = self._read_json(timeout_sec=max(10, timeout_sec))
        if ready.get("kind") != "ready":
            self.close()
            raise GomenError(f"gomen 시작 실패: {ready}\n{self._format_process_debug()}")

    def _reader_loop(self, proc):
        stdout = proc.stdout
        if stdout is None:
            self.stdout_queue.put(None)
            return

        try:
            for line in stdout:
                self._remember_stdout_line(line)
                self.stdout_queue.put(line.rstrip("\r\n"))
        finally:
            quiet = False
            with self.lock:
                quiet = self.closing
            exit_code = proc.poll()
            if exit_code is None:
                exit_code = self._shutdown_process_for_eof(proc, quiet=quiet)
            self.last_exit_code = exit_code
            with self.lock:
                if self.proc is proc:
                    self.proc = None
            self.stdout_queue.put(None)

    def _read_json(self, timeout_sec=20):
        try:
            line = self.stdout_queue.get(timeout=timeout_sec)
        except queue.Empty as exc:
            raise GomenError("gomen 응답 시간 초과") from exc

        if line is None:
            if self.proc is not None and self.proc.poll() is not None:
                self.last_exit_code = self.proc.poll()
            if self.last_exit_code is not None:
                raise GomenError(f"gomen 프로세스가 종료됨\n{self._format_process_debug()}")
            raise GomenError(f"gomen 출력 수신 실패\n{self._format_process_debug()}")

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
        with self.lock:
            self.closing = True
            proc = self.proc
            self.proc = None

        if proc is None:
            return

        for stream_name in ("stdin", "stdout", "stderr"):
            try:
                stream = getattr(proc, stream_name, None)
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        self.last_exit_code = proc.poll()
        for thread in (self.reader_thread, self.stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)


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


def get_gomen_bottom_rows(board):
    if not board or len(board) < 4:
        raise GomenError("보드 데이터가 부족함")

    rows = []
    for row in board[-4:]:
        text = "".join("X" if cell != "." else "." for cell in (row or [])[:10])
        rows.append((text + "." * 10)[:10])
    return rows


def board_to_gomen_garbage(board):
    """
    Encode the visible bottom 4 rows for Gomen.

    Mapping:
    - only the bottom 4 visible rows are encoded
    - bit 0 = bottom row, leftmost column
    - bits increase left -> right within a row
    - then continue bottom -> top across the 4 rows
    - occupied cell = 1, empty cell = 0
    """
    value = 0
    bottom_rows = get_gomen_bottom_rows(board)
    for row_offset, row in enumerate(reversed(bottom_rows)):
        for col_index, cell in enumerate(row[:10]):
            if cell != ".":
                bit_index = row_offset * 10 + col_index
                value |= 1 << bit_index

    return value


def gomen_garbage_to_bottom_rows(garbage):
    try:
        value = int(garbage)
    except (TypeError, ValueError) as exc:
        raise GomenError("garbage 값이 유효한 정수가 아닙니다.") from exc

    rows = []
    for row_offset in range(3, -1, -1):
        cells = []
        for col_index in range(10):
            bit_index = row_offset * 10 + col_index
            cells.append("X" if (value >> bit_index) & 1 else ".")
        rows.append("".join(cells))
    return rows


def format_gomen_garbage_bits(garbage):
    return format(int(garbage), "040b")


def build_gomen_debug_payload(board, active, hold, queue, state_queue, queue_text, garbage, branch_name):
    return {
        "branch_name": str(branch_name or ""),
        "bottom_rows": get_gomen_bottom_rows(board),
        "garbage": int(garbage),
        "garbage_bits": format_gomen_garbage_bits(garbage),
        "active": clean_piece_text(active),
        "hold": clean_piece_text(hold),
        "raw_queue": [piece for piece in (queue or []) if piece in VALID_PIECES],
        "state_queue": str(state_queue or ""),
        "queue_text": str(queue_text or ""),
    }


def log_gomen_request(debug_payload):
    print(f"[GOMEN REQUEST] branch={debug_payload['branch_name'] or '-'}")
    for index, row_text in enumerate(debug_payload["bottom_rows"], start=1):
        print(f"[GOMEN REQUEST] bottom_row_{index}={row_text}")
    print(f"[GOMEN REQUEST] garbage_dec={debug_payload['garbage']}")
    print(f"[GOMEN REQUEST] garbage_bits={debug_payload['garbage_bits']}")
    print(f"[GOMEN REQUEST] active={debug_payload['active'] or '-'}")
    print(f"[GOMEN REQUEST] hold={debug_payload['hold'] or '-'}")
    print(f"[GOMEN REQUEST] raw_queue={debug_payload['raw_queue']}")
    print(f"[GOMEN REQUEST] state_queue={debug_payload['state_queue'] or '-'}")
    print(f"[GOMEN REQUEST] queue_text={debug_payload['queue_text'] or '-'}")


def build_gomen_branches(active, hold, queue, manual_see=""):
    state_queue = make_state_queue(active, queue, manual_see=manual_see)
    hold_piece = clean_piece_text(hold)
    branches = [
        {
            "name": "active-first",
            "queue_text": state_queue,
            "target_queue": state_queue,
            "hold": True,
        }
    ]
    if hold_piece and not clean_piece_text(manual_see):
        branches.append(
            {
                "name": "hold-first",
                "queue_text": hold_piece[0] + state_queue,
                "target_queue": hold_piece[0] + state_queue,
                "hold": True,
            }
        )
    return state_queue, branches


def _run_gomen_branch(session, branch, garbage, *, timeout_sec, physics, limit):
    result = session.solve(
        queue_text=branch["queue_text"],
        garbage=garbage,
        hold=branch.get("hold", True),
        physics=physics,
        limit=limit,
        timeout_sec=timeout_sec,
        target_queue=branch.get("target_queue", ""),
    )
    result["branch_name"] = branch["name"]
    result["engine_zero"] = int(result.get("total") or 0) == 0
    result["exact_queue_filter_miss"] = (
        int(result.get("total") or 0) > 0 and not bool(result.get("exact_match_used"))
    )
    return result


def _select_best_gomen_branch_result(branch_results):
    def sort_key(item):
        return (
            1 if int(item.get("total") or 0) > 0 else 0,
            1 if int(item.get("shown_total") or 0) > 0 else 0,
            int(item.get("total") or 0),
            int(item.get("shown_total") or 0),
            -int(item.get("branch_index") or 0),
        )

    return max(branch_results, key=sort_key)


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
    state_queue, branches = build_gomen_branches(active, hold, queue, manual_see=manual_see)
    garbage = board_to_gomen_garbage(board)
    hold_text = clean_piece_text(hold)
    raw_queue = tuple(piece for piece in (queue or []) if piece in VALID_PIECES)

    cache_key = (
        tuple(get_gomen_bottom_rows(board)),
        clean_piece_text(active),
        hold_text,
        raw_queue,
        clean_piece_text(manual_see),
        physics,
        int(limit),
    )
    cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    session = get_gomen_session()
    branch_results = []
    for branch_index, branch in enumerate(branches):
        debug_payload = build_gomen_debug_payload(
            board,
            active,
            hold,
            queue,
            state_queue,
            branch["queue_text"],
            garbage,
            branch["name"],
        )
        log_gomen_request(debug_payload)
        result = _run_gomen_branch(
            session,
            branch,
            garbage,
            timeout_sec=timeout_sec,
            physics=physics,
            limit=limit,
        )
        result["branch_index"] = branch_index
        result["debug_request"] = debug_payload
        branch_results.append(result)

    selected = copy.deepcopy(_select_best_gomen_branch_result(branch_results))
    selected["branch_results"] = copy.deepcopy(branch_results)
    selected["queue_text"] = selected["debug_request"]["queue_text"]
    selected["state_queue"] = state_queue
    selected["garbage"] = str(garbage)
    _RESULT_CACHE[cache_key] = copy.deepcopy(selected)
    return selected
