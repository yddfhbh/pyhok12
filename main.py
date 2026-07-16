import ctypes
import threading
from itertools import combinations
import tkinter as tk
from tkinter import messagebox

try:
    from py_fumen_py import decode as decode_fumen
except Exception:
    decode_fumen = None

try:
    from tools.setup_finder.setup_finder import find_setup_candidates_for_pc, find_setup_for_pc
except Exception:
    find_setup_candidates_for_pc = None
    find_setup_for_pc = None

from hydra_helper import (
    HydraError,
    close_hydra_sessions,
    make_hydra_see_string,
    run_hydra_auto_active,
    run_hydra_with_solution,
    warm_hydra_session,
)
from gomen_helper import (
    GomenError,
    close_gomen_sessions,
    make_state_queue,
    run_gomen_solver,
    warm_gomen_session,
)
from app_paths import ensure_runtime_file
from tetrio_state_source import (
    TETROMINO_BASE_COORDS,
    TetrioStateSource,
    VISIBLE_FIELD_PIECES,
    load_config,
)

VK_DELETE = 0x2E
VK_END = 0x23


user32 = ctypes.windll.user32
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
DEFAULT_PIECE_COLORS = {
        "I": [66, 175, 225],
        "J": [17, 101, 181],
        "L": [243, 137, 39],
        "O": [246, 208, 60],
        "S": [81, 184, 77],
        "T": [151, 57, 162],
        "Z": [235, 79, 101],
        "G": [134, 134, 134],
}


class TetrisScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PYHOK12 TETR.IO CDP")
        self.root.geometry("620x1060")
        self.root.resizable(False, False)

        self.config = load_config("config.json")
        self.state_source = TetrioStateSource("config.json")
        self.last_result = None
        self.last_scan_signature = None
        self.current_output_mode = "placeholder"
        self.setup_option_refresh_job = None
        self.state_status_job = None

        self.auto_scan_interval_ms = 500
        self.auto_scan_enabled = False
        self.auto_scan_job = None
        self.hotkey_poll_job = None
        self.is_scanning = False
        self.is_hydra_running = False
        self.is_pc_solving = False
        self.is_closing = False
        self.is_hydra_warming = False
        self.hydra_warm_ready = False
        self.is_pc_solver_warming = False
        self.pc_solver_warm_ready = False
        self.enable_hold_solution_variant = False
        self.hotkey_pressed = {
            VK_DELETE: False,
            VK_END: False,
        }

        self.active_var = tk.StringVar(value="")
        self.manual_see_var = tk.StringVar(value="")
        self.bag_var = tk.StringVar(value="7")
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
            text="TETR.IO CDP STATE READER",
            font=("Malgun Gothic", 16, "bold"),
        )
        self.title_label.pack(pady=(12, 4))

        self.button_frame = tk.Frame(root)
        self.button_frame.pack(pady=8)

        self.pc_scan_button = tk.Button(
            self.button_frame,
            text="상태 읽기 ->\nPC 해법 찾기\n[Del]",
            width=16,
            height=3,
            font=("Malgun Gothic", 11, "bold"),
            command=self.scan_for_pc_solve,
        )
        self.pc_scan_button.grid(row=0, column=0, padx=5)

        self.setup_scan_button = tk.Button(
            self.button_frame,
            text="상태 읽기 ->\n최적 셋업 찾기\n[End]",
            width=16,
            height=3,
            font=("Malgun Gothic", 11, "bold"),
            command=self.scan_for_setup_finder,
        )
        self.setup_scan_button.grid(row=0, column=1, padx=5)

        self.reload_button = tk.Button(
            self.button_frame,
            text="설정 다시읽기",
            width=10,
            height=2,
            font=("Malgun Gothic", 12),
            command=self.reload_config,
        )
        self.reload_button.grid(row=0, column=2, padx=5)

        self.connect_button = tk.Button(
            self.button_frame,
            text="브라우저 연결",
            width=10,
            height=2,
            font=("Malgun Gothic", 12),
            command=self.connect_browser_source,
        )
        self.connect_button.grid(row=0, column=3, padx=5)

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
        self.browser_status_var = tk.StringVar(value="Browser: Connecting")
        self.game_state_var = tk.StringVar(value="Game state: Waiting")
        self.mode_var = tk.StringVar(value="Mode: Unknown")
        self.identity_var = tk.StringVar(value="Game ID: -")
        self.counter_var = tk.StringVar(value="Piece counter: -")
        self.board_size_var = tk.StringVar(value="Board size: -")
        self.queue_status_var = tk.StringVar(value="Current/Hold/Queue: -")
        self.age_var = tk.StringVar(value="Last update age: -")
        self.detail_var = tk.StringVar(value="Detail: Waiting for game state")
        self.build_state_status_panel()
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
            text="Hydra 카드 계산",
            width=11,
            font=("Malgun Gothic", 9, "bold"),
            command=self.run_hydra_now,
        )
        self.hydra_button.grid(row=0, column=6, padx=6, pady=4)

        self.hydra_result_label = tk.Label(
            self.hydra_frame,
            text="HYDRA: 대기 중",
            anchor="w",
            font=("Malgun Gothic", 10),
        )
        self.hydra_result_label.grid(
            row=1,
            column=0,
            columnspan=7,
            sticky="we",
            padx=6,
            pady=(0, 5),
        )

        self.setup_option_frame = tk.LabelFrame(
            root,
            text="SETUP OPTIONS",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.setup_option_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.build_setup_option_controls()

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
            text="SCAN RESULT",
            font=("Malgun Gothic", 11, "bold"),
        )
        self.output_label.pack(pady=(0, 6))

        self.output_canvas_frame = tk.Frame(self.output_frame)
        self.output_canvas_frame.pack()

        self.output = tk.Canvas(
            self.output_canvas_frame,
            width=self.solve_canvas_width,
            height=self.solve_canvas_height,
            bg="#fbfaf6",
            highlightthickness=1,
            highlightbackground="#d8d3c7",
        )
        self.output.pack(side="left")

        self.output_scrollbar = tk.Scrollbar(
            self.output_canvas_frame,
            orient="vertical",
            command=self.output.yview,
        )
        self.output_scrollbar.pack(side="left", fill="y")
        self.output.config(yscrollcommand=self.output_scrollbar.set)

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
        self.bind_output_scroll_events()

        self.status_label = tk.Label(
            root,
            text="브라우저 게임 상태 대기 중",
            font=("Malgun Gothic", 10),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        self.draw_empty_board()
        self.print_message(
            "Del: PC 해법 상태 읽기 / End: 최적 셋업 상태 읽기\n"
            "왼쪽 버튼을 골라 실행하세요.\n"
            "PC 해법은 상세 카드,\n"
            "최적 셋업은 셋업 카드를 표시합니다."
        )
        self.bind_keyboard_shortcuts()
        self.register_global_hotkeys()
        self.root.after(150, self.start_hydra_warmup)
        self.root.after(250, self.start_pc_solver_warmup)
        self.root.after(200, self.start_browser_source)
        self.last_hydra_signature = None
        self.last_pc_signature = None

    def bind_keyboard_shortcuts(self):
        self.root.bind("<Delete>", self.on_local_delete_hotkey, add="+")
        self.root.bind("<End>", self.on_local_end_hotkey, add="+")

    def on_local_delete_hotkey(self, _event=None):
        self.scan_for_pc_solve(show_popup=False)
        return "break"

    def on_local_end_hotkey(self, _event=None):
        self.scan_for_setup_finder(show_popup=False)
        return "break"

    def register_global_hotkeys(self):
        self.schedule_hotkey_poll()

    def schedule_hotkey_poll(self):
        if self.is_closing:
            return
        if self.hotkey_poll_job is not None:
            self.root.after_cancel(self.hotkey_poll_job)
        self.hotkey_poll_job = self.root.after(25, self.poll_global_hotkeys)

    def poll_global_hotkeys(self):
        self.hotkey_poll_job = None
        if self.is_closing:
            return

        foreground_hwnd = user32.GetForegroundWindow()
        app_hwnd = self.root.winfo_id()
        app_is_foreground = foreground_hwnd == app_hwnd

        for virtual_key in self.hotkey_pressed:
            is_pressed = bool(user32.GetAsyncKeyState(virtual_key) & 0x8000)
            was_pressed = self.hotkey_pressed[virtual_key]

            if is_pressed and not was_pressed and not app_is_foreground:
                self.handle_global_hotkey(virtual_key)

            self.hotkey_pressed[virtual_key] = is_pressed

        self.schedule_hotkey_poll()

    def handle_global_hotkey(self, hotkey_id):
        if hotkey_id == VK_DELETE:
            self.scan_for_pc_solve(show_popup=False)
            return
        if hotkey_id == VK_END:
            self.scan_for_setup_finder(show_popup=False)

    def unregister_global_hotkeys(self):
        for virtual_key in self.hotkey_pressed:
            self.hotkey_pressed[virtual_key] = False

    def bind_output_scroll_events(self):
        for widget in (self.output, self.output_hint):
            widget.bind("<MouseWheel>", self.on_output_mousewheel)
            widget.bind("<Button-4>", self.on_output_mousewheel)
            widget.bind("<Button-5>", self.on_output_mousewheel)

    def on_output_mousewheel(self, event):
        if event.num == 4:
            self.output.yview_scroll(-1, "units")
            return "break"
        if event.num == 5:
            self.output.yview_scroll(1, "units")
            return "break"

        delta = getattr(event, "delta", 0)
        if delta:
            step = -1 if delta > 0 else 1
            self.output.yview_scroll(step, "units")
            return "break"
        return None

    def reset_output_scroll(self):
        self.output.yview_moveto(0)

    def get_default_setup_option_config(self):
        return {
            1: {"mode": "Simple", "priority": "LITZS"},
            2: {"mode": "Advanced", "priority": "TLSOZJI"},
            3: {"mode": "Advanced", "priority": "SSLOIJ"},
            4: {"priority": "TZJOISL"},
            5: {"allow_3p": True, "allow_4p": True, "allow_bd": True},
            6: {"specific_sol": True, "priority": "LTIJOS"},
            7: {"priority": "IJTZLO"},
        }

    def build_setup_option_controls(self):
        defaults = self.get_default_setup_option_config()
        self.setup_option_vars = {}

        for col in range(7):
            self.setup_option_frame.grid_columnconfigure(col, weight=1)

        font_small = ("Consolas", 8)

        for round_num in range(1, 8):
            group = tk.Frame(self.setup_option_frame, bd=1, relief="solid", padx=3, pady=2)
            group.grid(row=0, column=round_num - 1, padx=2, pady=3, sticky="nsew")
            tk.Label(
                group,
                text=f"{round_num}st PC" if round_num == 1 else f"{round_num}nd PC" if round_num == 2 else f"{round_num}rd PC" if round_num == 3 else f"{round_num}th PC",
                font=("Consolas", 8, "bold"),
            ).grid(row=0, column=0, sticky="ew", pady=(0, 2))

            round_vars = {}
            default = defaults.get(round_num, {})

            if round_num in (1, 2, 3):
                mode_var = tk.StringVar(value=default.get("mode", "Simple"))
                mode_menu = tk.OptionMenu(group, mode_var, "Simple", "Advanced")
                mode_menu.config(font=font_small, width=8, padx=1, pady=1, indicatoron=False)
                mode_menu["menu"].config(font=font_small)
                mode_menu.grid(row=1, column=0, sticky="ew")
                round_vars["mode"] = mode_var

                priority_var = tk.StringVar(value=default.get("priority", ""))
                self.create_setup_option_value_label(group, priority_var, row=2)
                round_vars["priority"] = priority_var
            elif round_num == 5:
                allow_3p_var = tk.BooleanVar(value=bool(default.get("allow_3p", True)))
                allow_4p_var = tk.BooleanVar(value=bool(default.get("allow_4p", True)))
                allow_bd_var = tk.BooleanVar(value=bool(default.get("allow_bd", True)))
                tk.Checkbutton(group, text="3P", variable=allow_3p_var, font=font_small, anchor="w").grid(
                    row=1, column=0, sticky="w"
                )
                tk.Checkbutton(group, text="4P", variable=allow_4p_var, font=font_small, anchor="w").grid(
                    row=2, column=0, sticky="w"
                )
                tk.Checkbutton(group, text="BD", variable=allow_bd_var, font=font_small, anchor="w").grid(
                    row=3, column=0, sticky="w"
                )
                round_vars["allow_3p"] = allow_3p_var
                round_vars["allow_4p"] = allow_4p_var
                round_vars["allow_bd"] = allow_bd_var
            elif round_num == 6:
                specific_sol_var = tk.BooleanVar(value=bool(default.get("specific_sol", True)))
                tk.Checkbutton(
                    group,
                    text="Specific Sol%",
                    variable=specific_sol_var,
                    font=("Consolas", 7),
                    anchor="w",
                ).grid(row=1, column=0, sticky="w")
                priority_var = tk.StringVar(value=default.get("priority", ""))
                self.create_setup_option_value_label(group, priority_var, row=2)
                round_vars["specific_sol"] = specific_sol_var
                round_vars["priority"] = priority_var
            else:
                priority_var = tk.StringVar(value=default.get("priority", ""))
                self.create_setup_option_value_label(group, priority_var, row=1)
                round_vars["priority"] = priority_var

            for variable in round_vars.values():
                variable.trace_add("write", self.on_setup_option_changed)

            self.setup_option_vars[round_num] = round_vars

        tk.Label(
            self.setup_option_frame,
            text="큐는 최신 CDP snapshot 값을 자동 사용합니다.",
            anchor="w",
            font=("Malgun Gothic", 8),
            fg="#6f6a61",
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=4, pady=(0, 2))

    def create_setup_option_value_label(self, parent, variable, row):
        holder = tk.Frame(parent, bd=1, relief="sunken", bg="#fbfbfb")
        holder.grid(row=row, column=0, sticky="ew", pady=(2, 0))
        tk.Label(
            holder,
            textvariable=variable,
            font=("Consolas", 8, "bold"),
            bg="#fbfbfb",
            fg="#2d2d2d",
            justify="center",
            padx=4,
            pady=2,
        ).pack(fill="x")

    def build_state_status_panel(self):
        self.state_frame = tk.LabelFrame(
            self.root,
            text="BROWSER STATE",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.state_frame.pack(fill="x", padx=14, pady=(0, 6))

        for variable in (
            self.browser_status_var,
            self.game_state_var,
            self.mode_var,
            self.identity_var,
            self.counter_var,
            self.board_size_var,
            self.queue_status_var,
            self.age_var,
            self.detail_var,
        ):
            tk.Label(
                self.state_frame,
                textvariable=variable,
                anchor="w",
                justify="left",
                font=("Consolas", 9),
            ).pack(fill="x", padx=8, pady=1)

    def print_message(self, text):
        self.clear_output_view(text)

    def clear_output_view(self, text=""):
        self.output.delete("all")
        self.current_output_mode = "placeholder"
        self.output_hint_var.set((text or "").strip())
        header_bottom = self.draw_output_context_header()
        self.output.create_text(
            self.solve_canvas_width / 2,
            max(self.solve_canvas_height / 2, header_bottom + 90),
            text="PC 해법 또는\n최적 셋업 카드가\n여기에 표시됩니다.",
            fill="#8f8a80",
            font=("Consolas", 15, "bold"),
            justify="center",
        )
        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, self.solve_canvas_height))
        self.reset_output_scroll()

    def render_setup_empty_state(self, message):
        self.output.delete("all")
        self.current_output_mode = "setup"
        self.output_hint_var.set((message or "").strip())
        header_bottom = self.draw_output_context_header()
        self.output.create_text(
            self.solve_canvas_width / 2,
            max(self.solve_canvas_height / 2, header_bottom + 70),
            text=message or "현재 옵션으로 셋업을 찾지 못했습니다.",
            fill="#8f8a80",
            font=("Malgun Gothic", 12, "bold"),
            justify="center",
        )
        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, self.solve_canvas_height))
        self.reset_output_scroll()

    def get_setup_options_by_round(self):
        options = {}
        for round_num, round_vars in getattr(self, "setup_option_vars", {}).items():
            round_options = {}
            if "mode" in round_vars:
                round_options["mode"] = (round_vars["mode"].get() or "").strip()
            if "priority" in round_vars:
                round_options["priority"] = (round_vars["priority"].get() or "").strip().upper()
            if "allow_3p" in round_vars:
                round_options["allow_3p"] = bool(round_vars["allow_3p"].get())
            if "allow_4p" in round_vars:
                round_options["allow_4p"] = bool(round_vars["allow_4p"].get())
            if "allow_bd" in round_vars:
                round_options["allow_bd"] = bool(round_vars["allow_bd"].get())
            if "specific_sol" in round_vars:
                round_options["specific_sol"] = bool(round_vars["specific_sol"].get())
            options[round_num] = round_options
        return options

    def on_setup_option_changed(self, *_args):
        if self.setup_option_refresh_job is not None:
            self.root.after_cancel(self.setup_option_refresh_job)
        self.setup_option_refresh_job = self.root.after(120, self.refresh_setup_options_preview)

    def refresh_setup_options_preview(self):
        self.setup_option_refresh_job = None

        if self.current_output_mode != "setup" or not self.last_result:
            return

        variants = self.build_setup_variants(self.last_result)
        round_text = self.build_round_status_suffix(self.last_result)
        if variants:
            self.render_setup_groups(variants)
            self.status_label.config(text=f"최적 셋업 옵션 반영 완료{round_text}")
        else:
            self.render_setup_empty_state("현재 옵션으로\n최적 셋업을 찾지 못했습니다.")
            self.status_label.config(text=f"현재 옵션으로 최적 셋업 없음{round_text}")

    def get_scan_context(self, hydra_result=None, solver_result=None):
        result = self.last_result or {}
        active_guess = (result.get("current") or result.get("active_guess") or "").strip().upper()
        active_input = (self.active_var.get() or "").strip().upper()
        active_effective = active_input or active_guess or "-"
        hold = (result.get("hold") or "").strip().upper() or "-"
        queue_list = [piece for piece in (result.get("queue") or []) if piece]
        queue_text = "".join(queue_list) or "-"
        manual_see = (self.manual_see_var.get() or "").strip().upper()

        if solver_result and solver_result.get("queue_text"):
            see_text = solver_result.get("queue_text")
        elif hydra_result and hydra_result.get("see"):
            see_text = hydra_result.get("see")
        else:
            try:
                see_text = make_hydra_see_string(
                    "" if active_effective == "-" else active_effective,
                    "" if hold == "-" else hold,
                    queue_list,
                    manual_see=manual_see,
                )
            except Exception:
                see_text = manual_see or "-"

        return {
            "current": active_guess or "-",
            "active_guess": active_guess or "-",
            "active_effective": active_effective,
            "hold": hold,
            "queue": queue_text,
            "see": see_text or "-",
        }

    def draw_output_context_header(self, hydra_result=None, solver_result=None):
        if not self.last_result:
            return 0

        context = self.get_scan_context(hydra_result=hydra_result, solver_result=solver_result)
        x = 8
        y = 8
        width = self.solve_canvas_width - 16
        height = 52

        self.output.create_rectangle(
            x,
            y,
            x + width,
            y + height,
            fill="#f6f2e8",
            outline="#ddd5c7",
        )
        self.output.create_text(
            x + 10,
            y + 12,
            text=(
                f"ACTIVE {context['active_effective']}   "
                f"HOLD {context['hold']}   "
                f"QUEUE {context['queue']}"
            ),
            anchor="w",
            fill="#252525",
            font=("Consolas", 10, "bold"),
        )
        self.output.create_text(
            x + 10,
            y + 32,
            text=(
                f"{'SOLVE' if solver_result else 'SEE'} {context['see']}   "
                f"CURRENT {context['current']}"
            ),
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 9, "bold"),
        )
        return y + height + 8

    def set_scan_buttons_state(self, state):
        self.pc_scan_button.config(state=state)
        self.setup_scan_button.config(state=state)

    def scan_for_pc_solve(self, show_popup=True):
        self.scan(scan_target="pc_solve", show_popup=show_popup)

    def scan_for_setup_finder(self, show_popup=True):
        self.scan(scan_target="setup", show_popup=show_popup)

    def schedule_auto_scan(self, delay_ms=None):
        if not self.auto_scan_enabled:
            return

        if self.auto_scan_job is not None:
            self.root.after_cancel(self.auto_scan_job)

        wait_ms = self.auto_scan_interval_ms if delay_ms is None else delay_ms
        self.auto_scan_job = self.root.after(wait_ms, self.auto_scan_tick)

    def auto_scan_tick(self):
        self.auto_scan_job = None
        self.scan_for_pc_solve(show_popup=False)

    def on_close(self):
        self.is_closing = True
        self.auto_scan_enabled = False
        if self.auto_scan_job is not None:
            self.root.after_cancel(self.auto_scan_job)
            self.auto_scan_job = None
        if self.hotkey_poll_job is not None:
            self.root.after_cancel(self.hotkey_poll_job)
            self.hotkey_poll_job = None
        if self.state_status_job is not None:
            self.root.after_cancel(self.state_status_job)
            self.state_status_job = None
        self.unregister_global_hotkeys()
        close_gomen_sessions()
        close_hydra_sessions()
        self.state_source.close()
        self.root.destroy()

    def reload_config(self):
        try:
            self.config = load_config("config.json")
            self.state_source.reload_config()
            self.status_label.config(text="CDP 설정 다시 읽음")
        except Exception as exc:
            messagebox.showerror("설정 오류", str(exc))

    def connect_browser_source(self):
        try:
            self.state_source.start()
            self.status_label.config(text="브라우저 연결 시도 중...")
        except Exception as exc:
            self.status_label.config(text="브라우저 연결 실패")
            messagebox.showerror("브라우저 연결 오류", str(exc))

    def start_browser_source(self):
        if self.is_closing:
            return
        try:
            self.state_source.start()
        except Exception as exc:
            self.status_label.config(text=f"브라우저 연결 대기: {exc}")
        self.refresh_browser_status()

    def refresh_browser_status(self):
        self.state_status_job = None
        if self.is_closing:
            return

        status = self.state_source.get_status()
        identity = status.get("round_id") or status.get("game_id") or "-"
        age_ms = status.get("last_update_age_ms")
        age_text = "-" if age_ms is None else f"{age_ms}ms"
        queue_text = status.get("queue") or "-"

        self.browser_status_var.set(f"Browser: {status.get('browser_status', 'Unknown')}")
        self.game_state_var.set(f"Game state: {status.get('game_state', 'Unknown')}")
        self.mode_var.set(f"Mode: {status.get('mode', 'Unknown')}")
        self.identity_var.set(f"Game ID: {identity}")
        self.counter_var.set(f"Piece counter: {status.get('piece_counter', '-')}")
        self.board_size_var.set(f"Board size: {status.get('board_size', '-')}")
        self.queue_status_var.set(
            f"Current/Hold/Queue: {status.get('current', '-')} / {status.get('hold', '-')} / {queue_text}"
        )
        self.age_var.set(f"Last update age: {age_text}")
        self.detail_var.set(f"Detail: {status.get('detail', '-')}")

        self.state_status_job = self.root.after(500, self.refresh_browser_status)

    def start_hydra_warmup(self):
        if self.is_closing or self.is_hydra_warming or self.hydra_warm_ready:
            return

        self.is_hydra_warming = True
        current_text = self.hydra_result_label.cget("text")
        if not str(current_text).startswith("PC SOLVER"):
            self.hydra_result_label.config(text="HYDRA: 예열 중...")

        worker = threading.Thread(target=self._hydra_warmup_worker, daemon=True)
        worker.start()

    def start_pc_solver_warmup(self):
        if self.is_closing or self.is_pc_solver_warming or self.pc_solver_warm_ready:
            return

        self.is_pc_solver_warming = True
        worker = threading.Thread(target=self._pc_solver_warmup_worker, daemon=True)
        worker.start()

    def scan(self, scan_target="setup", show_popup=True):
        if self.is_scanning or self.is_closing:
            return

        self.is_scanning = True
        if scan_target == "pc_solve":
            self.status_label.config(text="PC 해법용 게임 상태 읽는 중...")
        else:
            self.status_label.config(text="최적 셋업용 게임 상태 읽는 중...")
        self.set_scan_buttons_state("disabled")

        worker = threading.Thread(
            target=self._scan_worker,
            args=(scan_target, show_popup),
            daemon=True,
        )
        worker.start()

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.always_on_top.get())
        if self.always_on_top.get():
            self.status_label.config(text="창 항상 위 고정 ON")
        else:
            self.status_label.config(text="창 항상 위 고정 OFF")

    def build_round_status_suffix(self, result):
        round_info = self.get_pc_round_info(result)
        parts = []

        if round_info["pc_round"] is not None:
            parts.append(f"{round_info['pc_round']}회차")
        if round_info["pc_progress"] is not None:
            parts.append(f"{round_info['pc_progress']}/10p")
        if round_info["pieces_count"] is not None:
            parts.append(f"총 {round_info['pieces_count']}p")
        if round_info["bag_in_cycle"] is not None:
            parts.append(f"가방 {round_info['bag_in_cycle']}")

        if not parts:
            return ""

        return f" ({' / '.join(parts)})"

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
                f"piece counter 대기 | "
                f"구조 {info['structure']}"
            )

        return (
            f"현재 PC: {info['pc_round']}회차 | "
            f"{info['pc_progress']}/10p | "
            f"총 {info['pieces_count']}p | "
            f"가방 {info['bag_in_cycle']} | "
            f"구조 {info['structure']}"
        )

    def run_hydra_now(self, show_popup=True, force=True):
        if self.is_hydra_running or self.is_closing:
            return

        if not self.last_result:
            if show_popup:
                messagebox.showwarning("Hydra", "먼저 게임 상태를 읽어야 합니다.")
            return

        board = self.last_result["board"]
        active_guess = self.last_result.get("active_guess")
        hold = self.last_result["hold"]
        queue = self.last_result["queue"]

        active = (self.active_var.get() or "").strip().upper()
        manual_see = (self.manual_see_var.get() or "").strip().upper()
        bag_arg = self.bag_var.get()

        if not active and not manual_see and not active_guess and not show_popup:
            self.hydra_result_label.config(text="HYDRA: Current piece 대기 중")
            self.status_label.config(text="Hydra current piece 대기 중")
            self.print_message("current piece가 준비되면 Hydra 해법 계산이 시작됩니다.")
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

        if not force and not show_popup and hydra_signature == self.last_hydra_signature:
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

    def run_pc_solver_now(self, show_popup=True, force=True):
        if self.is_pc_solving or self.is_closing:
            return

        if not self.last_result:
            if show_popup:
                messagebox.showwarning("PC Solver", "먼저 게임 상태를 읽어야 합니다.")
            return

        board = self.last_result["board"]
        active_guess = (self.last_result.get("active_guess") or "").strip().upper()
        hold = (self.last_result.get("hold") or "").strip().upper()
        queue = self.last_result["queue"]
        active = (self.active_var.get() or "").strip().upper()
        manual_see = (self.manual_see_var.get() or "").strip().upper()

        if not active and not manual_see and not active_guess:
            self.hydra_result_label.config(text="PC SOLVER: Current piece 대기 중")
            self.status_label.config(text="PC 해법 대기 - current 없음")
            self.print_message("current piece가 준비되면 PC 해법 계산이 시작됩니다.")
            return

        pc_signature = (
            tuple("".join(row) for row in board),
            active_guess,
            hold,
            tuple(queue),
            active,
            manual_see,
        )
        if not force and not show_popup and pc_signature == self.last_pc_signature:
            self.status_label.config(text="PC 해법 계산 생략 - 같은 상태")
            return

        self.last_pc_signature = pc_signature
        self.is_pc_solving = True

        if show_popup or self.current_output_mode != "setup":
            self.clear_output_view("PC 해법 계산 중...")
        else:
            self.output_hint_var.set("추천 셋업 표시 중\nPC 해법 계산 중...")

        self.hydra_result_label.config(text="PC SOLVER: 계산 중...")
        self.status_label.config(text="PC 해법 계산 중...")

        worker = threading.Thread(
            target=self._run_pc_solver_worker,
            args=(board, active_guess, hold, queue, active, manual_see, show_popup),
            daemon=True,
        )
        worker.start()

    def render_solution_groups(self, variants, hydra_result=None):
        self.output.delete("all")
        self.current_output_mode = "hydra"

        card_width = 260
        card_height = 120
        gap_y = 12
        start_x = 8
        start_y = self.draw_output_context_header(hydra_result=hydra_result)

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
        self.reset_output_scroll()
        self.output_hint_var.set("\n".join(summary_lines))

    def render_pc_solution_groups(self, variants, solver_result=None):
        self.output.delete("all")
        self.current_output_mode = "pc_solve"

        card_width = 260
        card_height = 128
        gap_y = 12
        start_x = 8
        start_y = self.draw_output_context_header(solver_result=solver_result)
        visible = variants[:6]
        for index, variant in enumerate(visible):
            y = start_y + index * (card_height + gap_y)
            self.draw_pc_solution_card(start_x, y, card_width, card_height, variant)

        bottom = start_y + len(visible) * (card_height + gap_y)
        self.output.config(
            scrollregion=(0, 0, self.solve_canvas_width, max(bottom, self.solve_canvas_height))
        )
        self.reset_output_scroll()
        self.output_hint_var.set("")

    def draw_pc_solution_card(self, x, y, width, height, variant):
        title = variant.get("title") or "해법"
        preview_rows = variant.get("preview_rows") or []
        queue_text = variant.get("queue_text") or ""
        state_queue = variant.get("state_queue") or queue_text

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

        if preview_rows:
            self.draw_site_preview_board(preview_rows, x + 10, y + 22, scale=0.55)
        else:
            self.output.create_text(
                x + 10,
                y + 46,
                text="배치 미리보기 없음",
                anchor="w",
                fill="#8f6f4a",
                font=("Malgun Gothic", 8, "bold"),
            )

        self.output.create_text(
            x + 120,
            y + 34,
            text="QUEUE",
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 8, "bold"),
        )
        self.draw_piece_strip(queue_text, x + 120, y + 42, block_size=14, gap=2)
        self.output.create_text(
            x + 10,
            y + 108,
            text=f"STATE {state_queue}",
            anchor="w",
            fill="#252525",
            font=("Consolas", 9, "bold"),
        )

    def draw_solution_compact_card(self, x, y, width, height, variant):
        solution = variant.get("solution") or {}
        title = variant.get("title") or "해법"
        pieces = solution.get("pieces") or []
        preview_rows = self.build_compact_solution_rows(
            solution,
            setup_rows=self.get_current_solution_setup_rows(),
        )

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

        if preview_rows and any(any(ch not in ".X" for ch in row) for row in preview_rows):
            self.draw_preview_board(
                preview_rows,
                x + 10,
                y + 28,
                cell_size=10,
            )
        else:
            self.output.create_text(
                x + 10,
                y + 46,
                text="배치 미리보기 복원 실패",
                anchor="w",
                fill="#8f6f4a",
                font=("Malgun Gothic", 8, "bold"),
            )

        self.output.create_text(
            x + 116,
            y + 32,
            text="순서",
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 8, "bold"),
        )
        self.draw_piece_strip("".join(pieces[:6]), x + 116, y + 40, block_size=14, gap=2)

        piece_text = " -> ".join(pieces) if pieces else "-"
        self.output.create_text(
            x + 10,
            y + 88,
            text=piece_text,
            anchor="w",
            fill="#252525",
            font=("Consolas", 9, "bold"),
        )

    def decode_gomen_cells(self, cells_text):
        text = str(cells_text or "").strip().upper().replace("_", ".")
        filtered = "".join(ch for ch in text if ch in ".GIJLOSTZX")
        if not filtered:
            return []

        if len(filtered) < 40:
            filtered += "." * (40 - len(filtered))
        filtered = filtered[:40]

        rows = []
        for start in range(0, 40, 10):
            rows.append(filtered[start:start + 10])
        return rows

    def get_gomen_order_groups(self, solution):
        raw_groups = solution.get("order_groups") or []
        normalized_groups = []
        for group in raw_groups:
            normalized_groups.append(
                [str(item or "").strip().upper() for item in group if item]
            )

        while len(normalized_groups) < 2:
            normalized_groups.append([])

        return {
            "without_hold": normalized_groups[0],
            "with_hold": normalized_groups[1],
        }

    def build_pc_variant(self, index, title, solution, queue_text):
        rows = self.decode_gomen_cells(solution.get("cells", ""))
        queue_value = str(queue_text or "").strip().upper()
        if not queue_value:
            return None

        return {
            "title": title if title.startswith(f"{index}. ") else f"{index}. {title}",
            "preview_rows": rows,
            "queue_text": queue_value,
            "piece_text": queue_value,
            "solution": solution,
        }

    def build_pc_solver_variants(self, solver_result, active="", hold=""):
        variants = []
        solutions = solver_result.get("solutions") or []
        queue_text = str(solver_result.get("queue_text") or "").strip().upper()
        state_queue = str(solver_result.get("state_queue") or queue_text).strip().upper()
        base_title = f"ACTIVE {active}" if active else "SOLVE"

        if solutions and queue_text:
            for index, solution in enumerate(solutions[:6], start=1):
                variant = self.build_pc_variant(
                    index=index,
                    title=base_title,
                    solution=solution,
                    queue_text=queue_text,
                )
                if not variant:
                    continue
                variant["state_queue"] = state_queue
                variant["matched_group"] = solution.get("matched_group") or ""
                variants.append(variant)

        return variants

    def get_current_solution_setup_rows(self):
        board = (self.last_result or {}).get("board") or []
        if not board:
            return []

        rows = []
        for row in board[-4:]:
            row_text = "".join("X" if cell != "." else "." for cell in row[:10])
            rows.append((row_text + "." * 10)[:10])

        while len(rows) < 4:
            rows.insert(0, "." * 10)

        return rows[-4:]

    def clear_preview_full_rows(self, board):
        kept = []
        for row in board:
            if all(cell != "." for cell in row):
                continue
            kept.append(row[:])

        while len(kept) < 4:
            kept.insert(0, list("." * 10))

        return kept[-4:]

    def build_compact_solution_rows(self, solution, setup_rows=None):
        if not solution:
            return []

        steps = solution.get("steps") or []
        init_rows = setup_rows or solution.get("init_rows") or []

        if not init_rows and steps:
            init_rows = steps[0].get("prev_rows") or []

        board = []
        for row in init_rows[:4]:
            row_text = row if isinstance(row, str) else "".join(row)
            row_text = (row_text + "." * 10)[:10]
            board.append(list(row_text))

        while len(board) < 4:
            board.insert(0, list("." * 10))

        initial_mask = set()
        for row_index in range(4):
            for col_index in range(10):
                if board[row_index][col_index] != ".":
                    board[row_index][col_index] = "X"
                    initial_mask.add((row_index, col_index))

        for step in steps:
            piece = step.get("piece", "X")
            placed_rows = step.get("placed_rows")
            if not placed_rows:
                continue

            for row_index in range(min(4, len(placed_rows))):
                row = placed_rows[row_index]
                for col_index in range(min(10, len(row))):
                    if row[col_index] == "X":
                        board[row_index][col_index] = piece

            board = self.clear_preview_full_rows(board)

        final_rows = []
        if steps:
            final_rows = steps[-1].get("rows") or []
        if not final_rows:
            final_rows = solution.get("init_rows") or []

        normalized_final = []
        for row in final_rows[:4]:
            row_text = row if isinstance(row, str) else "".join(row)
            normalized_final.append((row_text + "." * 10)[:10])
        while len(normalized_final) < 4:
            normalized_final.insert(0, "." * 10)

        for row_index in range(4):
            for col_index in range(10):
                final_cell = normalized_final[row_index][col_index]
                if final_cell == "X":
                    if (row_index, col_index) in initial_mask:
                        board[row_index][col_index] = "X"
                    elif board[row_index][col_index] == ".":
                        board[row_index][col_index] = "X"
                    else:
                        continue
                else:
                    board[row_index][col_index] = "."

        return ["".join(row) for row in board]

    def render_setup_groups(self, variants):
        self.output.delete("all")
        self.current_output_mode = "setup"

        card_width = 132
        card_height = 164
        gap_x = 12
        gap_y = 14
        start_x = 8
        start_y = self.draw_output_context_header()
        visible = variants[:6]

        for index, variant in enumerate(visible):
            col = index % 2
            row = index // 2
            x = start_x + col * (card_width + gap_x)
            y = start_y + row * (card_height + gap_y)
            self.draw_setup_card(x, y, card_width, card_height, variant)

        rows_used = (len(visible) + 1) // 2
        bottom = start_y + rows_used * (card_height + gap_y)
        self.output.config(scrollregion=(0, 0, self.solve_canvas_width, max(bottom, self.solve_canvas_height)))
        self.reset_output_scroll()
        self.output_hint_var.set(
            "\n".join(
                f"{variant['title']}: {variant['setup']['id']} / {variant.get('display_lookup_text') or variant.get('lookup_queue_text') or variant['queue_text'][:3]}"
                for variant in visible
            )
        )

    def draw_setup_card(self, x, y, width, height, variant):
        setup = variant["setup"]
        queue_text = variant["queue_text"]
        lookup_queue_text = variant.get("lookup_queue_text") or queue_text
        display_lookup_text = variant.get("display_lookup_text") or lookup_queue_text
        round_text = variant.get("round_text") or "-"
        setup_id = setup.get("id", "-")
        option_label = setup.get("option_label", "")
        preview_rows = variant.get("preview_rows") or []
        match_text = setup.get("match", "")
        percent_text = setup.get("percent", "")
        display_sequence = match_text or display_lookup_text[:5]
        detail_text = f"MATCH {match_text}" if match_text else f"LOOKUP {display_lookup_text}"
        if percent_text:
            detail_text += f" ({percent_text})"

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
        if option_label:
            self.output.create_text(
                x + width - 10,
                y + 12,
                text=option_label,
                anchor="e",
                fill="#816d3d",
                font=("Consolas", 8, "bold"),
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
            y + 88,
            text="SEQ",
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 8, "bold"),
        )
        self.draw_piece_strip(display_sequence, x + 38, y + 82, block_size=11, gap=2)

        self.output.create_text(
            x + 10,
            y + 106,
            text=setup_id,
            anchor="w",
            fill="#111111",
            font=("Consolas", 15, "bold"),
        )
        self.output.create_text(
            x + 10,
            y + 126,
            text=detail_text,
            anchor="w",
            fill="#3a3a3a",
            font=("Consolas", 9),
        )
        self.output.create_text(
            x + 10,
            y + 144,
            text=f"SOL {setup.get('sol', '-')} / STATE {queue_text[:6]}",
            anchor="w",
            fill="#3a3a3a",
            font=("Consolas", 8),
        )

    def build_setup_variants(self, result):
        if find_setup_for_pc is None:
            return []

        board = result.get("board") or []
        active = result.get("active_guess") or ""
        hold = result.get("hold") or ""
        queue = [piece for piece in result.get("queue", []) if piece]
        queue_text_only = "".join(queue)
        round_info = self.get_pc_round_info(result)
        pieces_count = round_info["pieces_count"]
        round_from_counter = round_info["pc_round"]
        pc_progress = round_info["pc_progress"]
        bag_in_cycle = round_info["bag_in_cycle"]

        if not active:
            return []

        if round_from_counter not in (1, 2, 3, 4, 5, 6, 7):
            return []

        setup_options = self.get_setup_options_by_round()

        state_queue = make_state_queue(active, queue)
        candidates = []
        active_lookup_queue = state_queue
        if hold and hold != active:
            active_lookup_queue = active + hold + queue_text_only
        candidates.append(("1. ACTIVE 시작", active_lookup_queue, state_queue))
        if hold and hold != active:
            hold_lookup_queue = hold + state_queue
            candidates.append(("2. HOLD 시작", hold_lookup_queue, state_queue))

        variants = []
        seen_keys = set()
        for title, lookup_queue_text, state_queue_text in candidates:
            try:
                if find_setup_candidates_for_pc is not None:
                    found_items = find_setup_candidates_for_pc(
                        lookup_queue_text,
                        round_from_counter,
                        options=setup_options,
                        limit=3,
                    )
                else:
                    found = find_setup_for_pc(
                        lookup_queue_text,
                        round_from_counter,
                        options=setup_options,
                    )
                    found_items = [found] if found.get("ok") else []
            except Exception:
                continue

            for index, found in enumerate(found_items, start=1):
                if not found.get("ok"):
                    continue

                setup = found.get("result") or {}
                setup_id = setup.get("id")
                unique_key = (
                    setup_id,
                    setup.get("fumen", ""),
                    setup.get("sol", ""),
                )
                if not setup_id or unique_key in seen_keys:
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

                card_title = title if len(found_items) == 1 else f"{title} #{index}"
                display_lookup_text = (setup.get("match") or found.get("queue") or lookup_queue_text)[:6]
                variants.append(
                    {
                        "title": card_title,
                        "queue_text": state_queue_text,
                        "lookup_queue_text": found.get("queue") or lookup_queue_text,
                        "display_lookup_text": display_lookup_text,
                        "setup": setup,
                        "round_text": round_text,
                        "preview_rows": self.assign_setup_piece_colors(
                            self.decode_setup_preview(setup.get("fumen", "")),
                            setup.get("match") or found.get("queue") or lookup_queue_text,
                        ),
                    }
                )
                seen_keys.add(unique_key)

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
        if piece == ".":
            return "#050505"
        if piece in ("X", "G"):
            rgb = DEFAULT_PIECE_COLORS.get("G", [134, 134, 134])
            return self.rgb_to_hex(rgb)
        if piece in DEFAULT_PIECE_COLORS:
            return self.rgb_to_hex(DEFAULT_PIECE_COLORS[piece])
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
                elif piece in ("X", "G"):
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

    def get_site_board_palette(self, piece):
        regular = {
            "G": "#686868",
            "I": "#41AFDE",
            "J": "#1883BF",
            "L": "#EF9536",
            "O": "#F7D33E",
            "S": "#66C65C",
            "T": "#B451AC",
            "Z": "#EF624D",
        }
        top = {
            "G": "#949494",
            "I": "#43D3FF",
            "J": "#1BA6F9",
            "L": "#FFBF60",
            "O": "#FFF952",
            "S": "#88EE86",
            "T": "#E56ADD",
            "Z": "#FF9484",
        }
        key = "G" if piece in ("X", "G") else piece
        return regular.get(key, "#686868"), top.get(key, "#949494")

    def draw_site_preview_board(self, rows, x, y, scale=0.5):
        if not rows:
            return

        cell = 20 * scale
        top_height = 4 * scale
        shadow_x = 5 * scale
        shadow_y = 7 * scale
        width = 10 * cell
        height = (len(rows) + 2) * cell

        self.output.create_rectangle(
            x,
            y,
            x + width,
            y + height,
            fill="#F3F3ED",
            outline="#DDD5C7",
        )

        for row_index, row in enumerate(rows):
            for col_index, piece in enumerate(row[:10]):
                if piece in (".", "_"):
                    continue

                regular_color, top_color = self.get_site_board_palette(piece)
                cell_x = x + col_index * cell
                cell_y = y + (row_index + 2) * cell

                self.output.create_rectangle(
                    cell_x + shadow_x,
                    cell_y + shadow_y,
                    cell_x + shadow_x + cell,
                    cell_y + shadow_y + cell,
                    fill="#E7E7E2",
                    outline="",
                )
                self.output.create_rectangle(
                    cell_x,
                    cell_y - top_height,
                    cell_x + cell,
                    cell_y,
                    fill=top_color,
                    outline="",
                )
                self.output.create_rectangle(
                    cell_x,
                    cell_y,
                    cell_x + cell,
                    cell_y + cell,
                    fill=regular_color,
                    outline="",
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

    def build_piece_variants(self, piece):
        coords = TETROMINO_BASE_COORDS.get(piece)
        if not coords:
            return []

        variants = set()
        rotated = list(coords)
        for _ in range(4):
            min_r = min(r for r, _ in rotated)
            min_c = min(c for _, c in rotated)
            normalized = tuple(sorted((r - min_r, c - min_c) for r, c in rotated))
            variants.add(normalized)
            rotated = [(-c, r) for r, c in rotated]

        return [list(variant) for variant in variants]

    def build_piece_placements(self, occupied, piece, row_count, col_count):
        placements = []

        for variant in self.build_piece_variants(piece):
            max_r = max(r for r, _ in variant)
            max_c = max(c for _, c in variant)

            for base_r in range(row_count - max_r):
                for base_c in range(col_count - max_c):
                    cells = {
                        (base_r + r, base_c + c)
                        for r, c in variant
                    }
                    if cells.issubset(occupied):
                        placements.append(cells)

        return placements

    def assign_setup_piece_colors(self, rows, sequence_text):
        if not rows:
            return rows

        occupied = {
            (row_index, col_index)
            for row_index, row in enumerate(rows)
            for col_index, cell in enumerate(row[:10])
            if cell == "X"
        }
        if not occupied:
            return rows

        visible_piece_count = len(occupied) // 4
        if visible_piece_count <= 0 or visible_piece_count * 4 != len(occupied):
            return rows

        sequence = [
            piece for piece in str(sequence_text or "").upper()
            if piece in TETROMINO_BASE_COORDS
        ]
        if len(sequence) < visible_piece_count:
            return rows

        row_count = len(rows)
        col_count = min(10, max((len(row) for row in rows), default=10))

        def search(remaining_cells, pieces_left):
            if not pieces_left:
                return {} if not remaining_cells else None

            anchor = min(remaining_cells)

            tried = set()
            for index, piece in enumerate(pieces_left):
                if piece in tried:
                    continue
                tried.add(piece)

                placements = self.build_piece_placements(remaining_cells, piece, row_count, col_count)
                for placement in placements:
                    if anchor not in placement:
                        continue

                    next_remaining = remaining_cells - placement
                    next_pieces = pieces_left[:index] + pieces_left[index + 1:]
                    found = search(next_remaining, next_pieces)
                    if found is not None:
                        found = dict(found)
                        for cell in placement:
                            found[cell] = piece
                        return found

            return None

        piece_choices = [tuple(sequence)]
        if len(sequence) > visible_piece_count:
            piece_choices = [
                tuple(sequence[index] for index in combo)
                for combo in combinations(range(len(sequence)), visible_piece_count)
            ]

        for pieces in piece_choices:
            matched = search(set(occupied), list(pieces))
            if matched is None:
                continue

            colored_rows = [list(row[:10]) for row in rows]
            for row_index, col_index in occupied:
                colored_rows[row_index][col_index] = matched.get((row_index, col_index), "X")
            return colored_rows

        fallback_pieces = sequence[:visible_piece_count]
        if len(fallback_pieces) == visible_piece_count:
            colored_rows = [list(row[:10]) for row in rows]
            ordered_cells = sorted(
                occupied,
                key=lambda cell: (-cell[0], cell[1]),
            )
            for piece_index, piece in enumerate(fallback_pieces):
                chunk = ordered_cells[piece_index * 4:(piece_index + 1) * 4]
                for row_index, col_index in chunk:
                    colored_rows[row_index][col_index] = piece
            return colored_rows

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

    def _scan_worker(self, scan_target, show_popup):
        try:
            print("[STATE] read start")
            result = self.state_source.get_latest_result()
            if result is None:
                detail = self.state_source.get_status().get("detail") or "Waiting for game state"
                raise RuntimeError(detail)

            print(
                "[STATE RESULT]",
                "pieces_count=", result.get("pieces_count"),
                "pc_round=", result.get("pc_round"),
                "current=", result.get("current"),
                "hold=", result.get("hold"),
                "queue=", result.get("queue"),
            )

            self._post_to_ui(self._on_scan_success, result, scan_target)

        except Exception as exc:
            print("[STATE ERROR]", repr(exc))
            self._post_to_ui(self._on_scan_error, str(exc), show_popup)

        finally:
            print("[STATE] finished")
            self._post_to_ui(self._on_scan_finished)

    def _on_scan_success(self, result, scan_target):
        self.last_result = result
        self.pc_round_label.config(text=self.format_pc_round_info(result))

        print(
            "[STATE RESULT]",
            "pieces_count=", result.get("pieces_count"),
            "pc_round=", result.get("pc_round"),
            "current=", result.get("current"),
            "hold=", result.get("hold"),
            "queue=", result.get("queue"),
        )
        scan_signature = self._make_scan_signature(result)
        same_scan = scan_signature == self.last_scan_signature
        self.last_scan_signature = scan_signature

        self.draw_board(result["board"])
        round_text = self.build_round_status_suffix(result)

        if scan_target == "setup":
            setup_variants = self.build_setup_variants(result)
            if setup_variants:
                self.render_setup_groups(setup_variants)
                if same_scan:
                    self.status_label.config(text=f"최적 셋업 다시 표시{round_text}")
                else:
                    self.status_label.config(text=f"최적 셋업 찾기 완료{round_text}")
            else:
                self.print_message(
                    "최적 셋업을 찾지 못했습니다.\n"
                    "회차/큐 인식이 맞는지 확인해 주세요."
                )
                if same_scan:
                    self.status_label.config(text=f"최적 셋업 없음 - 같은 상태{round_text}")
                else:
                    self.status_label.config(text=f"최적 셋업 없음{round_text}")
            return

        if scan_target == "pc_solve":
            active = (self.active_var.get() or "").strip().upper()
            manual_see = (self.manual_see_var.get() or "").strip().upper()

            if not result.get("current") and not active and not manual_see:
                self.hydra_result_label.config(text="HYDRA: Current piece 대기 중")
                self.print_message(
                    "PC 해법을 찾으려면 current piece가 필요합니다.\n"
                    "다음 CDP snapshot을 기다리거나 ACTIVE/SEE를 직접 입력해 주세요."
                )
                self.status_label.config(text=f"PC 해법 대기 - current 없음{round_text}")
                return

            self.run_pc_solver_now(show_popup=False, force=True)
            return

        self.print_message("상태 읽기 완료.")
        if same_scan:
            self.status_label.config(text=f"상태 읽기 완료 (변화 없음){round_text}")
        else:
            self.status_label.config(text=f"상태 읽기 완료{round_text}")

    def _on_scan_error(self, error_text, show_popup):
        self.status_label.config(text="게임 상태 읽기 실패")
        if show_popup:
            messagebox.showerror("게임 상태 오류", error_text)

    def _on_scan_finished(self):
        self.is_scanning = False
        if not self.is_closing:
            self.set_scan_buttons_state("normal")
            if self.auto_scan_enabled:
                self.schedule_auto_scan()

    def _run_pc_solver_worker(self, board, active_guess, hold, queue, active, manual_see, show_popup):
        try:
            active_piece = active or active_guess or ""
            result = run_gomen_solver(
                board=board,
                active=active_piece,
                hold=hold,
                queue=queue,
                manual_see=manual_see,
                limit=6,
                timeout_sec=20,
            )
            result["active"] = active_piece
            result["hold_piece"] = hold
            result["variants"] = self.build_pc_solver_variants(
                result,
                active=active_piece,
                hold=hold,
            )
            self._post_to_ui(self._on_pc_solver_success, result)
        except GomenError as exc:
            self._post_to_ui(self._on_pc_solver_error, str(exc), show_popup)
        except Exception as exc:
            self._post_to_ui(self._on_pc_solver_error, str(exc), show_popup)
        finally:
            self._post_to_ui(self._on_pc_solver_finished)

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

    def _on_pc_solver_success(self, result):
        shown_total = int(result.get("shown_total") or result.get("total") or 0)
        matched_total = int(result.get("matched_total") or 0)
        exact_match_used = bool(result.get("exact_match_used"))
        variants = result.get("variants") or []
        visible_total = len(variants[:6])
        fast_text = "fast" if result.get("fast") else "normal"
        queue_text = result.get("queue_text") or "-"
        duration_ms = result.get("duration_ms", "?")
        match_text = "exact" if exact_match_used else f"fallback({matched_total})"
        self.hydra_result_label.config(
            text=(
                f"PC SOLVER: 표시 {visible_total}개 / 후보 {shown_total}개 | "
                f"{duration_ms}ms | {fast_text} | {match_text} | queue={queue_text}"
            )
        )

        if variants:
            self.render_pc_solution_groups(variants, solver_result=result)
            self.status_label.config(text="PC 해법 계산 완료")
            return

        self.print_message("PC 해법은 찾았지만 카드로 표시할 수 있는 결과가 없습니다.")
        self.status_label.config(text="PC 해법 카드 없음")

    def _on_pc_solver_error(self, error_text, show_popup):
        headline = error_text.splitlines()[0] if error_text else "알 수 없는 오류"
        self.hydra_result_label.config(text=f"PC SOLVER 오류: {headline}")
        self.status_label.config(text="PC 해법 계산 실패")
        self.print_message("PC 해법 계산에 실패했습니다.")

        if show_popup:
            messagebox.showerror("PC Solver 오류", error_text)

    def _on_pc_solver_finished(self):
        self.is_pc_solving = False

    def _build_solution_variants(self, board, active, hold, queue, bag_arg, manual_see, primary_result):
        variants = []
        seen = set()

        primary_solution = primary_result.get("solution")
        primary_key = self._solution_key(primary_solution)
        if primary_solution and primary_key:
            variants.append(
                {
                    "title": self.make_solution_title(
                        index=1,
                        solution=primary_solution,
                        active=active,
                        hold=hold,
                    ),
                    "solution": primary_solution,
                }
            )
            seen.add(primary_key)

        if (not self.enable_hold_solution_variant) or manual_see or not hold or not active or hold == active:
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
                        "title": self.make_solution_title(
                            index=len(variants) + 1,
                            solution=alt_solution,
                            active=hold,
                            hold=active,
                        ),
                        "solution": alt_solution,
                    }
                )
                seen.add(alt_key)
        except HydraError:
            pass

        return variants

    def make_solution_title(self, index, solution, active, hold):
        pieces = solution.get("pieces") or []
        first_piece = pieces[0] if pieces else ""

        if first_piece and hold and first_piece == hold and hold != active:
            return f"{index}. HOLD {hold}"
        if first_piece and active and first_piece == active:
            return f"{index}. ACTIVE {active}"
        if first_piece:
            return f"{index}. START {first_piece}"
        if active:
            return f"{index}. ACTIVE {active}"
        return f"{index}. SOLVE"

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
        self.status_label.config(text="Hydra 해법 계산 완료")

        variants = result.get("solution_variants") or []
        if variants:
            self.render_solution_groups(variants, hydra_result=result)
            return

        solution = result.get("solution")
        if solution and solution.get("pieces"):
            active = result.get("active") or ""
            hold = (self.last_result or {}).get("hold") or ""
            self.render_solution_groups(
                [{
                    "title": self.make_solution_title(1, solution, active=active, hold=hold),
                    "solution": solution,
                }],
                hydra_result=result,
            )
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

    def _pc_solver_warmup_worker(self):
        try:
            warm_gomen_session(timeout_sec=30)
            self._post_to_ui(self._on_pc_solver_warmup_success)
        except Exception:
            self._post_to_ui(self._on_pc_solver_warmup_finished)

    def _on_hydra_warmup_success(self):
        self.hydra_warm_ready = True
        current_text = self.hydra_result_label.cget("text")
        if not self.is_hydra_running and not str(current_text).startswith("PC SOLVER"):
            self.hydra_result_label.config(text="HYDRA: 대기 중")
        self._on_hydra_warmup_finished()

    def _on_hydra_warmup_finished(self):
        self.is_hydra_warming = False

    def _on_pc_solver_warmup_success(self):
        self.pc_solver_warm_ready = True
        self._on_pc_solver_warmup_finished()

    def _on_pc_solver_warmup_finished(self):
        self.is_pc_solver_warming = False


if __name__ == "__main__":
    ensure_runtime_file("config.json")
    root = tk.Tk()
    app = TetrisScannerApp(root)
    root.mainloop()
