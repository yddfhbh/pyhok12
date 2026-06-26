import ast
import copy
import os
import queue
import re
import subprocess
import sys
import threading
import time


VALID_PIECES = set("IJLOSTZ")
PIECE_PRIORITY = "IJLOSTZ"
_SESSION_LOCK = threading.Lock()
_RESULT_CACHE = {}
_SESSION_CACHE = {}


class HydraError(Exception):
    pass


class HydraSession:
    def __init__(self, decision_mode=False, threads=4):
        self.decision_mode = bool(decision_mode)
        self.threads = max(1, int(threads))
        self.proc = None
        self.stdout_queue = queue.Queue()
        self.reader_thread = None
        self.current_field_hash = None
        self.current_see = None
        self.lock = threading.Lock()

    def ensure_started(self, field_hash, see, timeout_sec=30):
        if self.proc is not None and self.proc.poll() is None:
            return

        hydra_exe = get_hydra_exe_path()
        cmd = [
            hydra_exe,
            "-f", str(field_hash),
            "-s", str(see),
            "-m", str(self.threads),
        ]
        if self.decision_mode:
            cmd.append("-d")

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            cmd,
            cwd=get_hydra_dir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        self.stdout_queue = queue.Queue()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        startup_output = self._read_until_prompt(timeout_sec=max(30, timeout_sec))
        if "Invalid hash" in startup_output:
            self.close()
            raise HydraError("Hydra 시작 실패\n\n" + startup_output[-1500:])

        self.current_field_hash = field_hash
        self.current_see = see

    def _reader_loop(self):
        try:
            while self.proc is not None and self.proc.stdout is not None:
                chunk = self.proc.stdout.read(1)
                if not chunk:
                    break
                self.stdout_queue.put(chunk)
        finally:
            self.stdout_queue.put(None)

    def _read_until_prompt(self, timeout_sec=20):
        deadline = time.monotonic() + timeout_sec
        chunks = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HydraError("Hydra 응답 시간 초과")

            try:
                chunk = self.stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise HydraError("Hydra 응답 시간 초과") from exc

            if chunk is None:
                break

            chunks.append(chunk)
            text = "".join(chunks)
            if text.endswith("> "):
                return text[:-2]

        text = "".join(chunks)
        if text:
            return text

        if self.proc is not None and self.proc.poll() is not None:
            raise HydraError("Hydra 프로세스가 예기치 않게 종료됨")

        raise HydraError("Hydra 출력 수신 실패")

    def _send_command(self, command, timeout_sec=20):
        if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
            raise HydraError("Hydra 세션이 종료됨")

        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        return self._read_until_prompt(timeout_sec=timeout_sec)

    def run_query(self, field_hash, see, bag_arg, timeout_sec=20):
        with self.lock:
            self.ensure_started(field_hash=field_hash, see=len(see), timeout_sec=timeout_sec)

            if self.current_field_hash != field_hash:
                output = self._send_command(f"-f {field_hash}", timeout_sec=timeout_sec)
                if "Invalid hash" in output:
                    raise HydraError("Hydra 필드 변경 실패\n\n" + output[-1500:])
                self.current_field_hash = field_hash

            if self.current_see != len(see):
                output = self._send_command(f"-s {len(see)}", timeout_sec=timeout_sec)
                if "Invalid see" in output:
                    raise HydraError("Hydra see 변경 실패\n\n" + output[-1500:])
                self.current_see = len(see)

            return self._send_command(f"{see} {bag_arg}", timeout_sec=timeout_sec)

    def close(self):
        proc = self.proc
        self.proc = None

        if proc is None:
            return

        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write("X\n")
                proc.stdin.flush()
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
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_hydra_dir():
    return os.path.join(get_app_base_dir(), "tools", "hydra")


def get_hydra_exe_path():
    return os.path.join(get_hydra_dir(), "hydra_solver.exe")


def get_hydra_session(decision_mode=False, threads=4):
    key = (bool(decision_mode), max(1, int(threads)))
    with _SESSION_LOCK:
        session = _SESSION_CACHE.get(key)
        if session is None:
            session = HydraSession(decision_mode=decision_mode, threads=threads)
            _SESSION_CACHE[key] = session
        return session


def close_hydra_sessions():
    with _SESSION_LOCK:
        sessions = list(_SESSION_CACHE.values())
        _SESSION_CACHE.clear()

    for session in sessions:
        session.close()


def clear_hydra_cache():
    _RESULT_CACHE.clear()


