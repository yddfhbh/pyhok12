import threading
import tkinter as tk
from tkinter import messagebox

try:
    from py_fumen_py import decode as decode_fumen
except Exception:
    decode_fumen = None

try:
    from tools.setup_finder.setup_finder import find_setup_for_pc
except Exception:
    find_setups = None

from hydra_helper import (
    HydraError,
    close_hydra_sessions,
    run_hydra_auto_active,
    run_hydra_with_solution,
    warm_hydra_session,
)
from recognizer import VISIBLE_FIELD_PIECES, load_config, recognize_all
from scanner import capture_screen, save_debug_screenshot
PC_BAG_TABLE = [
        [1, 1, 1, 1, 1, 1, 1, 2, 2, 2],       # 1회차: 7+3
        [2, 2, 2, 2, 3, 3, 3, 3, 3, 3],       # 2회차: 4+6
        [3, 4, 4, 4, 4, 4, 4, 4, 5, 5],       # 3회차: 1+7+2
        [5, 5, 5, 5, 5, 6, 6, 6, 6, 6],       # 4회차: 5+5
        [6, 6, 7, 7, 7, 7, 7, 7, 7, 8],       # 5회차: 2+7+1
        [8, 8, 8, 8, 8, 8, 9, 9, 9, 9],       # 6회차: 6+4
        [9, 9, 9, 10, 10, 10, 10, 10, 10, 10], # 7회차: 3+7
]
PC_STRUCTURES = {
        1: "7+3",
        2: "4+6",
        3: "1+7+2",
        4: "5+5",
        5: "2+7+1",
        6: "6+4",
        7: "3+7",
}    
    

class TetrisScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TETRIS SCAN")
        self.root.geometry("620x980")
        self.root.resizable(False, False)

        self.config = load_config("config.json")
        self.last_screenshot = None
        self.last_result = None
        self.last_scan_signature = None
        self.current_output_mode = "placeholder"

        self.auto_scan_interval_ms = 500
        self.auto_scan_enabled = False
        self.auto_scan_job = None
        self.is_scanning = False
        self.is_hydra_running = False
        self.is_closing = False
        self.is_hydra_warming = False
        self.hydra_warm_ready = False

        self.active_var = tk.StringVar(value="")
        self.manual_see_var = tk.StringVar(value="")
        self.bag_var = tk.StringVar(value="7")
        self.hydra_auto_var = tk.BooleanVar(value=False)

        self.always_on_top = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cell_size = 21
        self.board_cols = 10
        self.board_rows = 20
        self.solve_card_cell_size = 7
        self.solve_canvas_width = 292
        self.solve_canvas_height = 420

        self.title_label = tk.Label(
            root,
            text="TETRIS FIELD SCANNER",
            font=("Malgun Gothic", 16, "bold"),
        )
        self.title_label.pack(pady=(12, 4))

        self.button_frame = tk.Frame(root)
        self.button_frame.pack(pady=8)

        self.scan_button = tk.Button(
            self.button_frame,
            text="스캔",
            width=12,
            height=2,
            font=("Malgun Gothic", 12, "bold"),
            command=self.scan,
        )
        self.scan_button.grid(row=0, column=0, padx=5)

        self.debug_button = tk.Button(
            self.button_frame,
            text="캡처 저장",
            width=12,
            height=2,
            font=("Malgun Gothic", 12),
            command=self.save_debug,
        )
        self.debug_button.grid(row=0, column=1, padx=5)

        self.reload_button = tk.Button(
            self.button_frame,
            text="설정 다시읽기",
            width=12,
            height=2,
            font=("Malgun Gothic", 12),
            command=self.reload_config,
        )
        self.reload_button.grid(row=0, column=2, padx=5)

        self.topmost_check = tk.Checkbutton(
            root,
            text="창 항상 위에 고정",
            variable=self.always_on_top,
            command=self.toggle_topmost,
            font=("Malgun Gothic", 10),
        )
        self.topmost_check.pack(pady=(0, 4))
        self.pc_round_label = tk.Label(
            root,
            text="현재 PC: -",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        )
        self.pc_round_label.pack(fill="x", padx=16, pady=(0, 4))
        self.hydra_frame = tk.LabelFrame(
            root,
            text="HYDRA PC SOLVER",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.hydra_frame.pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(self.hydra_frame, text="ACTIVE", font=("Malgun Gothic", 9)).grid(
            row=0, column=0, padx=4, pady=4
        )
        self.active_entry = tk.Entry(
            self.hydra_frame,
            textvariable=self.active_var,
            width=5,
            font=("Consolas", 11),
        )
        self.active_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(self.hydra_frame, text="SEE 직접입력", font=("Malgun Gothic", 9)).grid(
            row=0, column=2, padx=4, pady=4
        )
        self.manual_see_entry = tk.Entry(
            self.hydra_frame,
            textvariable=self.manual_see_var,
            width=14,
            font=("Consolas", 11),
        )
        self.manual_see_entry.grid(row=0, column=3, padx=4, pady=4)

        tk.Label(self.hydra_frame, text="BAG", font=("Malgun Gothic", 9)).grid(
            row=0, column=4, padx=4, pady=4
        )
        self.bag_entry = tk.Entry(
            self.hydra_frame,
            textvariable=self.bag_var,
            width=5,
            font=("Consolas", 11),
        )
        self.bag_entry.grid(row=0, column=5, padx=4, pady=4)

        self.hydra_button = tk.Button(
            self.hydra_frame,
            text="Hydra 계산",
            width=11,
            font=("Malgun Gothic", 9, "bold"),
            command=self.run_hydra_now,
        )
        self.hydra_button.grid(row=0, column=6, padx=6, pady=4)

        self.hydra_auto_check = tk.Checkbutton(
            self.hydra_frame,
            text="스캔 후 자동",
            variable=self.hydra_auto_var,
            font=("Malgun Gothic", 9),
        )
        self.hydra_auto_check.grid(row=0, column=7, padx=4, pady=4)

        self.hydra_result_label = tk.Label(
            self.hydra_frame,
            text="HYDRA: 대기 중",
            anchor="w",
            font=("Malgun Gothic", 10),
        )
        self.hydra_result_label.grid(
            row=1,
            column=0,
            columnspan=8,
            sticky="we",
            padx=6,
            pady=(0, 5),
        )

        self.main_frame = tk.Frame(root)
        self.main_frame.pack(padx=12, pady=8)

        self.board_frame = tk.Frame(self.main_frame)
        self.board_frame.grid(row=0, column=0, padx=(0, 14), sticky="n")

        self.board_label = tk.Label(
            self.board_frame,
            text="COLOR BOARD",
            font=("Malgun Gothic", 11, "bold"),
        )
        self.board_label.pack(pady=(0, 6))

        self.board_canvas = tk.Canvas(
            self.board_frame,
            width=self.board_cols * self.cell_size,
            height=self.board_rows * self.cell_size,
            bg="#050505",
            highlightthickness=2,
            highlightbackground="#444444",
        )
        self.board_canvas.pack()

        self.output_frame = tk.Frame(self.main_frame)
        self.output_frame.grid(row=0, column=1, sticky="n")

        self.output_label = tk.Label(
            self.output_frame,
            text="PC SOLVE",
            font=("Malgun Gothic", 11, "bold"),
        )
        self.output_label.pack(pady=(0, 6))

        self.output = tk.Canvas(
            self.output_frame,
            width=self.solve_canvas_width,
            height=self.solve_canvas_height,
            bg="#fbfaf6",
            highlightthickness=1,
            highlightbackground="#d8d3c7",
        )
        self.output.pack()

        self.output_hint_var = tk.StringVar(value="")
        self.output_hint = tk.Label(
            self.output_frame,
            textvariable=self.output_hint_var,
            justify="left",
            anchor="nw",
            wraplength=self.solve_canvas_width - 4,
            font=("Malgun Gothic", 10),
        )
        self.output_hint.pack(fill="x", pady=(8, 0))

        self.status_label = tk.Label(
            root,
            text="수동 스캔 대기 중",
            font=("Malgun Gothic", 10),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        self.draw_empty_board()
        self.print_message("기본은 수동 스캔입니다.\n스캔 후 추천 셋업 또는 Hydra 해법이 표시됩니다.")
        self.root.after(150, self.start_hydra_warmup)
        self.last_hydra_signature = None

    def print_message(self, text):
        self.clear_output_view(text)

    def clear_output_view(self, text=""):
        self.output.delete("all")
        self.current_output_mode = "placeholder"
        self.output_hint_var.set((text or "").strip())
        self.output.create_text(
            self.solve_canvas_width / 2,
            self.solve_canvas_height / 2,
            text="추천 셋업 또는\nHydra 카드가\n여기에 표시됩니다.",
            fill="#8f8a80",
            font=("Consolas", 15, "bold"),
            justify="center",
        )

    def schedule_auto_scan(self, delay_ms=None):
        if not self.auto_scan_enabled:
            return

        if self.auto_scan_job is not None:
            self.root.after_cancel(self.auto_scan_job)

        wait_ms = self.auto_scan_interval_ms if delay_ms is None else delay_ms
        self.auto_scan_job = self.root.after(wait_ms, self.auto_scan_tick)

    def auto_scan_tick(self):
        self.auto_scan_job = None
        self.scan(show_popup=False)

    def on_close(self):
        self.is_closing = True
        self.auto_scan_enabled = False
        if self.auto_scan_job is not None:
            self.root.after_cancel(self.auto_scan_job)
            self.auto_scan_job = None
        close_hydra_sessions()
        self.root.destroy()

    def reload_config(self):
        try:
            self.config = load_config("config.json")
            self.status_label.config(text="config.json 다시 읽음")
        except Exception as exc:
            messagebox.showerror("설정 오류", str(exc))

    def start_hydra_warmup(self):
        if self.is_closing or self.is_hydra_warming or self.hydra_warm_ready:
            return

        self.is_hydra_warming = True
        self.hydra_result_label.config(text="HYDRA: 예열 중...")

        worker = threading.Thread(target=self._hydra_warmup_worker, daemon=True)
        worker.start()

    def scan(self, show_popup=True):
        if self.is_scanning or self.is_closing:
            return

        self.is_scanning = True
        self.status_label.config(text="스캔 중...")
        self.scan_button.config(state="disabled")

        worker = threading.Thread(
            target=self._scan_worker,
            args=(show_popup,),
            daemon=True,
        )
        worker.start()

    def save_debug(self):
        if self.last_screenshot is None:
            try:
                monitor_index = self.config.get("monitor_index", 1)
                self.last_screenshot = capture_screen(monitor_index)
            except Exception as exc:
                messagebox.showerror("캡처 오류", str(exc))
                return

        save_debug_screenshot(self.last_screenshot, "debug_screenshot.png")
        self.status_label.config(text="debug_screenshot.png 저장됨")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.always_on_top.get())
        if self.always_on_top.get():
            self.status_label.config(text="창 항상 위 고정 ON")
        else:
            self.status_label.config(text="창 항상 위 고정 OFF")

    def get_pc_round_info(self, result):
        pieces_count = result.get("pieces_count")

        if pieces_count is not None:
            try:
                pieces = int(float(pieces_count))
                if pieces < 0:
                    pieces = 0

                cycle_pieces = pieces % 70
                pc_round = cycle_pieces // 10 + 1
                pc_progress = cycle_pieces % 10
                bag_in_cycle = PC_BAG_TABLE[pc_round - 1][pc_progress]
                structure = PC_STRUCTURES.get(pc_round, "-")

                return {
                    "pieces_count": pieces,
                    "cycle_pieces": cycle_pieces,
                    "pc_round": pc_round,
                    "pc_progress": pc_progress,
                    "bag_in_cycle": bag_in_cycle,
                    "structure": structure,
                    "source": "pieces",
                }
            except (TypeError, ValueError):
                pass

        fallback_round = result.get("pc_round")
        if fallback_round is not None:
            try:
                round_num = int(float(fallback_round))
                if round_num <= 0:
                    round_num = 1

                round_num = ((round_num - 1) % 7) + 1
                structure = PC_STRUCTURES.get(round_num, "-")

                return {
                    "pieces_count": None,
                    "cycle_pieces": None,
                    "pc_round": round_num,
                    "pc_progress": None,
                    "bag_in_cycle": None,
                    "structure": structure,
                    "source": "pc_round",
                }
            except (TypeError, ValueError):
                pass

        # pieces OCR이 실패했더라도, 보드에 현재 active 미노만 잡힌 상태면
        # 게임 시작 직후 0p = 1회차로 본다.
        board = result.get("board") or []
        active = result.get("active_guess") or ""

        try:
            if active and self.has_single_top_active_piece(board, active) and self.count_visible_cells(board) <= 4:
                pieces = 0
                cycle_pieces = 0
                pc_round = 1
                pc_progress = 0
                bag_in_cycle = PC_BAG_TABLE[0][0]
                structure = PC_STRUCTURES.get(1, "-")

                return {
                    "pieces_count": pieces,
                    "cycle_pieces": cycle_pieces,
                    "pc_round": pc_round,
                    "pc_progress": pc_progress,
                    "bag_in_cycle": bag_in_cycle,
                    "structure": structure,
                    "source": "active_only_fallback",
                }
        except Exception:
            pass

        return {
            "pieces_count": None,
            "cycle_pieces": None,
            "pc_round": None,
            "pc_progress": None,
            "bag_in_cycle": None,
            "structure": "-",
            "source": "unknown",
        }


    def format_pc_round_info(self, result):
        info = self.get_pc_round_info(result)

        if info["pc_round"] is None:
            return "현재 PC: 인식 실패"

        if info["source"] == "active_only_fallback":
            return (
                f"현재 PC: {info['pc_round']}회차 | "
                f"{info['pc_progress']}/10p | "
                f"총 {info['pieces_count']}p | "
                f"가방 {info['bag_in_cycle']} | "
                f"구조 {info['structure']} | 추정"
            )

        if info["pieces_count"] is None:
            return (
                f"현재 PC: {info['pc_round']}회차 | "
                f"pieces 인식 실패 | "
                f"구조 {info['structure']}"
            )

        return (
            f"현재 PC: {info['pc_round']}회차 | "
            f"{info['pc_progress']}/10p | "
            f"총 {info['pieces_count']}p | "
            f"가방 {info['bag_in_cycle']} | "
            f"구조 {info['structure']}"
        )

    def run_hydra_now(self, show_popup=True):
        if self.is_hydra_running or self.is_closing:
            return

        if not self.last_result:
            if show_popup:
                messagebox.showwarning("Hydra", "먼저 스캔을 실행해야 합니다.")
            return

        board = self.last_result["board"]
        active_guess = self.last_result.get("active_guess")
        hold = self.last_result["hold"]
        queue = self.last_result["queue"]

        active = (self.active_var.get() or "").strip().upper()
        manual_see = (self.manual_see_var.get() or "").strip().upper()
        bag_arg = self.bag_var.get()

        if not active and not manual_see and not active_guess and not show_popup:
            self.hydra_result_label.config(text="HYDRA: ACTIVE 자동 인식 대기 중")
            self.status_label.config(text="Hydra ACTIVE 대기 중")
            self.print_message("ACTIVE 인식이 잡히면 Hydra 계산이 시작됩니다.")
            return

        hydra_signature = (
            tuple("".join(row) for row in board),
            active_guess,
            hold,
            tuple(queue),
            active,
            manual_see,
            bag_arg,
        )

        if not show_popup and hydra_signature == self.last_hydra_signature:
            self.status_label.config(text="Hydra 계산 생략 - 같은 상태")
            return

        self.last_hydra_signature = hydra_signature

        self.is_hydra_running = True
        self.hydra_button.config(state="disabled")
        if show_popup or self.current_output_mode != "setup":
            self.clear_output_view("Hydra 해법 계산 중...")
        else:
            self.output_hint_var.set("추천 셋업 표시 중\nHydra 해법 계산 중...")
        self.status_label.config(text="Hydra 계산 중...")

        worker = threading.Thread(
            target=self._run_hydra_worker,
            args=(board, active_guess, hold, queue, active, manual_see, bag_arg, show_popup),
            daemon=True,
        )
        worker.start()

    def render_solution_groups(self, variants):
        self.output.delete("all")
        self.current_output_mode = "hydra"

        card_width = 260
        card_height = 120
        gap_y = 12
        start_x = 8
        start_y = 8

        summary_lines = []

        visible = variants[:2]  # ACTIVE / HOLD 정도만 보여주기

        for index, variant in enumerate(visible):
            y = start_y + index * (card_height + gap_y)
            self.draw_solution_compact_card(
                start_x,
                y,
                card_width,
                card_height,
                variant,
            )

            solution = variant.get("solution") or {}
            pieces = solution.get("pieces") or []
            title = variant.get("title") or f"{index + 1}. 해법"
            summary_lines.append(f"{title}: {' -> '.join(pieces)}")

        bottom = start_y + len(visible) * (card_height + gap_y)
        self.output.config(
            scrollregion=(0, 0, self.solve_canvas_width, max(bottom, self.solve_canvas_height))
        )
        self.output_hint_var.set("\n".join(summary_lines))

    def draw_solution_step_row(self, steps, start_y):
        card_width = 84
        card_height = 62
        gap_x = 10
        gap_y = 8
        start_x = 8
        cards_per_row = 3

        visible_steps = steps[:9]  # 너무 길어도 최대 9개까지만 표시

        for index, step in enumerate(visible_steps):
            col = index % cards_per_row
            row = index // cards_per_row

            x = start_x + col * (card_width + gap_x)
            y = start_y + row * (card_height + gap_y)

            self.draw_solution_step_card(
                x,
                y,
                card_width,
                card_height,
                index + 1,
                step,
            )

        row_count = (len(visible_steps) + cards_per_row - 1) // cards_per_row
        return start_y + row_count * card_height + max(0, row_count - 1) * gap_y

    def draw_solution_piece_row(self, pieces, start_y):
        card_width = 84
        card_height = 62
        gap_x = 10
        start_x = 8

        for index, piece in enumerate(pieces[:3]):
            x = start_x + index * (card_width + gap_x)
            self.output.create_rectangle(
                x,
                start_y,
                x + card_width,
                start_y + card_height,
                fill="#f4f1e8",
                outline="#dfd9ca",
            )
            self.output.create_text(
                x + card_width / 2,
                start_y + 14,
                text=f"{index + 1}",
                fill="#5f5a52",
                font=("Consolas", 10, "bold"),
            )
            self.output.create_rectangle(
                x + 18,
                start_y + 24,
                x + card_width - 18,
                start_y + card_height - 12,
                fill=self.get_piece_color(piece),
                outline="",
            )
            self.output.create_text(
                x + card_width / 2,
                start_y + 39,
                text=piece,
                fill="#ffffff",
                font=("Consolas", 18, "bold"),
            )

        return start_y + card_height

    def draw_solution_step_card(self, x, y, width, height, step_no, step):
        self.output.create_rectangle(
            x,
            y,
            x + width,
            y + height,
            fill="#f4f1e8",
            outline="#dfd9ca",
        )

        piece = step.get("piece", "?")
        self.output.create_text(
            x + 10,
            y + 10,
            text=f"{step_no}. {piece}",
            anchor="w",
            fill="#252525",
            font=("Consolas", 10, "bold"),
        )

        current_rows = step.get("rows") or []
        previous_rows = step.get("prev_rows") or ["." * 10 for _ in range(4)]
        new_cells = set()

        for row_index in range(min(len(current_rows), 4)):
            current_row = current_rows[row_index]
            previous_row = previous_rows[row_index] if row_index < len(previous_rows) else "." * 10
            for col_index in range(min(len(current_row), 10)):
                if current_row[col_index] == "X" and previous_row[col_index] != "X":
                    new_cells.add((row_index, col_index))

        if not new_cells:
            for row_index in range(min(len(current_rows), 4)):
                current_row = current_rows[row_index]
                for col_index in range(min(len(current_row), 10)):
                    if current_row[col_index] == "X":
                        new_cells.add((row_index, col_index))

        cell = self.solve_card_cell_size
        board_x = x + 7
        board_y = y + 20

        for row_index in range(4):
            current_row = current_rows[row_index] if row_index < len(current_rows) else "." * 10
            for col_index in range(10):
                occupied = col_index < len(current_row) and current_row[col_index] == "X"
                if occupied:
                    fill = self.get_piece_color(piece) if (row_index, col_index) in new_cells else "#6f6b67"
                else:
                    fill = "#f8f6ef"

                x1 = board_x + col_index * cell
                y1 = board_y + row_index * cell
                x2 = x1 + cell
                y2 = y1 + cell
                self.output.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=fill,
                    outline="#ece7da",
                )

    def clear_preview_full_rows(self, board):
        kept = []

        for row in board:
            # 색 상관없이 10칸 다 차 있으면 full row로 보고 제거
            if all(cell != "." for cell in row):
                continue
            kept.append(row[:])

        while len(kept) < 4:
            kept.insert(0, list("." * 10))

        return kept[-4:]

    def build_compact_solution_rows(self, solution):
        if not solution:
            return []

        steps = solution.get("steps") or []
        init_rows = solution.get("init_rows") or []

        if not init_rows and steps:
            init_rows = steps[0].get("prev_rows") or []

        if not init_rows:
            init_rows = ["." * 10 for _ in range(4)]

        board = []
        for row in init_rows[:4]:
            row_text = row if isinstance(row, str) else "".join(row)
            row_text = (row_text + "." * 10)[:10]
            board.append(list(row_text))

        while len(board) < 4:
            board.insert(0, list("." * 10))

        # 초기 스택은 회색으로 표시
        for r in range(4):
            for c in range(10):
                if board[r][c] != ".":
                    board[r][c] = "X"

        for step in steps:
            piece = step.get("piece", "X")
            placed_rows = step.get("placed_rows")

            # placed_rows를 우선 신뢰
            if placed_rows:
                for r in range(min(4, len(placed_rows))):
                    row = placed_rows[r]
                    for c in range(min(10, len(row))):
                        if row[c] == "X":
                            board[r][c] = piece

                # 한 수 둘 때마다 줄 삭제 반영
                board = self.clear_preview_full_rows(board)
                continue

            # placed_rows를 못 구한 경우엔 아예 색칠을 생략하는 편이 더 안전함
            # (기존 fallback은 line clear가 있으면 색이 자주 틀어짐)
            continue

        return ["".join(row) for row in board]

    def draw_solution_compact_card(self, x, y, width, height, variant):
        solution = variant.get("solution") or {}
        title = variant.get("title") or "해법"
        pieces = solution.get("pieces") or []

        preview_rows = self.build_compact_solution_rows(solution)

        self.output.create_rectangle(
            x,
            y,
            x + width,
            y + height,
            fill="#f4f1e8",
            outline="#dfd9ca",
        )

        self.output.create_text(
            x + 10,
            y + 12,
            text=title,
            anchor="w",
            fill="#252525",
            font=("Consolas", 10, "bold"),
        )

        # 통합 보드 미리보기
        if preview_rows and any(any(ch not in ".X" for ch in row) for row in preview_rows):
            self.draw_preview_board(
                preview_rows,
                x + 10,
                y + 24,
                cell_size=12,
            )
        else:
            self.output.create_text(
                x + 10,
                y + 42,
                text="배치 미리보기 불안정\n순서만 표시",
                anchor="w",
                fill="#8f6f4a",
                font=("Malgun Gothic", 9, "bold"),
            )

        # 해법 순서 텍스트
        piece_text = " -> ".join(pieces) if pieces else "-"
        self.output.create_text(
            x + 10,
            y + 80,
            text=piece_text,
            anchor="w",
            fill="#252525",
            font=("Consolas", 9, "bold"),
        )

        # 작은 색상 strip
        self.draw_piece_strip(
            "".join(pieces[:10]),
            x + 10,
            y + 92,
            block_size=10,
            gap=2,
        )

    def render_setup_groups(self, variants):
        self.output.delete("all")
        self.current_output_mode = "setup"

        card_width = 132
        card_height = 164
        gap_x = 12
        gap_y = 14
        start_x = 8
        start_y = 8

        for index, variant in enumerate(variants[:4]):
            col = index % 2
            row = index // 2
            x = start_x + col * (card_width + gap_x)
            y = start_y + row * (card_height + gap_y)
            self.draw_setup_card(x, y, card_width, card_height, variant)

        bottom = start_y + ((len(variants[:4]) + 1) // 2) * (card_height + gap_y)
        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, max(bottom, self.solve_canvas_height)))
        self.output_hint_var.set(
            "\n".join(
                f"{variant['title']}: {variant['setup']['id']} / {variant['queue_text'][:3]}"
                for variant in variants[:4]
            )
        )

    def draw_setup_card(self, x, y, width, height, variant):
        setup = variant["setup"]
        queue_text = variant["queue_text"]
        round_text = variant.get("round_text") or "-"
        setup_id = setup.get("id", "-")
        preview_rows = variant.get("preview_rows") or []

        self.output.create_rectangle(
            x,
            y,
            x + width,
            y + height,
            fill="#f4f1e8",
            outline="#dfd9ca",
        )
        self.output.create_text(
            x + 10,
            y + 12,
            text=variant["title"],
            anchor="w",
            fill="#252525",
            font=("Consolas", 10, "bold"),
        )
        self.output.create_text(
            x + 10,
            y + 32,
            text=round_text,
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 9, "bold"),
        )

        if preview_rows:
            self.draw_preview_board(
                preview_rows,
                x + 10,
                y + 44,
                cell_size=10,
            )
        else:
            self.draw_piece_strip(setup_id, x + 10, y + 46, block_size=11, gap=2)

        self.output.create_text(
            x + 10,
            y + 98,
            text=setup_id,
            anchor="w",
            fill="#111111",
            font=("Consolas", 17, "bold"),
        )
        self.draw_piece_strip(queue_text[:3], x + 10, y + 110, block_size=10, gap=2)
        self.output.create_text(
            x + 10,
            y + 128,
            text=f"queue {queue_text[:3]}",
            anchor="w",
            fill="#3a3a3a",
            font=("Consolas", 9),
        )
        self.output.create_text(
            x + 10,
            y + 146,
            text=f"SOL {setup.get('sol', '-')}",
            anchor="w",
            fill="#3a3a3a",
            font=("Consolas", 9),
        )

    def build_setup_variants(self, result):
        if find_setup_for_pc is None:
            return []

        board = result.get("board") or []
        active = result.get("active_guess") or ""
        hold = result.get("hold") or ""
        queue = [piece for piece in result.get("queue", []) if piece]
        round_info = self.get_pc_round_info(result)
        pieces_count = round_info["pieces_count"]
        round_from_counter = round_info["pc_round"]
        pc_progress = round_info["pc_progress"]
        bag_in_cycle = round_info["bag_in_cycle"]
        

        if not active:
            return []

        if round_from_counter not in (1, 7):
            return []

        candidates = []
        candidates.append(("1. ACTIVE 시작", active + "".join(queue)))
        if hold and hold != active:
            candidates.append(("2. HOLD 시작", hold + "".join(queue)))

        variants = []
        seen_ids = set()
        for title, queue_text in candidates:
            try:
                found = find_setup_for_pc(queue_text, round_from_counter)
            except Exception:
                continue

            if not found.get("ok"):
                continue

            setup = found.get("result") or {}
            setup_id = setup.get("id")
            if not setup_id or setup_id in seen_ids:
                continue

            if round_from_counter is not None and pieces_count is not None:
                round_text = (
                    f"{round_from_counter}회차 | "
                    f"{pc_progress}/10p | "
                    f"B{bag_in_cycle}"
                )
            elif round_from_counter is not None:
                round_text = f"{round_from_counter}회차"
            elif pieces_count is not None:
                round_text = f"{pieces_count}p"
            else:
                round_text = "-"
            variants.append(
                {
                    "title": title,
                    "queue_text": found.get("queue") or queue_text,
                    "setup": setup,
                    "round_text": round_text,
                    "preview_rows": self.decode_setup_preview(setup.get("fumen", "")),
                }
            )
            seen_ids.add(setup_id)

        return variants

    def count_visible_cells(self, board):
        count = 0
        for row in board or []:
            for cell in row:
                if cell in VISIBLE_FIELD_PIECES:
                    count += 1
        return count


    def should_show_setup_recommendation(self, result):
        board = result.get("board") or []
        active = result.get("active_guess") or ""

        # 상단 active 미노 하나가 제대로 잡힌 상황이 아니면 셋업 추천 X
        if not self.has_single_top_active_piece(board, active):
            return False

        # 현재 낙하 미노만 있으면 보통 4칸.
        # 5칸 이상이면 이미 바닥에 놓인 블럭이 있다고 판단.
        return self.count_visible_cells(board) <= 4    

    def has_single_top_active_piece(self, board, active):
        if not board or not active or active not in VISIBLE_FIELD_PIECES:
            return False

        rows = len(board)
        cols = len(board[0]) if rows else 0

        # 아래 5칸은 제외하고, 그 위에 있는 active 덩어리를 현재 조작 미노로 봄
        active_search_bottom = max(0, rows - 5)

        cells = []
        for row_index in range(active_search_bottom):
            for col_index, piece in enumerate(board[row_index]):
                if piece in VISIBLE_FIELD_PIECES:
                    cells.append((row_index, col_index, piece))

        if not cells:
            return False

        # active 후보 영역에 여러 종류가 섞이면 셋업 추천은 하지 않음
        active_cells = [(r, c) for r, c, piece in cells if piece == active]
        other_cells = [(r, c, piece) for r, c, piece in cells if piece != active]

        if other_cells:
            return False

        if len(active_cells) > 4:
            return False

        remaining = set(active_cells)
        stack = [next(iter(remaining))]
        seen = set()

        while stack:
            cell = stack.pop()
            if cell in seen:
                continue

            seen.add(cell)
            r, c = cell

            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (r + dr, c + dc)
                if nxt in remaining and nxt not in seen:
                    stack.append(nxt)

        return len(seen) == len(remaining)

    def rgb_to_hex(self, rgb):
        if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
            return "#050505"

        r, g, b = (max(0, min(255, int(value))) for value in rgb[:3])
        return f"#{r:02x}{g:02x}{b:02x}"

    def get_piece_color(self, piece):
        colors = self.config.get("colors", {})

        if piece == ".":
            return "#050505"
        if piece == "X":
            rgb = colors.get("G", [134, 134, 134])
            return self.rgb_to_hex(rgb)
        if piece in colors:
            return self.rgb_to_hex(colors[piece])
        return "#6f6b67"

    def draw_piece_strip(self, pieces, x, y, block_size=10, gap=2):
        for index, piece in enumerate(str(pieces or "")):
            fill = self.get_piece_color(piece)
            x1 = x + index * (block_size + gap)
            y1 = y
            x2 = x1 + block_size
            y2 = y1 + block_size
            self.output.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=fill,
                outline="#d7d0c3",
            )
            self.output.create_text(
                x1 + block_size / 2,
                y1 + block_size / 2 + 0.5,
                text=piece,
                fill="#ffffff",
                font=("Consolas", max(7, block_size - 4), "bold"),
            )

    def draw_preview_board(self, rows, x, y, cell_size=10):
        if not rows:
            return

        for row_index, row in enumerate(rows):
            for col_index, piece in enumerate(row[:10]):
                x1 = x + col_index * cell_size
                y1 = y + row_index * cell_size
                x2 = x1 + cell_size
                y2 = y1 + cell_size

                if piece == ".":
                    fill = "#efe8dc"
                    outline = "#ddd5c7"
                elif piece == "X":
                    fill = "#6b6964"
                    outline = "#7b7872"
                else:
                    fill = self.get_piece_color(piece)
                    outline = "#ffffff"

                self.output.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=fill,
                    outline=outline,
                )

    def decode_setup_preview(self, fumen_text):
        if decode_fumen is None or not fumen_text:
            return []

        try:
            pages = decode_fumen(fumen_text)
        except Exception:
            return []

        if not pages:
            return []

        field = pages[0].field
        rows = []
        for y in range(3, -1, -1):
            row = []
            for x in range(10):
                try:
                    mino = field.at(x, y)
                    name = getattr(mino, "name", "") or str(mino)
                except Exception:
                    name = ""

                if name in ("EMPTY", "_", ""):
                    row.append(".")
                elif name in ("GRAY", "G", "X"):
                    row.append("X")
                else:
                    row.append(name[:1])
            rows.append(row)

        return rows

    def draw_empty_board(self):
        empty_board = [["." for _ in range(self.board_cols)] for _ in range(self.board_rows)]
        self.draw_board(empty_board)

    def draw_board(self, board):
        self.board_canvas.delete("all")

        for row_index in range(self.board_rows):
            for col_index in range(self.board_cols):
                try:
                    piece = board[row_index][col_index]
                except IndexError:
                    piece = "."

                x1 = col_index * self.cell_size
                y1 = row_index * self.cell_size
                x2 = x1 + self.cell_size
                y2 = y1 + self.cell_size
                fill = self.get_piece_color(piece)

                self.board_canvas.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=fill,
                    outline="#202020",
                )

                if piece != ".":
                    self.board_canvas.create_rectangle(
                        x1 + 3,
                        y1 + 3,
                        x2 - 3,
                        y2 - 3,
                        outline="#ffffff",
                        width=1,
                    )

    def _post_to_ui(self, callback, *args):
        if self.is_closing:
            return

        def runner():
            if not self.is_closing:
                callback(*args)

        try:
            self.root.after(0, runner)
        except RuntimeError:
            pass

    def _scan_worker(self, show_popup):
        try:
            import time

            print("[SCAN] start")
            monitor_index = self.config.get("monitor_index", 1)

            t0 = time.time()
            print("[SCAN] capture start")
            img = capture_screen(monitor_index)
            print(f"[SCAN] capture done {time.time() - t0:.3f}s")

            t1 = time.time()
            print("[SCAN] recognize start")
            result = recognize_all(img, self.config)
            print(f"[SCAN] recognize done {time.time() - t1:.3f}s")
            print(
                "[SCAN RESULT]",
                "pieces_count=", result.get("pieces_count"),
                "pc_round=", result.get("pc_round"),
                "active=", result.get("active_guess"),
                "hold=", result.get("hold"),
                "queue=", result.get("queue"),
            )

            self._post_to_ui(self._on_scan_success, img, result)

        except Exception as exc:
            print("[SCAN ERROR]", repr(exc))
            self._post_to_ui(self._on_scan_error, str(exc), show_popup)

        finally:
            print("[SCAN] finished")
            self._post_to_ui(self._on_scan_finished)

    def _on_scan_success(self, img, result):
        self.last_screenshot = img
        self.last_result = result
        self.pc_round_label.config(text=self.format_pc_round_info(result))

        print(
            "[SCAN RESULT]",
            "pieces_count=", result.get("pieces_count"),
            "pc_round=", result.get("pc_round"),
            "active=", result.get("active_guess"),
            "hold=", result.get("hold"),
            "queue=", result.get("queue"),
        )
        scan_signature = self._make_scan_signature(result)
        same_scan = scan_signature == self.last_scan_signature
        self.last_scan_signature = scan_signature

        setup_variants = []

        if not same_scan:
            self.draw_board(result["board"])
            setup_variants = self.build_setup_variants(result)

            round_info = self.get_pc_round_info(result)
            round_text = ""

            if round_info["pc_round"] is not None:
                round_text = (
                    f" ({round_info['pc_round']}회차"
                    f" / {round_info['pc_progress']}/10p"
                    f" / 총 {round_info['pieces_count']}p"
                    f" / 가방 {round_info['bag_in_cycle']}"
                    f")"
                )

            if setup_variants:
                self.render_setup_groups(setup_variants)
                self.status_label.config(text=f"스캔 완료 - 추천 셋업 표시{round_text}")
                return

            self.print_message("스캔 완료.\nHydra 계산을 누르면 해법 카드가 표시됩니다.")
            self.status_label.config(text=f"스캔 완료{round_text}")

            if self.hydra_auto_var.get():
                if result.get("active_guess"):
                    self.status_label.config(text=f"스캔 완료 - Hydra 계산 시작{round_text}")
                    self.run_hydra_now(show_popup=False)
                else:
                    self.status_label.config(text="Hydra ACTIVE 대기 중")
                    self.output_hint_var.set("ACTIVE 인식이 잡히면 Hydra 계산이 시작됩니다.")
        else:
            self.status_label.config(text="스캔 완료 (변화 없음)")

    def _on_scan_error(self, error_text, show_popup):
        self.status_label.config(text="스캔 실패")
        if show_popup:
            messagebox.showerror("스캔 오류", error_text)

    def _on_scan_finished(self):
        self.is_scanning = False
        if not self.is_closing:
            self.scan_button.config(state="normal")
            if self.auto_scan_enabled:
                self.schedule_auto_scan()

    def _run_hydra_worker(self, board, active_guess, hold, queue, active, manual_see, bag_arg, show_popup):
        try:
            if active or manual_see:
                primary_result = run_hydra_with_solution(
                    board=board,
                    active=active,
                    hold=hold,
                    queue=queue,
                    manual_see=manual_see,
                    bag_arg=bag_arg,
                    threads=4,
                    timeout_sec=60,
                )
                primary_result["active"] = active
                primary_result["active_mode"] = "manual"
                primary_result["candidates"] = []
                primary_result["solution_variants"] = self._build_solution_variants(
                    board=board,
                    active=active,
                    hold=hold,
                    queue=queue,
                    bag_arg=bag_arg,
                    manual_see=manual_see,
                    primary_result=primary_result,
                )
                result = primary_result
            elif active_guess:
                primary_result = run_hydra_with_solution(
                    board=board,
                    active=active_guess,
                    hold=hold,
                    queue=queue,
                    manual_see="",
                    bag_arg=bag_arg,
                    threads=4,
                    timeout_sec=60,
                )
                primary_result["active"] = active_guess
                primary_result["active_mode"] = "board_guess"
                primary_result["candidates"] = []
                primary_result["solution_variants"] = self._build_solution_variants(
                    board=board,
                    active=active_guess,
                    hold=hold,
                    queue=queue,
                    bag_arg=bag_arg,
                    manual_see="",
                    primary_result=primary_result,
                )
                result = primary_result
            else:
                result = run_hydra_auto_active(
                    board=board,
                    hold=hold,
                    queue=queue,
                    manual_see="",
                    bag_arg=bag_arg,
                    threads=4,
                    timeout_sec=60,
                )
                if result.get("active"):
                    primary_result = run_hydra_with_solution(
                        board=board,
                        active=result["active"],
                        hold=hold,
                        queue=queue,
                        manual_see="",
                        bag_arg=bag_arg,
                        threads=4,
                        timeout_sec=60,
                    )
                    result["solution"] = primary_result.get("solution")
                    result["solution_variants"] = self._build_solution_variants(
                        board=board,
                        active=result["active"],
                        hold=hold,
                        queue=queue,
                        bag_arg=bag_arg,
                        manual_see="",
                        primary_result=primary_result,
                    )

            self._post_to_ui(self._on_hydra_success, result)
        except HydraError as exc:
            self._post_to_ui(self._on_hydra_error, "hydra", str(exc), show_popup)
        except Exception as exc:
            self._post_to_ui(self._on_hydra_error, "generic", str(exc), show_popup)
        finally:
            self._post_to_ui(self._on_hydra_finished)

    def _build_solution_variants(self, board, active, hold, queue, bag_arg, manual_see, primary_result):
        variants = []
        seen = set()

        primary_solution = primary_result.get("solution")
        primary_key = self._solution_key(primary_solution)
        if primary_solution and primary_key:
            variants.append(
                {
                    "title": f"1. ACTIVE {active}",
                    "solution": primary_solution,
                }
            )
            seen.add(primary_key)

        if manual_see or not hold or not active or hold == active:
            return variants

        try:
            alt_result = run_hydra_with_solution(
                board=board,
                active=hold,
                hold=active,
                queue=queue,
                manual_see="",
                bag_arg=bag_arg,
                threads=4,
                timeout_sec=60,
            )
            alt_solution = alt_result.get("solution")
            alt_key = self._solution_key(alt_solution)
            if alt_solution and alt_key and alt_key not in seen:
                variants.append(
                    {
                        "title": f"{len(variants) + 1}. HOLD {hold}",
                        "solution": alt_solution,
                    }
                )
                seen.add(alt_key)
        except HydraError:
            pass

        return variants

    def _solution_key(self, solution):
        if not solution:
            return None
        pieces = tuple(solution.get("pieces") or [])
        steps = tuple(step.get("hash") for step in solution.get("steps") or [])
        return pieces, steps

    def _on_hydra_success(self, result):
        if result["total"]:
            percent_text = f"{result['percent']:.2f}%" if result["percent"] is not None else "?"
            label = (
                f"HYDRA: {result['success']}/{result['total']} "
                f"({percent_text}) | {result['time_ms']}ms | "
                f"hash={result['field_hash']} | see={result['see']} bag={result['bag_arg']}"
            )
        else:
            label = (
                f"HYDRA: {result['success']} | {result['time_ms']}ms | "
                f"hash={result['field_hash']} | see={result['see']} bag={result['bag_arg']}"
            )

        if result.get("active_mode") == "auto_active":
            top_candidates = ", ".join(
                candidate["active"]
                for candidate in result.get("candidates", [])
            )
            if top_candidates:
                label += f" | active={result['active']} [{top_candidates}]"
            else:
                label += f" | active={result['active']}"
        elif result.get("active_mode") == "board_guess":
            label += f" | active~{result['active']}"
        elif result.get("active"):
            label += f" | active={result['active']}"

        self.hydra_result_label.config(text=label)
        self.status_label.config(text="Hydra 계산 완료")

        variants = result.get("solution_variants") or []
        if variants:
            self.render_solution_groups(variants)
            return

        solution = result.get("solution")
        if solution and solution.get("pieces"):
            self.render_solution_groups([{"title": "1. SOLVE", "solution": solution}])
        else:
            self.print_message("Hydra 결과는 나왔지만 카드형 해법 데이터는 찾지 못했습니다.")

    def _on_hydra_error(self, error_kind, error_text, show_popup):
        headline = error_text.splitlines()[0] if error_text else "알 수 없는 오류"
        self.hydra_result_label.config(text=f"HYDRA 오류: {headline}")
        self.status_label.config(text="Hydra 계산 실패")
        if error_kind == "hydra":
            self.print_message("Hydra 계산에 실패했습니다.")
        else:
            self.print_message("Hydra 계산 중 예외가 발생했습니다.")

        if show_popup:
            messagebox.showerror("Hydra 오류", error_text)

    def _on_hydra_finished(self):
        self.is_hydra_running = False
        if not self.is_closing:
            self.hydra_button.config(state="normal")

    def _make_scan_signature(self, result):
        board = tuple("".join(row) for row in result.get("board", []))
        queue = tuple(result.get("queue", []))
        return (
            board,
            result.get("active_guess"),
            result.get("hold"),
            queue,
            result.get("pieces_count"),
            result.get("pc_round"),
        )

    def _hydra_warmup_worker(self):
        try:
            warm_hydra_session(decision_mode=True, threads=4, timeout_sec=60)
            self._post_to_ui(self._on_hydra_warmup_success)
        except Exception:
            self._post_to_ui(self._on_hydra_warmup_finished)

    def _on_hydra_warmup_success(self):
        self.hydra_warm_ready = True
        if not self.is_hydra_running:
            self.hydra_result_label.config(text="HYDRA: 대기 중")
        self._on_hydra_warmup_finished()
        worker = threading.Thread(target=self._secondary_hydra_warmup_worker, daemon=True)
        worker.start()

    def _on_hydra_warmup_finished(self):
        self.is_hydra_warming = False

    def _secondary_hydra_warmup_worker(self):
        try:
            warm_hydra_session(decision_mode=False, threads=4, timeout_sec=60)
        except Exception:
            pass


if __name__ == "__main__":
    root = tk.Tk()
    app = TetrisScannerApp(root)
    root.mainloop()
