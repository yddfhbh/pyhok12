import threading
import tkinter as tk
from tkinter import messagebox

try:
    from py_fumen_py import decode as decode_fumen
except Exception:
    decode_fumen = None

try:
    from tools.setup_finder.setup_finder import find_setups
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

        y = 8
        summary_lines = []
        for index, variant in enumerate(variants[:3], start=1):
            title = variant.get("title") or f"{index}. 해법"
            solution = variant.get("solution") or {}
            pieces = solution.get("pieces") or []
            steps = solution.get("steps") or []

            self.output.create_text(
                10,
                y + 10,
                text=title,
                anchor="w",
                fill="#252525",
                font=("Consolas", 10, "bold"),
            )
            y += 20

            if steps:
                y = self.draw_solution_step_row(steps, y)
            else:
                y = self.draw_solution_piece_row(pieces, y)

            summary_lines.append(f"{title}: {' -> '.join(pieces)}")
            y += 16

        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, max(y, self.solve_canvas_height)))
        self.output_hint_var.set("\n".join(summary_lines))

    def draw_solution_step_row(self, steps, start_y):
        card_width = 84
        card_height = 62
        gap_x = 10
        start_x = 8

        for index, step in enumerate(steps[:3]):
            x = start_x + index * (card_width + gap_x)
            self.draw_solution_step_card(x, start_y, card_width, card_height, index + 1, step)

        return start_y + card_height

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

    def render_setup_groups(self, variants):
        self.output.delete("all")
        self.current_output_mode = "setup"

        card_width = 132
        card_height = 164
        gap_x = 12
        gap_y = 14
        start_x = 8
        start_y = 8

        for index, variant in enumerate(variants[:2]):
            col = index % 2
            row = index // 2
            x = start_x + col * (card_width + gap_x)
            y = start_y + row * (card_height + gap_y)
            self.draw_setup_card(x, y, card_width, card_height, variant)

        bottom = start_y + ((len(variants[:2]) + 1) // 2) * (card_height + gap_y)
        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, max(bottom, self.solve_canvas_height)))
        self.output_hint_var.set(
            "\n".join(
                f"{variant['title']}: {variant['setup']['id']} / {variant['queue_text'][:3]}"
                for variant in variants[:2]
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
        if find_setups is None:
            return []

        board = result.get("board") or []
        active = result.get("active_guess") or ""
        hold = result.get("hold") or ""
        queue = [piece for piece in result.get("queue", []) if piece]
        pieces_count = result.get("pieces_count")
        round_from_counter = result.get("pc_round")

        if not self.has_single_top_active_piece(board, active):
            return []

        candidates = []
        candidates.append(("1. ACTIVE 시작", active + "".join(queue)))
        if hold and hold != active:
            candidates.append(("2. HOLD 시작", hold + "".join(queue)))

        variants = []
        seen_ids = set()
        for title, queue_text in candidates:
            try:
                found = find_setups(queue_text)
            except Exception:
                continue

            seventh = found.get("seventh") or {}
            if not seventh.get("ok"):
                continue

            setup = seventh.get("result") or {}
            setup_id = setup.get("id")
            if not setup_id or setup_id in seen_ids:
                continue

            round_num = round_from_counter or seventh.get("pc")
            if round_num and pieces_count is not None:
                round_text = f"{round_num}회차 | {pieces_count}p"
            elif round_num:
                round_text = f"{round_num}회차"
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

    def has_single_top_active_piece(self, board, active):
        if not board or not active or active not in VISIBLE_FIELD_PIECES:
            return False

        cells = []
        top_rows = min(4, len(board))
        for row_index in range(top_rows):
            for col_index, piece in enumerate(board[row_index]):
                if piece in VISIBLE_FIELD_PIECES:
                    cells.append((row_index, col_index, piece))

        if not cells or len(cells) > 4:
            return False

        if any(piece != active for _, _, piece in cells):
            return False

        remaining = {(r, c) for r, c, _ in cells}
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
            monitor_index = self.config.get("monitor_index", 1)
            img = capture_screen(monitor_index)
            result = recognize_all(img, self.config)
            self._post_to_ui(self._on_scan_success, img, result)
        except Exception as exc:
            self._post_to_ui(self._on_scan_error, str(exc), show_popup)
        finally:
            self._post_to_ui(self._on_scan_finished)

    def _on_scan_success(self, img, result):
        self.last_screenshot = img
        self.last_result = result

        scan_signature = self._make_scan_signature(result)
        same_scan = scan_signature == self.last_scan_signature
        self.last_scan_signature = scan_signature

        if not same_scan:
            self.draw_board(result["board"])
            setup_variants = self.build_setup_variants(result)
            round_text = ""
            if result.get("pc_round") is not None:
                round_text = f" ({result['pc_round']}회차"
                if result.get("pieces_count") is not None:
                    round_text += f" / {result['pieces_count']}p"
                round_text += ")"
            if setup_variants:
                self.render_setup_groups(setup_variants)
                self.status_label.config(text=f"스캔 완료 - 추천 셋업 표시{round_text}")
            else:
                self.print_message("스캔 완료.\nHydra 계산을 누르면 해법 카드가 표시됩니다.")
                self.status_label.config(text=f"스캔 완료{round_text}")
        else:
            self.status_label.config(text="스캔 완료 (변화 없음)")

        if self.hydra_auto_var.get() and not same_scan:
            self.run_hydra_now(show_popup=False)

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
                    timeout_sec=25,
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
                    timeout_sec=25,
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
                    timeout_sec=25,
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
                        timeout_sec=25,
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
                timeout_sec=25,
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