def warm_hydra_session(decision_mode=False, threads=4, timeout_sec=60):
    warm_board = [["." for _ in range(10)] for _ in range(20)]
    warm_hash = board_to_hydra_hash(warm_board)
    warm_see = "IJLOSTZ"
    session = get_hydra_session(decision_mode=decision_mode, threads=threads)
    session.ensure_started(
        field_hash=warm_hash,
        see=len(warm_see),
        timeout_sec=timeout_sec,
    )


def warm_hydra_sessions(threads=4, timeout_sec=60):
    for decision_mode in (True, False):
        warm_hydra_session(
            decision_mode=decision_mode,
            threads=threads,
            timeout_sec=timeout_sec,
        )


def normalize_bottom_4_rows(board):
    if not board or len(board) < 4:
        raise HydraError("보드 데이터가 부족함")

    bottom_rows = board[-4:]
    binary_rows = []
    for row in bottom_rows:
        bits = ""
        for cell in row:
            bits += "0" if cell == "." else "1"
        binary_rows.append(bits)

    full_rows = [line for line in binary_rows if line == "1111111111"]
    not_full_rows = [line for line in binary_rows if line != "1111111111"]
    return not_full_rows + full_rows


def board_to_hydra_hash(board):
    rows = normalize_bottom_4_rows(board)
    value = 0
    for line in rows:
        for ch in line:
            value <<= 1
            if ch == "1":
                value += 1
    return value


def board_to_hydra_field_text(board):
    rows = normalize_bottom_4_rows(board)
    return "\n".join(
        "".join("X" if ch == "1" else "." for ch in line)
        for line in rows
    )


def clean_piece_text(text):
    text = (text or "").upper().strip()
    return "".join(ch for ch in text if ch in VALID_PIECES)


def make_hydra_see_string(active, hold, queue, manual_see=""):
    manual_see = clean_piece_text(manual_see)
    if manual_see:
        if len(manual_see) < 2:
            raise HydraError("Hydra SEE 문자열은 최소 2글자 이상이어야 함")
        return manual_see

    active = clean_piece_text(active)
    hold = clean_piece_text(hold)

    queue_text = ""
    for piece in queue:
        if piece in VALID_PIECES:
            queue_text += piece

    if not active:
        raise HydraError("ACTIVE 미노를 입력해야 함")

    if hold:
        see = hold[0] + active[0] + queue_text
    else:
        see = active[0] + queue_text

    if len(see) < 2:
        raise HydraError("Hydra SEE 문자열이 너무 짧음")

    if len(see) > 11:
        see = see[:11]

    return see


def parse_hydra_result(output):
    result_match = re.search(r"Result:\s*([0-9.]+)(?:/([0-9.]+))?", output)
    time_match = re.search(r"Time:\s*([0-9]+)\s*ms", output)

    if not result_match:
        raise HydraError("Hydra 결과를 파싱하지 못함\n\n" + output[-1000:])

    success = result_match.group(1)
    total = result_match.group(2)
    time_ms = time_match.group(1) if time_match else "?"

    percent = None
    if total:
        try:
            percent = float(success) / float(total) * 100
        except ZeroDivisionError:
            percent = None

    return {
        "success": success,
        "total": total,
        "percent": percent,
        "time_ms": time_ms,
        "raw": output,
    }


def run_hydra(board, active, hold, queue, manual_see="", bag_arg="7", threads=4, timeout_sec=20):
    return _run_hydra(
        board=board,
        active=active,
        hold=hold,
        queue=queue,
        manual_see=manual_see,
        bag_arg=bag_arg,
        threads=threads,
        timeout_sec=timeout_sec,
        decision_mode=False,
    )


def _run_hydra(board, active, hold, queue, manual_see="", bag_arg="7", threads=4, timeout_sec=20, decision_mode=False):
    hydra_dir = get_hydra_dir()
    hydra_exe = get_hydra_exe_path()
    graph_path = os.path.join(hydra_dir, "graph.bin")

    if not os.path.exists(hydra_exe):
        raise HydraError(f"hydra_solver.exe 없음: {hydra_exe}")

    if not os.path.exists(graph_path):
        raise HydraError(f"graph.bin 없음: {graph_path}")

    field_hash = board_to_hydra_hash(board)
    field_text = board_to_hydra_field_text(board)
    see = make_hydra_see_string(active, hold, queue, manual_see=manual_see)
    bag_arg = (bag_arg or "7").strip().upper()
    thread_count = max(1, int(threads))

    cache_key = (
        bool(decision_mode),
        field_hash,
        see,
        bag_arg,
        thread_count,
    )
    cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    session = get_hydra_session(decision_mode=decision_mode, threads=thread_count)
    output = session.run_query(
        field_hash=field_hash,
        see=see,
        bag_arg=bag_arg,
        timeout_sec=timeout_sec,
    )

    parsed = parse_hydra_result(output)
    parsed["field_hash"] = field_hash
    parsed["field_text"] = field_text
    parsed["see"] = see
    parsed["bag_arg"] = bag_arg
    if decision_mode:
        parsed["solution"] = load_hydra_solution(hydra_dir)

    _RESULT_CACHE[cache_key] = copy.deepcopy(parsed)
    return parsed


