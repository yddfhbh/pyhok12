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

    def solve_state(
        self,
        *,
        current,
        initial_hold="",
        next_queue="",
        garbage,
        can_hold=True,
        physics="TETRIO",
        limit=6,
        timeout_sec=20,
    ):
        with self.lock:
            self.ensure_started(timeout_sec=timeout_sec)

            if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
                raise GomenError("gomen 세션이 종료되었습니다.")

            request = {
                "mode": "state",
                "current": clean_piece_text(current)[:1],
                "initial_hold": clean_piece_text(initial_hold)[:1],
                "next_queue": clean_piece_text(next_queue),
                "garbage": str(int(garbage)),
                "can_hold": bool(can_hold),
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


def count_pc_solver_fixed_cells(board):
    return sum(1 for row in get_gomen_bottom_rows(board) for cell in row if cell != ".")


def calculate_solver_piece_limit(board):
    fixed_cells = count_pc_solver_fixed_cells(board)
    if fixed_cells < 0 or fixed_cells > 40:
        raise GomenError(f"고정 셀 수가 유효 범위를 벗어남: {fixed_cells}")

    remaining_cells = 40 - fixed_cells
    if remaining_cells < 0 or remaining_cells % 4 != 0:
        raise GomenError(
            f"PC solver용 고정 셀 수가 잘못되었습니다. fixed_cells={fixed_cells} remaining_cells={remaining_cells}"
        )

    placements_needed = remaining_cells // 4
    solver_piece_limit = placements_needed + 1
    return {
        "fixed_cells": fixed_cells,
        "remaining_cells": remaining_cells,
        "placements_needed": placements_needed,
        "solver_piece_limit": solver_piece_limit,
    }


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


def build_gomen_debug_payload(
    board,
    active,
    hold,
    queue,
    state_queue,
    next_queue,
    garbage,
    branch_name,
    solver_context,
    use_hold,
):
    return {
        "branch_name": str(branch_name or ""),
        "bottom_rows": get_gomen_bottom_rows(board),
        "garbage": int(garbage),
        "garbage_bits": format_gomen_garbage_bits(garbage),
        "active": clean_piece_text(active),
        "hold": clean_piece_text(hold),
        "raw_queue": "".join(piece for piece in (queue or []) if piece in VALID_PIECES),
        "state_queue": str(state_queue or ""),
        "next_queue": str(next_queue or ""),
        "next_length": len(str(next_queue or "")),
        "fixed_cells": int(solver_context["fixed_cells"]),
        "remaining_cells": int(solver_context["remaining_cells"]),
        "placements_needed": int(solver_context["placements_needed"]),
        "solver_piece_limit": int(solver_context["solver_piece_limit"]),
        "use_hold": bool(use_hold),
    }


def log_gomen_request(debug_payload):
    print(f"[GOMEN REQUEST] branch={debug_payload['branch_name'] or '-'}")
    print(f"[GOMEN REQUEST] fixed_cells={debug_payload['fixed_cells']}")
    print(f"[GOMEN REQUEST] remaining_cells={debug_payload['remaining_cells']}")
    print(f"[GOMEN REQUEST] placements_needed={debug_payload['placements_needed']}")
    print(f"[GOMEN REQUEST] solver_piece_limit={debug_payload['solver_piece_limit']}")
    for index, row_text in enumerate(debug_payload["bottom_rows"], start=1):
        print(f"[GOMEN REQUEST] bottom_row_{index}={row_text}")
    print(f"[GOMEN REQUEST] garbage_dec={debug_payload['garbage']}")
    print(f"[GOMEN REQUEST] garbage_bits={debug_payload['garbage_bits']}")
    print(f"[GOMEN REQUEST] active={debug_payload['active'] or '-'}")
    print(f"[GOMEN REQUEST] hold={debug_payload['hold'] or '-'}")
    print(f"[GOMEN REQUEST] raw_queue={debug_payload['raw_queue']}")
    print(f"[GOMEN REQUEST] state_queue={debug_payload['state_queue'] or '-'}")
    print(f"[GOMEN REQUEST] next_queue={debug_payload['next_queue'] or '-'}")
    print(f"[GOMEN REQUEST] next_length={debug_payload['next_length']}")
    print(f"[GOMEN REQUEST] use_hold={'true' if debug_payload['use_hold'] else 'false'}")


def build_gomen_branches(active, hold, queue, manual_see="", solver_piece_limit=None):
    state_queue = make_state_queue(active, queue, manual_see=manual_see)
    current_piece = clean_piece_text(state_queue[:1])
    hold_piece = clean_piece_text(hold)[:1]
    next_source = clean_piece_text(state_queue[1:])
    required_total = int(solver_piece_limit) if solver_piece_limit is not None else len(state_queue)
    required_next = max(0, required_total - 1 - (1 if hold_piece else 0))
    next_queue = next_source[:required_next]
    available_total = 1 + len(next_source) + (1 if hold_piece else 0)

    return state_queue, [
        {
            "name": "exact-state",
            "current": current_piece,
            "initial_hold": hold_piece,
            "next_queue": next_queue,
            "can_hold": True,
            "available_total": available_total,
            "required_total": required_total,
            "required_next": required_next,
        }
    ]


def _run_gomen_branch(session, branch, garbage, *, timeout_sec, physics, limit):
    result = session.solve_state(
        current=branch["current"],
        initial_hold=branch.get("initial_hold", ""),
        next_queue=branch.get("next_queue", ""),
        garbage=garbage,
        can_hold=branch.get("can_hold", True),
        physics=physics,
        limit=limit,
        timeout_sec=timeout_sec,
    )
    result["branch_name"] = branch["name"]
    result["engine_zero"] = int(result.get("total") or 0) == 0
    result["exact_queue_filter_miss"] = False
    return result


def _log_gomen_branch_response(result):
    print(
        f"[GOMEN RESPONSE] branch={result.get('branch_name') or '-'} "
        f"raw_total={int(result.get('total') or 0)} "
        f"matched_total={int(result.get('matched_total') or 0)} "
        f"shown_total={int(result.get('shown_total') or 0)} "
        f"solutions={len(result.get('solutions') or [])}"
    )


def _solution_cache_key(solution):
    cells = str(solution.get("cells") or "")
    placements = tuple(_normalize_piece_list(solution.get("placements")))
    hold_actions = tuple(_normalize_hold_actions(solution.get("hold_actions"), len(placements)))
    final_hold = clean_piece_text(solution.get("final_hold"))[:1]
    consumed_next_count = int(solution.get("consumed_next_count") or 0)
    if cells and placements:
        return ("exact-state", cells, placements, hold_actions, final_hold, consumed_next_count)
    return ("json", json.dumps(solution, ensure_ascii=True, sort_keys=True))


def _annotate_branch_solutions(result):
    branch_name = str(result.get("branch_name") or "")
    state_queue = str(result.get("state_queue") or "")
    for solution in result.get("solutions") or []:
        solution.setdefault("branch_name", branch_name)
        solution.setdefault("state_queue", state_queue)
    return result


def _normalize_piece_list(value):
    if isinstance(value, str):
        return [piece for piece in clean_piece_text(value)]

    pieces = []
    for item in value or []:
        piece = clean_piece_text(item)[:1]
        if piece:
            pieces.append(piece)
    return pieces


def _normalize_hold_actions(value, expected_length):
    actions = []
    for item in value or []:
        actions.append(bool(item))
    if len(actions) < expected_length:
        actions.extend([False] * (expected_length - len(actions)))
    return actions[:expected_length]


def _serialize_solution_for_log(solution):
    try:
        return json.dumps(solution, ensure_ascii=True, sort_keys=True)
    except Exception:
        return str(solution)


def _log_invalid_solution(reason, solution, current, hold, next_queue):
    print(f"[PC SOLVER FILTER] reason={reason}")
    print(f"[PC SOLVER FILTER] solution={_serialize_solution_for_log(solution)}")
    print(f"[PC SOLVER FILTER] current={current or '-'}")
    print(f"[PC SOLVER FILTER] hold={hold or '-'}")
    print(f"[PC SOLVER FILTER] next={next_queue or '-'}")


def _validate_solution_sequence(solution, current, hold, next_queue):
    placements = _normalize_piece_list(solution.get("placements"))
    hold_actions = _normalize_hold_actions(solution.get("hold_actions"), len(placements))
    active_piece = clean_piece_text(current)[:1]
    hold_piece = clean_piece_text(hold)[:1]
    next_pieces = list(clean_piece_text(next_queue))
    next_index = 0

    if not active_piece:
        return False, "missing_current", None
    if not placements:
        return False, "missing_placements", None

    raw_initial_current = solution.get("initial_current")
    raw_initial_hold = solution.get("initial_hold")
    solution_current = clean_piece_text(raw_initial_current)[:1]
    solution_hold = clean_piece_text(raw_initial_hold)[:1]
    if raw_initial_current not in (None, "") and solution_current != active_piece:
        return False, "initial_current_mismatch", None
    if raw_initial_hold not in (None, "") and solution_hold != hold_piece:
        return False, "initial_hold_mismatch", None

    for step_index, (placement_piece, used_hold) in enumerate(zip(placements, hold_actions)):
        current_piece = active_piece
        if used_hold:
            if hold_piece:
                current_piece, hold_piece = hold_piece, current_piece
            else:
                if next_index >= len(next_pieces):
                    return False, "invalid_hold_transition", None
                hold_piece = current_piece
                current_piece = next_pieces[next_index]
                next_index += 1

        if placement_piece != current_piece:
            if not used_hold and placement_piece in next_pieces[next_index:]:
                return False, "invalid_next_order", None
            return False, "invalid_hold_transition", None

        if step_index < len(placements) - 1:
            if next_index >= len(next_pieces):
                return False, "invalid_next_order", None
            active_piece = next_pieces[next_index]
            next_index += 1

    final_hold = clean_piece_text(solution.get("final_hold"))[:1]
    if final_hold != hold_piece:
        return False, "final_hold_mismatch", None

    consumed_next_count = int(solution.get("consumed_next_count") or 0)
    if consumed_next_count != next_index:
        return False, "consumed_next_count_mismatch", None

    normalized = copy.deepcopy(solution)
    normalized["placements"] = placements
    normalized["hold_actions"] = hold_actions
    normalized["initial_current"] = active_piece = clean_piece_text(current)[:1]
    normalized["initial_hold"] = clean_piece_text(hold)[:1]
    normalized["initial_next"] = clean_piece_text(next_queue)
    normalized["final_hold"] = hold_piece
    normalized["consumed_next_count"] = next_index
    normalized["physics"] = str(solution.get("physics") or "")
    return True, "", normalized


def _filter_valid_solutions(result, *, current, hold, next_queue):
    filtered = []
    seen_solution_keys = set()
    for solution in result.get("solutions") or []:
        is_valid, reason, normalized = _validate_solution_sequence(
            solution,
            current=current,
            hold=hold,
            next_queue=next_queue,
        )
        if not is_valid:
            _log_invalid_solution(reason, solution, current, hold, next_queue)
            continue
        key = _solution_cache_key(normalized)
        if key in seen_solution_keys:
            continue
        seen_solution_keys.add(key)
        filtered.append(normalized)
    return filtered


def _build_branch_cache_key(
    *,
    garbage,
    branch_name,
    queue_text,
    placements_needed,
    solver_piece_limit,
    physics,
    use_hold,
    current,
    hold,
):
    return (
        int(garbage),
        str(branch_name or ""),
        str(queue_text or "").strip().upper(),
        int(placements_needed),
        int(solver_piece_limit),
        str(physics or "").strip().upper(),
        bool(use_hold),
        clean_piece_text(current),
        clean_piece_text(hold),
    )


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


def _run_gomen_solver_exact(board, active, hold, queue, manual_see="", limit=6, physics="TETRIO", timeout_sec=20):
    garbage = board_to_gomen_garbage(board)
    solver_context = calculate_solver_piece_limit(board)
    state_queue, branches = build_gomen_branches(
        active,
        hold,
        queue,
        manual_see=manual_see,
        solver_piece_limit=solver_context["solver_piece_limit"],
    )
    hold_text = clean_piece_text(hold)[:1]
    session = get_gomen_session()
    branch = branches[0]
    available_total = int(branch.get("available_total") or 0)
    required_total = int(branch.get("required_total") or 0)
    if available_total < required_total:
        raise GomenError(f"PC SOLVER: NEXT 큐 부족\n필요={required_total}, 확보={available_total}")

    debug_payload = build_gomen_debug_payload(
        board,
        active,
        hold,
        queue,
        state_queue,
        branch["next_queue"],
        garbage,
        branch["name"],
        solver_context,
        branch.get("can_hold", True),
    )
    log_gomen_request(debug_payload)
    cache_key = _build_branch_cache_key(
        garbage=garbage,
        branch_name=branch["name"],
        queue_text=branch["next_queue"],
        placements_needed=solver_context["placements_needed"],
        solver_piece_limit=solver_context["solver_piece_limit"],
        physics=physics,
        use_hold=branch.get("can_hold", True),
        current=branch["current"],
        hold=hold_text,
    )
    cached_branch = _RESULT_CACHE.get(cache_key)
    if cached_branch is not None:
        result = copy.deepcopy(cached_branch)
    else:
        result = _run_gomen_branch(
            session,
            branch,
            garbage,
            timeout_sec=timeout_sec,
            physics=physics,
            limit=limit,
        )
        _RESULT_CACHE[cache_key] = copy.deepcopy(result)

    result["branch_index"] = 0
    result["debug_request"] = debug_payload
    result["state_queue"] = state_queue
    result["next_queue"] = branch["next_queue"]
    result["initial_current"] = branch["current"]
    result["initial_hold"] = hold_text

    filtered_solutions = _filter_valid_solutions(
        result,
        current=branch["current"],
        hold=hold_text,
        next_queue=branch["next_queue"],
    )
    result["solutions"] = filtered_solutions[: int(limit)]
    result["shown_total"] = len(result["solutions"])
    result["matched_total"] = len(result["solutions"])
    result["exact_match_used"] = True
    result["engine_zero"] = int(result.get("total") or 0) == 0
    result["exact_queue_filter_miss"] = False
    if int(result.get("total") or 0) > 0 and not result["solutions"]:
        result["no_valid_sequence"] = True
        result["display_message"] = "PC SOLVER: 유효한 실제 게임 순서 해법 없음"

    _annotate_branch_solutions(result)
    _log_gomen_branch_response(result)
    result["branch_results"] = [copy.deepcopy(result)]
    result["garbage"] = str(garbage)
    return result


def run_gomen_solver(board, active, hold, queue, manual_see="", limit=6, physics="TETRIO", timeout_sec=20):
    return _run_gomen_solver_exact(
        board,
        active,
        hold,
        queue,
        manual_see=manual_see,
        limit=limit,
        physics=physics,
        timeout_sec=timeout_sec,
    )

    garbage = board_to_gomen_garbage(board)
    solver_context = calculate_solver_piece_limit(board)
    state_queue, branches = build_gomen_branches(
        active,
        hold,
        queue,
        manual_see=manual_see,
        solver_piece_limit=solver_context["solver_piece_limit"],
    )
    hold_text = clean_piece_text(hold)
    success_results = []
    branch_errors = []
    shortage_lengths = []
    session = get_gomen_session()
    branch_results = []
    for branch_index, branch in enumerate(branches):
        branch_available = int(branch.get("available_length") or 0)
        branch_required = int(branch.get("required_length") or 0)
        if branch_available < branch_required:
            shortage_lengths.append(branch_available)
            branch_error = GomenError(
                f"PC SOLVER: NEXT 큐 부족\n필요={branch_required}, 확보={branch_available}"
            )
            branch_results.append(
                {
                    "ok": False,
                    "branch_name": branch["name"],
                    "error": str(branch_error),
                    "branch_index": branch_index,
                    "queue_text": branch["queue_text"],
                    "state_queue": state_queue,
                }
            )
            continue
        if len(branch["queue_text"]) != solver_context["solver_piece_limit"]:
            branch_error = GomenError(
                f"branch queue 길이 불일치: branch={branch['name']} "
                f"expected={solver_context['solver_piece_limit']} actual={len(branch['queue_text'])}"
            )
            branch_results.append(
                {
                    "ok": False,
                    "branch_name": branch["name"],
                    "error": str(branch_error),
                    "branch_index": branch_index,
                    "queue_text": branch["queue_text"],
                    "state_queue": state_queue,
                }
            )
            branch_errors.append(branch_error)
            continue

        debug_payload = build_gomen_debug_payload(
            board,
            active,
            hold,
            queue,
            state_queue,
            branch["queue_text"],
            garbage,
            branch["name"],
            solver_context,
            branch.get("use_hold", True),
        )
        log_gomen_request(debug_payload)
        cache_key = _build_branch_cache_key(
            garbage=garbage,
            branch_name=branch["name"],
            queue_text=branch["queue_text"],
            placements_needed=solver_context["placements_needed"],
            solver_piece_limit=solver_context["solver_piece_limit"],
            physics=physics,
            use_hold=branch.get("use_hold", True),
            current=active,
            hold=hold_text,
        )
        cached_branch = _RESULT_CACHE.get(cache_key)
        if cached_branch is not None:
            result = copy.deepcopy(cached_branch)
        else:
            try:
                result = _run_gomen_branch(
                    session,
                    branch,
                    garbage,
                    timeout_sec=timeout_sec,
                    physics=physics,
                    limit=limit,
                )
            except GomenError as exc:
                branch_results.append(
                    {
                        "ok": False,
                        "branch_name": branch["name"],
                        "error": str(exc),
                        "branch_index": branch_index,
                        "queue_text": branch["queue_text"],
                        "state_queue": state_queue,
                        "debug_request": debug_payload,
                    }
                )
                branch_errors.append(exc)
                continue
            _RESULT_CACHE[cache_key] = copy.deepcopy(result)

        result["branch_index"] = branch_index
        result["debug_request"] = debug_payload
        result["queue_text"] = branch["queue_text"]
        result["state_queue"] = state_queue
        _log_gomen_branch_response(result)
        branch_results.append(result)
        success_results.append(result)

    if not success_results:
        if shortage_lengths:
            raise GomenError(
                f"PC SOLVER: NEXT 큐 부족\n필요={solver_context['solver_piece_limit']}, 확보={max(shortage_lengths)}"
            )
        if branch_errors:
            raise branch_errors[0]
        raise GomenError("gomen branch 결과가 없습니다.")

    selected = _merge_branch_results(success_results, state_queue)
    selected["branch_results"] = copy.deepcopy(branch_results)
    selected["state_queue"] = state_queue
    selected["garbage"] = str(garbage)
    return selected