def load_hydra_solution(hydra_dir):
    tree_path = os.path.join(hydra_dir, "tree_data.js")
    if not os.path.exists(tree_path):
        return None

    with open(tree_path, "r", encoding="utf-8") as handle:
        text = handle.read()

    init_hash_match = re.search(r"init_hash=(\d+)", text)
    match = re.search(r"data=(.+)$", text, re.MULTILINE | re.DOTALL)
    if not match:
        return None

    data = ast.literal_eval(match.group(1).replace("null", "None"))
    init_hash = int(init_hash_match.group(1)) if init_hash_match else None
    return extract_hydra_solution(data, init_hash=init_hash)


def hydra_hash_to_rows(board_hash):
    bits = format(int(board_hash), "040b")
    rows = []
    for offset in range(0, 40, 10):
        row_bits = bits[offset:offset + 10]
        rows.append("".join("X" if bit == "1" else "." for bit in row_bits))
    return rows

def rows_to_hydra_hash(rows):
    value = 0
    for line in rows:
        for ch in line:
            value <<= 1
            if ch == "X":
                value += 1
    return value


def normalize_hash_rows(rows):
    normalized = []
    for row in rows[:4]:
        row_text = row if isinstance(row, str) else "".join(row)
        normalized.append((row_text + "." * 10)[:10])

    while len(normalized) < 4:
        normalized.insert(0, "." * 10)

    full_rows = [line for line in normalized if line == "X" * 10]
    not_full_rows = [line for line in normalized if line != "X" * 10]
    return not_full_rows + full_rows


def clear_full_rows_4(rows):
    kept = [line for line in rows if line != "X" * 10]
    while len(kept) < 4:
        kept.insert(0, "." * 10)
    return kept[-4:]


PIECE_ORIENTATIONS = {
    "I": [
        [(0, 0), (0, 1), (0, 2), (0, 3)],
        [(0, 0), (1, 0), (2, 0), (3, 0)],
    ],
    "O": [
        [(0, 0), (0, 1), (1, 0), (1, 1)],
    ],
    "T": [
        [(0, 0), (0, 1), (0, 2), (1, 1)],
        [(0, 1), (1, 0), (1, 1), (2, 1)],
        [(0, 1), (1, 0), (1, 1), (1, 2)],
        [(0, 0), (1, 0), (1, 1), (2, 0)],
    ],
    "J": [
        [(0, 0), (1, 0), (1, 1), (1, 2)],
        [(0, 0), (0, 1), (1, 0), (2, 0)],
        [(0, 0), (0, 1), (0, 2), (1, 2)],
        [(0, 1), (1, 1), (2, 0), (2, 1)],
    ],
    "L": [
        [(0, 2), (1, 0), (1, 1), (1, 2)],
        [(0, 0), (1, 0), (2, 0), (2, 1)],
        [(0, 0), (0, 1), (0, 2), (1, 0)],
        [(0, 0), (0, 1), (1, 1), (2, 1)],
    ],
    "S": [
        [(0, 1), (0, 2), (1, 0), (1, 1)],
        [(0, 0), (1, 0), (1, 1), (2, 1)],
    ],
    "Z": [
        [(0, 0), (0, 1), (1, 1), (1, 2)],
        [(0, 1), (1, 0), (1, 1), (2, 0)],
    ],
}


def infer_piece_placement_rows(prev_hash, next_hash, piece):
    if prev_hash is None or next_hash is None or piece not in PIECE_ORIENTATIONS:
        return None

    prev_rows = hydra_hash_to_rows(prev_hash)
    target_hash = int(next_hash)
    base = [list(row) for row in prev_rows]

    best_visible = None
    best_visible_count = -1

    for shape in PIECE_ORIENTATIONS[piece]:
        max_r = max(r for r, _ in shape)
        max_c = max(c for _, c in shape)

        # 중요:
        # top을 음수까지 허용한다.
        # 실제 미노가 4줄 PC 영역 위로 걸쳐도,
        # 아래 4줄에 보이는 칸만 hash에 반영될 수 있음.
        for top in range(-max_r, 4):
            for left in range(0, 10 - max_c):
                absolute_cells = [(top + r, left + c) for r, c in shape]
                visible_cells = [
                    (r, c)
                    for r, c in absolute_cells
                    if 0 <= r < 4 and 0 <= c < 10
                ]

                if not visible_cells:
                    continue

                if any(base[r][c] == "X" for r, c in visible_cells):
                    continue

                placed_board = [row[:] for row in base]
                placed_only = [["." for _ in range(10)] for _ in range(4)]

                for r, c in visible_cells:
                    placed_board[r][c] = "X"
                    placed_only[r][c] = "X"

                placed_rows = ["".join(row) for row in placed_board]

                candidates = [
                    placed_rows,
                    normalize_hash_rows(placed_rows),
                    clear_full_rows_4(placed_rows),
                ]

                if any(rows_to_hydra_hash(candidate) == target_hash for candidate in candidates):
                    visible_count = len(visible_cells)

                    # 4칸 다 보이는 배치를 우선,
                    # 아니면 가장 많이 보이는 배치를 fallback으로 사용
                    if visible_count > best_visible_count:
                        best_visible_count = visible_count
                        best_visible = ["".join(row) for row in placed_only]

                    if visible_count == 4:
                        return best_visible

    return best_visible


def make_hydra_step(piece, prev_hash, next_hash):
    placed_rows = infer_piece_placement_rows(prev_hash, next_hash, piece)

    return {
        "piece": piece,
        "hash": next_hash,
        "rows": hydra_hash_to_rows(next_hash),
        "prev_hash": prev_hash,
        "prev_rows": hydra_hash_to_rows(prev_hash) if prev_hash is not None else None,
        "placed_rows": placed_rows,
    }


def extract_hydra_solution(data, init_hash=None):
    if not data:
        return None

    if is_hydra_solve_path(data):
        return extract_solve_path_solution(data, init_hash=init_hash)

    best_solution = extract_best_branch_solution(data, init_hash=init_hash)
    if best_solution:
        return best_solution

    pieces = extract_best_branch_pieces(data)
    if not pieces:
        return None

    return {
        "mode": "best_branch",
        "pieces": pieces,
        "steps": [],
        "text": " -> ".join(pieces),
        "init_hash": init_hash,
        "init_rows": hydra_hash_to_rows(init_hash) if init_hash is not None else None,
    }


def extract_solve_path_solution(data, init_hash=None):
    pieces = []
    steps = []
    previous_hash = init_hash

    for item in data[1:]:
        if not isinstance(item, list) or len(item) < 2:
            continue

        board_hash = item[0]
        piece_index = item[1]

        if not isinstance(piece_index, int) or not (0 <= piece_index < len(PIECE_PRIORITY)):
            continue

        piece = PIECE_PRIORITY[piece_index]
        pieces.append(piece)
        steps.append(make_hydra_step(piece, previous_hash, board_hash))
        previous_hash = board_hash

    return {
        "mode": "solve_path",
        "pieces": pieces,
        "steps": steps,
        "text": " -> ".join(pieces) if pieces else "(no moves)",
        "init_hash": init_hash,
        "init_rows": hydra_hash_to_rows(init_hash) if init_hash is not None else None,
    }


def extract_best_branch_solution(node, init_hash=None):
    pieces = []
    steps = []

    previous_hash = init_hash
    current = node

    for _ in range(20):
        if is_hydra_solve_path(current):
            tail = extract_solve_path_solution(current, init_hash=previous_hash)
            if tail:
                pieces.extend(tail.get("pieces") or [])
                steps.extend(tail.get("steps") or [])
            break

        if not isinstance(current, list) or len(current) < 4:
            break

        piece_index = current[1]
        children = current[3]

        if not isinstance(piece_index, int) or not (0 <= piece_index < len(PIECE_PRIORITY)):
            break

        if not isinstance(children, list) or not children:
            break

        best_child = None
        best_child_score = float("-inf")

        for child in children:
            score = hydra_node_score(child)
            if score > best_child_score:
                best_child_score = score
                best_child = child

        if best_child is None:
            break

        piece = PIECE_PRIORITY[piece_index]

        # best_child가 solve path면 보드 hash로 취급하지 말고 바로 tail 처리
        if is_hydra_solve_path(best_child):
            pieces.append(piece)

            tail = extract_solve_path_solution(best_child, init_hash=previous_hash)
            if tail:
                pieces.extend(tail.get("pieces") or [])
                steps.extend(tail.get("steps") or [])
            break

        next_hash = None
        if isinstance(best_child, list) and best_child:
            candidate_hash = best_child[0]
            if isinstance(candidate_hash, int):
                next_hash = candidate_hash

        pieces.append(piece)

        if next_hash is not None:
            steps.append(make_hydra_step(piece, previous_hash, next_hash))
            previous_hash = next_hash

        current = best_child

    if not pieces:
        return None

    return {
        "mode": "best_branch",
        "pieces": pieces,
        "steps": steps,
        "text": " -> ".join(pieces),
        "init_hash": init_hash,
        "init_rows": hydra_hash_to_rows(init_hash) if init_hash is not None else None,
    }

def is_hydra_solve_path(node):
    return (
        isinstance(node, list)
        and len(node) >= 2
        and isinstance(node[0], list)
        and len(node[0]) == 1
    )

def extract_best_branch_pieces(node):
    if is_hydra_solve_path(node):
        result = extract_hydra_solution(node)
        return result["pieces"] if result else []

    if not isinstance(node, list) or len(node) < 4:
        return []

    piece_index = node[1]
    children = node[3]
    current_piece = ""
    if isinstance(piece_index, int) and 0 <= piece_index < len(PIECE_PRIORITY):
        current_piece = PIECE_PRIORITY[piece_index]

    best_child_pieces = []
    best_child_score = float("-inf")
    for child in children:
        score = hydra_node_score(child)
        if score > best_child_score:
            best_child_score = score
            best_child_pieces = extract_best_branch_pieces(child)

    return ([current_piece] if current_piece else []) + best_child_pieces


def hydra_node_score(node):
    if node is None:
        return float("-inf")

    if is_hydra_solve_path(node):
        if isinstance(node[0], list) and node[0]:
            return float(node[0][0])
        return 0.0

    if isinstance(node, list) and len(node) >= 3 and isinstance(node[2], (int, float)):
        return float(node[2])

    return float("-inf")


def run_hydra_with_solution(board, active, hold, queue, manual_see="", bag_arg="7", threads=4, timeout_sec=20):
    return _run_hydra(
        board=board,
        active=active,
        hold=hold,
        queue=queue,
        manual_see=manual_see,
        bag_arg=bag_arg,
        threads=threads,
        timeout_sec=timeout_sec,
        decision_mode=True,
    )


def _hydra_result_score(result):
    percent = result.get("percent")
    if percent is not None:
        return float(percent)

    try:
        return float(result.get("success", 0))
    except (TypeError, ValueError):
        return -1.0


def _hydra_time_score(result):
    time_ms = result.get("time_ms")
    if str(time_ms).isdigit():
        return -int(time_ms)
    return -(10**9)


def run_hydra_auto_active(board, hold, queue, manual_see="", bag_arg="7", threads=4, timeout_sec=20):
    manual_see = clean_piece_text(manual_see)
    if manual_see:
        result = run_hydra(
            board=board,
            active="",
            hold=hold,
            queue=queue,
            manual_see=manual_see,
            bag_arg=bag_arg,
            threads=threads,
            timeout_sec=timeout_sec,
        )
        result["active"] = ""
        result["active_mode"] = "manual_see"
        result["candidates"] = []
        return result

    successes = []
    last_error = None

    for active in PIECE_PRIORITY:
        try:
            result = run_hydra(
                board=board,
                active=active,
                hold=hold,
                queue=queue,
                manual_see="",
                bag_arg=bag_arg,
                threads=threads,
                timeout_sec=timeout_sec,
            )
            result["active"] = active
            result["active_mode"] = "auto_active"
            successes.append(result)
        except HydraError as exc:
            last_error = exc

    if not successes:
        if last_error:
            raise last_error
        raise HydraError("자동 ACTIVE 추정에 실패함")

    successes.sort(
        key=lambda item: (_hydra_result_score(item), _hydra_time_score(item)),
        reverse=True,
    )

    best = successes[0]
    best["candidates"] = [
        {
            "active": item["active"],
            "success": item["success"],
            "total": item["total"],
            "percent": item["percent"],
            "time_ms": item["time_ms"],
        }
        for item in successes[:3]
    ]
    return best
