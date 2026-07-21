import ctypes
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
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

from gomen_helper import (
    GomenError,
    close_gomen_sessions,
    make_state_queue,
    run_gomen_solver,
    warm_gomen_session,
)
from app_paths import ensure_runtime_file, get_resource_path, get_user_data_path
from tetrio_state_source import (
    TETROMINO_BASE_COORDS,
    TetrioStateSource,
    VISIBLE_FIELD_PIECES,
    load_config,
)

VK_DELETE = 0x2E
VK_END = 0x23
BROWSER_READY_TIMEOUT_SEC = 10.0
BROWSER_READY_POLL_INTERVAL_SEC = 0.25
CDP_ACTION_PORT_TIMEOUT_SEC = 1.0
CDP_ACTION_SNAPSHOT_TIMEOUT_SEC = 8.0
BROWSER_LAUNCH_ENV_KEYS = ("TETRIO_BROWSER_PATH", "BROWSER_PATH", "CHROME_PATH")


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


def get_tetrio_cdp_config(config):
    if isinstance(config, dict):
        return config.get("tetrio_cdp", {}) or {}
    return {}


def get_cdp_port(config):
    raw_value = get_tetrio_cdp_config(config).get("port", 9222)
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return 9222


def get_tetrio_url(config):
    return str(get_tetrio_cdp_config(config).get("url") or "https://tetr.io/").strip() or "https://tetr.io/"


def get_browser_profile_path(config):
    raw_value = str(get_tetrio_cdp_config(config).get("browser_profile_dir") or "").strip()
    if raw_value:
        path = Path(raw_value)
        if path.is_absolute():
            path.mkdir(parents=True, exist_ok=True)
            return path
        return get_user_data_path(*path.parts)
    return get_user_data_path("browser-profile")


def iter_browser_candidate_paths(config=None, env=None):
    env = env or os.environ
    yield str(get_resource_path("runtime", "chromium", "chrome.exe"))

    config_browser = str(get_tetrio_cdp_config(config).get("browser_path") or "").strip()
    if config_browser:
        yield config_browser

    for env_key in BROWSER_LAUNCH_ENV_KEYS:
        env_browser = str(env.get(env_key) or "").strip()
        if env_browser:
            yield env_browser

    local_appdata = str(env.get("LOCALAPPDATA") or "").strip()
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    if local_appdata:
        chrome_candidates.append(
            str(Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )
    for candidate in chrome_candidates:
        yield candidate

    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        yield candidate


def resolve_browser_executable(config=None, env=None, exists_fn=os.path.exists):
    attempted_paths = []
    seen_paths = set()
    for candidate in iter_browser_candidate_paths(config=config, env=env):
        normalized = os.path.normcase(os.path.normpath(str(candidate)))
        if not candidate or normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        attempted_paths.append(str(candidate))
        if exists_fn(candidate):
            return {
                "executable": str(candidate),
                "attempted_paths": attempted_paths,
            }
    return {
        "executable": None,
        "attempted_paths": attempted_paths,
    }


def build_browser_launch_args(config, profile_dir=None):
    profile_path = Path(profile_dir) if profile_dir is not None else get_browser_profile_path(config)
    return [
        f"--remote-debugging-port={get_cdp_port(config)}",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        get_tetrio_url(config),
    ]


def build_browser_launch_command(config, browser_executable=None, profile_dir=None):
    resolved = browser_executable or resolve_browser_executable(config).get("executable")
    if not resolved:
        raise RuntimeError("사용 가능한 Chrome 또는 Edge 브라우저를 찾지 못했습니다.")
    return [resolved, *build_browser_launch_args(config, profile_dir=profile_dir)]


def is_cdp_endpoint_available(port, *, timeout_sec=0.5, urlopen_impl=urllib.request.urlopen):
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/json/version")
    try:
        with urlopen_impl(request, timeout=timeout_sec) as response:
            status_code = getattr(response, "status", 200)
            return int(status_code) == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def wait_for_cdp_endpoint(port, *, timeout_sec=BROWSER_READY_TIMEOUT_SEC, poll_interval_sec=BROWSER_READY_POLL_INTERVAL_SEC, probe_fn=None):
    probe = probe_fn or is_cdp_endpoint_available
    deadline = time.time() + max(0.1, float(timeout_sec))
    while time.time() < deadline:
        if probe(port):
            return True
        time.sleep(max(0.05, float(poll_interval_sec)))
    return False


class TetrisScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PYHOK12 TETR.IO CDP")
        self.root.geometry("620x890")
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
        self.is_pc_solving = False
        self.is_closing = False
        self.is_pc_solver_warming = False
        self.pc_solver_warm_ready = False
        self.browser_launch_in_progress = False
        self.browser_action_in_progress = False
        self.browser_action_name = ""
        self.hotkey_pressed = {
            VK_DELETE: False,
            VK_END: False,
        }

        self.always_on_top = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cell_size = 17
        self.board_cols = 10
        self.board_rows = 20
        self.solve_card_cell_size = 7
        self.solve_canvas_width = 292
        self.solve_canvas_height = 320

        self.title_label = tk.Label(
            root,
            text="TETR.IO CDP STATE READER",
            font=("Malgun Gothic", 16, "bold"),
        )
        self.title_label.pack(pady=(12, 4))

        self.button_frame = tk.Frame(root)
        self.button_frame.pack(pady=8)
        for column in range(3):
            self.button_frame.grid_columnconfigure(column, weight=1)

        self.pc_scan_button = tk.Button(
            self.button_frame,
            text="PC 해법 찾기",
            width=15,
            height=2,
            font=("Malgun Gothic", 11, "bold"),
            command=self.on_pc_solver_requested,
        )
        self.pc_scan_button.grid(row=0, column=0, padx=5)

        self.setup_scan_button = tk.Button(
            self.button_frame,
            text="최적 셋업 찾기",
            width=15,
            height=2,
            font=("Malgun Gothic", 11, "bold"),
            command=self.on_setup_requested,
        )
        self.setup_scan_button.grid(row=0, column=1, padx=5)

        self.browser_open_button = tk.Button(
            self.button_frame,
            text="브라우저 열기",
            width=15,
            height=2,
            font=("Malgun Gothic", 11, "bold"),
            command=self.open_tetrio_browser,
        )
        self.browser_open_button.grid(row=0, column=2, padx=5)

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
        self.browser_status_var = tk.StringVar(value="Browser: Disconnected")
        self.game_state_var = tk.StringVar(value="Game state: Waiting")
        self.counter_var = tk.StringVar(value="Piece counter: -")
        self.queue_status_var = tk.StringVar(value="Current/Hold/Queue: -")
        self.age_var = tk.StringVar(value="Last update age: -")
        self.detail_var = tk.StringVar(value="Detail: 브라우저 열기를 눌러주세요")
        self.pc_solver_status_var = tk.StringVar(value="PC SOLVER: 대기 중")
        self.build_state_status_panel()
        self.pc_solver_status_label = tk.Label(
            root,
            textvariable=self.pc_solver_status_var,
            anchor="w",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.pc_solver_status_label.pack(fill="x", padx=16, pady=(0, 6))

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
            font=("Malgun Gothic", 9),
        )
        self.output_hint.pack(fill="x", pady=(8, 0))
        self.bind_output_scroll_events()

        self.status_label = tk.Label(
            root,
            text="브라우저 열기를 눌러주세요",
            font=("Malgun Gothic", 10),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        self.draw_empty_board()
        self.print_message(
            "Del: PC 해법 상태 읽기 / End: 최적 셋업 상태 읽기\n"
            "PC 해법 또는 최적 셋업 카드가 여기에 표시됩니다."
        )
        self.bind_keyboard_shortcuts()
        self.register_global_hotkeys()
        self.root.after(250, self.start_pc_solver_warmup)
        self.root.after(200, self.refresh_browser_status)
        self.last_pc_signature = None
        self.root.update_idletasks()
        required_height = self.root.winfo_reqheight()
        target_height = min(max(required_height + 12, 870), 900)
        self.root.geometry(f"620x{target_height}")

    def bind_keyboard_shortcuts(self):
        self.root.bind("<Delete>", self.on_local_delete_hotkey, add="+")
        self.root.bind("<End>", self.on_local_end_hotkey, add="+")

    def on_local_delete_hotkey(self, _event=None):
        self.on_pc_solver_requested(show_popup=False)
        return "break"

    def on_local_end_hotkey(self, _event=None):
        self.on_setup_requested(show_popup=False)
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
            self.on_pc_solver_requested(show_popup=False)
            return
        if hotkey_id == VK_END:
            self.on_setup_requested(show_popup=False)

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

        font_small = ("Consolas", 7)

        for round_num in range(1, 8):
            group = tk.Frame(self.setup_option_frame, bd=1, relief="solid", padx=2, pady=1)
            group.grid(row=0, column=round_num - 1, padx=1, pady=2, sticky="nsew")
            tk.Label(
                group,
                text=f"{round_num}st PC" if round_num == 1 else f"{round_num}nd PC" if round_num == 2 else f"{round_num}rd PC" if round_num == 3 else f"{round_num}th PC",
                font=("Consolas", 7, "bold"),
            ).grid(row=0, column=0, sticky="ew", pady=(0, 1))

            round_vars = {}
            default = defaults.get(round_num, {})

            if round_num in (1, 2, 3):
                mode_var = tk.StringVar(value=default.get("mode", "Simple"))
                mode_menu = tk.OptionMenu(group, mode_var, "Simple", "Advanced")
                mode_menu.config(font=font_small, width=7, padx=1, pady=0, indicatoron=False)
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
                    font=("Consolas", 6),
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
            font=("Malgun Gothic", 7),
            fg="#6f6a61",
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=3, pady=(0, 1))

    def create_setup_option_value_label(self, parent, variable, row):
        holder = tk.Frame(parent, bd=1, relief="sunken", bg="#fbfbfb")
        holder.grid(row=row, column=0, sticky="ew", pady=(1, 0))
        tk.Label(
            holder,
            textvariable=variable,
            font=("Consolas", 7, "bold"),
            bg="#fbfbfb",
            fg="#2d2d2d",
            justify="center",
            padx=3,
            pady=1,
        ).pack(fill="x")

    def build_state_status_panel(self):
        self.state_frame = tk.LabelFrame(
            self.root,
            text="BROWSER STATE",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.state_frame.pack(fill="x", padx=14, pady=(0, 6))

        variables = (
            self.browser_status_var,
            self.game_state_var,
            self.counter_var,
            self.queue_status_var,
            self.age_var,
            self.detail_var,
        )
        for column in range(2):
            self.state_frame.grid_columnconfigure(column, weight=1)
        for index, variable in enumerate(variables):
            tk.Label(
                self.state_frame,
                textvariable=variable,
                anchor="w",
                justify="left",
                font=("Consolas", 8),
            ).grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=8,
                pady=1,
            )

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

    def get_scan_context(self, solver_result=None):
        result = self.last_result or {}
        active_guess = (result.get("current") or result.get("active_guess") or "").strip().upper()
        active_effective = active_guess or "-"
        hold = (result.get("hold") or "").strip().upper() or "-"
        queue_list = [piece for piece in (result.get("queue") or []) if piece]
        next_text = "".join(queue_list) or "-"

        if solver_result and solver_result.get("state_queue"):
            see_text = str(solver_result.get("state_queue") or "-").strip().upper()
        else:
            try:
                see_text = make_state_queue(
                    "" if active_effective == "-" else active_effective,
                    queue_list,
                )
            except Exception:
                see_text = "-"

        return {
            "current": active_guess or "-",
            "active_guess": active_guess or "-",
            "active_effective": active_effective,
            "hold": hold,
            "next": next_text,
            "state": see_text or "-",
        }

    def draw_output_context_header(self, solver_result=None):
        if not self.last_result:
            return 0

        context = self.get_scan_context(solver_result=solver_result)
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
                f"NEXT {context['next']}"
            ),
            anchor="w",
            fill="#252525",
            font=("Consolas", 10, "bold"),
        )
        self.output.create_text(
            x + 10,
            y + 32,
            text=f"STATE {context['state']}",
            anchor="w",
            fill="#7a766d",
            font=("Consolas", 9, "bold"),
        )
        return y + height + 8

    def set_scan_buttons_state(self, state):
        self.pc_scan_button.config(state=state)
        self.setup_scan_button.config(state=state)

    def update_action_buttons_state(self):
        scan_state = (
            "disabled"
            if self.is_scanning
            or self.is_pc_solving
            or self.browser_launch_in_progress
            or self.browser_action_in_progress
            else "normal"
        )
        browser_state = "disabled" if self.browser_launch_in_progress or self.is_closing else "normal"
        self.set_scan_buttons_state(scan_state)
        self.browser_open_button.config(state=browser_state)

    def _log_cdp_action(self, message):
        print(f"[CDP ACTION] {message}")

    def on_pc_solver_requested(self, show_popup=True):
        self.run_action_with_browser_source(
            "pc-solver",
            self.execute_pc_solver,
            show_popup=show_popup,
        )

    def on_setup_requested(self, show_popup=True):
        self.run_action_with_browser_source(
            "setup-solver",
            self.execute_setup_solver,
            show_popup=show_popup,
        )

    def scan_for_pc_solve(self, show_popup=True):
        self.on_pc_solver_requested(show_popup=show_popup)

    def scan_for_setup_finder(self, show_popup=True):
        self.on_setup_requested(show_popup=show_popup)

    def schedule_auto_scan(self, delay_ms=None):
        if not self.auto_scan_enabled:
            return

        if self.auto_scan_job is not None:
            self.root.after_cancel(self.auto_scan_job)

        wait_ms = self.auto_scan_interval_ms if delay_ms is None else delay_ms
        self.auto_scan_job = self.root.after(wait_ms, self.auto_scan_tick)

    def auto_scan_tick(self):
        self.auto_scan_job = None
        self.on_pc_solver_requested(show_popup=False)

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
        self.state_source.close()
        self.root.destroy()

    def reload_config(self):
        try:
            self.config = load_config("config.json")
            self.state_source.reload_config()
            self.status_label.config(text="CDP 설정 다시 읽음")
        except Exception as exc:
            messagebox.showerror("설정 오류", str(exc))

    def _set_browser_detail(self, message):
        self.detail_var.set(f"Detail: {message}")

    def _get_browser_launch_popen_kwargs(self):
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        return {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "creationflags": creationflags,
        }

    def _ensure_browser_reader_started(self):
        self.state_source.ensure_connected(timeout_sec=BROWSER_READY_TIMEOUT_SEC)

    def ensure_browser_source_for_action(self, show_popup=True):
        port = get_cdp_port(self.config)
        if not is_cdp_endpoint_available(port, timeout_sec=CDP_ACTION_PORT_TIMEOUT_SEC):
            message = "브라우저 열기를 먼저 눌러주세요."
            self.status_label.config(text=message)
            self._set_browser_detail(message)
            self.state_source.mark_browser_closed(message)
            return False

        try:
            self._ensure_browser_reader_started()
        except Exception as exc:
            self.status_label.config(text="브라우저 연결 실패")
            self._set_browser_detail(str(exc))
            if show_popup:
                messagebox.showerror("브라우저 연결 오류", str(exc))
            return False
        return True

    def run_action_with_browser_source(self, action_name, callback, show_popup=True):
        if self.is_closing or self.browser_action_in_progress or self.is_scanning or self.is_pc_solving:
            return

        self.browser_action_in_progress = True
        self.browser_action_name = action_name
        self.update_action_buttons_state()
        self.status_label.config(text="브라우저 상태 연결 중...")
        self._set_browser_detail("브라우저 상태 연결 중...")
        action_started_at_ms = int(time.time() * 1000)

        worker = threading.Thread(
            target=self._run_action_with_browser_source_worker,
            args=(action_name, callback, show_popup, action_started_at_ms),
            daemon=True,
        )
        worker.start()

    def _run_action_with_browser_source_worker(
        self,
        action_name,
        callback,
        show_popup,
        action_started_at_ms,
    ):
        port = get_cdp_port(self.config)
        cdp_available = is_cdp_endpoint_available(
            port,
            timeout_sec=CDP_ACTION_PORT_TIMEOUT_SEC,
        )
        self._log_cdp_action(f"action={action_name}")
        self._log_cdp_action(f"cdp_available={str(cdp_available).lower()}")
        if not cdp_available:
            self.state_source.mark_browser_closed("브라우저 열기를 먼저 눌러주세요.")
            self._post_to_ui(
                self._on_browser_action_browser_missing,
                show_popup,
            )
            return

        self._log_cdp_action(
            f"reader_alive={str(self.state_source.is_reader_alive()).lower()}"
        )
        try:
            result = self.state_source.prepare_result_for_action(
                action_name,
                action_started_at_ms=action_started_at_ms,
                timeout_sec=CDP_ACTION_SNAPSHOT_TIMEOUT_SEC,
            )
            self._post_to_ui(
                self._on_browser_action_success,
                callback,
                result,
            )
        except Exception as exc:
            self._post_to_ui(
                self._on_browser_action_error,
                str(exc),
                show_popup,
            )

    def _finish_browser_action(self):
        self.browser_action_in_progress = False
        self.browser_action_name = ""
        if not self.is_closing:
            self.update_action_buttons_state()

    def _on_browser_action_browser_missing(self, show_popup):
        self._finish_browser_action()
        message = "브라우저 열기를 먼저 눌러주세요."
        self.status_label.config(text=message)
        self._set_browser_detail(message)
        if show_popup:
            messagebox.showwarning("브라우저 연결", message)

    def _on_browser_action_success(self, callback, result):
        self._finish_browser_action()
        self.status_label.config(text="브라우저 연결됨")
        self._set_browser_detail("브라우저 연결됨")
        callback(result)

    def _on_browser_action_error(self, error_text, show_popup):
        self._finish_browser_action()
        status_text = (
            "TETR.IO 게임 상태를 읽지 못했습니다."
            if error_text == "TETR.IO 게임 상태를 읽지 못했습니다."
            else "브라우저 상태 연결에 실패했습니다."
        )
        self.status_label.config(text=status_text)
        self._set_browser_detail(error_text)
        if show_popup:
            messagebox.showerror("브라우저 상태 오류", error_text)

    def open_tetrio_browser(self):
        if self.is_closing:
            return
        if self.browser_launch_in_progress:
            self.status_label.config(text="브라우저 준비 중...")
            return
        self.browser_launch_in_progress = True
        self.update_action_buttons_state()
        self.status_label.config(text="브라우저 준비 중...")
        self._set_browser_detail("브라우저 준비 중...")
        worker = threading.Thread(target=self._open_tetrio_browser_worker, daemon=True)
        worker.start()

    def _open_tetrio_browser_worker(self):
        port = get_cdp_port(self.config)
        try:
            already_open = is_cdp_endpoint_available(port)
            if not already_open:
                resolved = resolve_browser_executable(self.config)
                executable = resolved.get("executable")
                if not executable:
                    raise RuntimeError("사용 가능한 Chrome 또는 Edge 브라우저를 찾지 못했습니다.")
                command = build_browser_launch_command(
                    self.config,
                    browser_executable=executable,
                    profile_dir=get_browser_profile_path(self.config),
                )
                try:
                    subprocess.Popen(command, **self._get_browser_launch_popen_kwargs())
                except Exception as exc:
                    attempted_text = " | ".join(resolved.get("attempted_paths") or [executable])
                    raise RuntimeError(
                        f"브라우저 실행 실패\n"
                        f"시도 경로: {attempted_text}\n"
                        f"spawn error: {exc}"
                    ) from exc
                if not wait_for_cdp_endpoint(port):
                    raise RuntimeError(f"CDP 준비 확인 실패: http://127.0.0.1:{port}/json/version")

            self._ensure_browser_reader_started()
            self.state_source.wait_until_connected(timeout_sec=BROWSER_READY_TIMEOUT_SEC)
            self._post_to_ui(self._on_browser_open_success)
        except Exception as exc:
            self._post_to_ui(self._on_browser_open_error, str(exc))
        finally:
            self._post_to_ui(self._on_browser_open_finished)

    def _on_browser_open_success(self):
        self.status_label.config(text="브라우저 연결됨")
        self._set_browser_detail("브라우저 연결됨")

    def _on_browser_open_error(self, error_text):
        self.status_label.config(text="브라우저 연결 실패")
        self._set_browser_detail(error_text)
        messagebox.showerror("브라우저 연결 오류", error_text)

    def _on_browser_open_finished(self):
        self.browser_launch_in_progress = False
        self.update_action_buttons_state()
        if self.state_status_job is not None:
            self.root.after_cancel(self.state_status_job)
            self.state_status_job = None
        self.refresh_browser_status()

    def refresh_browser_status(self):
        self.state_status_job = None
        if self.is_closing:
            return

        try:
            status = self.state_source.get_status(allow_start=False)
        except Exception as exc:
            status = {
                "browser_status": "Error",
                "game_state": "Waiting",
                "piece_counter": None,
                "current": "-",
                "hold": "-",
                "queue": "-",
                "last_update_age_ms": None,
                "detail": str(exc),
            }
        port = get_cdp_port(self.config)
        cdp_open = is_cdp_endpoint_available(port)
        age_ms = status.get("last_update_age_ms")
        age_text = "-" if age_ms is None else f"{age_ms}ms"
        queue_text = status.get("queue") or "-"
        browser_status = status.get("browser_status", "Unknown")
        detail_text = status.get("detail", "-")

        if browser_status == "Disconnected":
            if cdp_open:
                browser_status = "Open"
                detail_text = "브라우저가 열려 있습니다. 브라우저 열기를 눌러 연결하세요."
            elif "브라우저 연결 끊김" not in detail_text and "exit code" not in detail_text:
                detail_text = "브라우저 열기를 눌러주세요"

        self.browser_status_var.set(f"Browser: {browser_status}")
        self.game_state_var.set(f"Game state: {status.get('game_state', 'Unknown')}")
        self.counter_var.set(f"Piece counter: {status.get('piece_counter', '-')}")
        self.queue_status_var.set(
            f"Current/Hold/Queue: {status.get('current', '-')} / {status.get('hold', '-')} / {queue_text}"
        )
        self.age_var.set(f"Last update age: {age_text}")
        self.detail_var.set(f"Detail: {detail_text}")

        self.state_status_job = self.root.after(500, self.refresh_browser_status)

    def start_pc_solver_warmup(self):
        if self.is_closing or self.is_pc_solver_warming or self.pc_solver_warm_ready:
            return

        self.is_pc_solver_warming = True
        self.pc_solver_status_var.set("PC SOLVER: 예열 중")
        worker = threading.Thread(target=self._pc_solver_warmup_worker, daemon=True)
        worker.start()

    def execute_pc_solver(self, result):
        self._on_scan_success(result, "pc_solve")

    def execute_setup_solver(self, result):
        self._on_scan_success(result, "setup")

    def scan(self, scan_target="setup", show_popup=True):
        if self.is_scanning or self.is_closing:
            return

        self.is_scanning = True
        if scan_target == "pc_solve":
            self.status_label.config(text="PC 해법용 게임 상태 읽는 중...")
        else:
            self.status_label.config(text="최적 셋업용 게임 상태 읽는 중...")
        self.update_action_buttons_state()

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
        failure_reason = result.get("pc_failure_reason") or "pieceCounter/linesCleared 없음"

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
                    "failure_reason": None,
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
                    "failure_reason": None,
                }
            except (TypeError, ValueError):
                pass

        return {
            "pieces_count": None,
            "cycle_pieces": None,
            "pc_round": None,
            "pc_progress": None,
            "bag_in_cycle": None,
            "structure": "-",
            "source": "unknown",
            "failure_reason": failure_reason,
        }


    def format_pc_round_info(self, result):
        info = self.get_pc_round_info(result)

        if info["pc_round"] is None:
            return f"현재 PC 인식 실패: {info.get('failure_reason') or 'pieceCounter/linesCleared 없음'}"

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

    def run_pc_solver_now(self, show_popup=True, force=True):
        if self.is_pc_solving or self.is_closing:
            return

        if not self.last_result:
            if show_popup:
                messagebox.showwarning("PC Solver", "먼저 게임 상태를 읽어야 합니다.")
            return

        board = self.last_result["board"]
        active_piece = (
            self.last_result.get("current")
            or self.last_result.get("active_guess")
            or ""
        ).strip().upper()
        hold = (self.last_result.get("hold") or "").strip().upper()
        queue = self.last_result["queue"]

        if not active_piece:
            self.pc_solver_status_var.set("PC SOLVER: Current piece 대기 중")
            self.status_label.config(text="PC 해법 대기 - current 없음")
            self.print_message("current piece가 준비되면 PC 해법 계산이 시작됩니다.")
            return

        pc_signature = (
            tuple("".join(row) for row in board),
            active_piece,
            hold,
            tuple(queue),
        )
        if not force and not show_popup and pc_signature == self.last_pc_signature:
            self.status_label.config(text="PC 해법 계산 생략 - 같은 상태")
            return

        self.last_pc_signature = pc_signature
        self.is_pc_solving = True
        self.update_action_buttons_state()

        if show_popup or self.current_output_mode != "setup":
            self.clear_output_view("PC 해법 계산 중...")
        else:
            self.output_hint_var.set("추천 셋업 표시 중\nPC 해법 계산 중...")

        self.pc_solver_status_var.set("PC SOLVER: 계산 중")
        self.status_label.config(text="PC 해법 계산 중...")
        print("[PC SOLVER] scan success")

        worker = threading.Thread(
            target=self._run_pc_solver_worker,
            args=(board, active_piece, hold, queue, show_popup),
            daemon=True,
        )
        worker.start()

    def render_pc_solution_groups(self, variants, solver_result=None):
        self.output.delete("all")
        self.current_output_mode = "pc_solve"

        card_width = 260
        card_height = 98
        gap_y = 8
        start_x = 8
        start_y = 8
        visible = variants[:6]
        for index, variant in enumerate(visible):
            y = start_y + index * (card_height + gap_y)
            self.draw_pc_solution_card_minimal(start_x, y, card_width, card_height, variant)

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

        if hold_start:
            badge_x1 = x + 34
            badge_y1 = y + 6
            badge_x2 = badge_x1 + 58
            badge_y2 = badge_y1 + 16
            self.output.create_rectangle(
                badge_x1,
                badge_y1,
                badge_x2,
                badge_y2,
                fill="#e8dfc9",
                outline="#cdbf9d",
            )
            self.output.create_text(
                (badge_x1 + badge_x2) / 2,
                (badge_y1 + badge_y2) / 2 + 0.5,
                text="HOLD 시작",
                fill="#6a5430",
                font=("Malgun Gothic", 7, "bold"),
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

    def build_pc_variant(self, index, title, solution, queue_text):
        rows = self.decode_gomen_cells(solution.get("cells", ""))
        queue_value = str(solution.get("queue_text") or queue_text or "").strip().upper()
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
        return self.build_pc_solver_variants_minimal(solver_result, active=active, hold=hold)

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
                variant["state_queue"] = str(solution.get("state_queue") or state_queue).strip().upper()
                variant["branch_name"] = str(solution.get("branch_name") or "")
                variant["matched_group"] = solution.get("matched_group") or ""
                variants.append(variant)

        return variants

    def draw_pc_solution_card_exact(self, x, y, width, height, variant):
        title = variant.get("title") or "PC 해법"
        preview_rows = variant.get("preview_rows") or []
        hold_start = bool(variant.get("hold_start"))

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


    def build_pc_variant_exact(self, index, title, solution, next_text):
        rows = self.decode_gomen_cells(solution.get("cells", ""))
        placements = [str(piece or "").strip().upper() for piece in (solution.get("placements") or []) if piece]
        hold_actions = [bool(value) for value in (solution.get("hold_actions") or [])]
        if not placements:
            return None

        first_hold = bool(hold_actions[0]) if hold_actions else False
        first_move_text = f"첫 수: HOLD → {first_piece} 사용" if first_hold else f"첫 수: {first_piece} 그대로 사용"
        return {
            "title": title if title.startswith(f"{index}. ") else f"{index}. {title}",
            "preview_rows": rows,
            "next_text": str(next_text or "").strip().upper(),
            "placements_text": "".join(placements),
            "first_move_text": first_move_text,
            "final_hold": (str(solution.get("final_hold") or "").strip().upper() or "-"),
            "solution": solution,
        }

    def build_pc_solver_variants_exact(self, solver_result, active="", hold=""):
        variants = []
        solutions = solver_result.get("solutions") or []
        next_text = "".join(piece for piece in (self.last_result or {}).get("queue", []) if piece)
        state_queue = str(solver_result.get("state_queue") or "").strip().upper()

        for index, solution in enumerate(solutions[:6], start=1):
            variant = self.build_pc_variant_exact(
                index=index,
                title="PC 해법",
                solution=solution,
                next_text=next_text,
            )
            if not variant:
                continue
            variant["state_queue"] = str(solution.get("state_queue") or state_queue).strip().upper()
            variant["branch_name"] = str(solution.get("branch_name") or "")
            variant["matched_group"] = solution.get("matched_group") or ""
            variants.append(variant)

        return variants

    def draw_pc_solution_card_minimal(self, x, y, width, height, variant):
        title = variant.get("title") or "1."
        preview_rows = variant.get("preview_rows") or []
        hold_start = bool(variant.get("hold_start"))

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

        if hold_start:
            badge_x1 = x + 34
            badge_y1 = y + 6
            badge_x2 = badge_x1 + 58
            badge_y2 = badge_y1 + 16
            self.output.create_rectangle(
                badge_x1,
                badge_y1,
                badge_x2,
                badge_y2,
                fill="#e8dfc9",
                outline="#cdbf9d",
            )
            self.output.create_text(
                (badge_x1 + badge_x2) / 2,
                (badge_y1 + badge_y2) / 2 + 0.5,
                text="HOLD 시작",
                fill="#6a5430",
                font=("Malgun Gothic", 7, "bold"),
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

    def build_pc_variant_minimal(self, index, solution):
        rows = self.decode_gomen_cells(solution.get("cells", ""))
        placements = [str(piece or "").strip().upper() for piece in (solution.get("placements") or []) if piece]
        hold_actions = [bool(value) for value in (solution.get("hold_actions") or [])]
        if not placements:
            return None

        return {
            "title": f"{index}.",
            "preview_rows": rows,
            "hold_start": bool(hold_actions[0]) if hold_actions else False,
            "solution": solution,
        }

    def build_pc_solver_variants_minimal(self, solver_result, active="", hold=""):
        variants = []
        solutions = solver_result.get("solutions") or []
        state_queue = str(solver_result.get("state_queue") or "").strip().upper()

        for index, solution in enumerate(solutions[:6], start=1):
            variant = self.build_pc_variant_minimal(index=index, solution=solution)
            if not variant:
                continue
            variant["state_queue"] = str(solution.get("state_queue") or state_queue).strip().upper()
            variant["branch_name"] = str(solution.get("branch_name") or "")
            variant["matched_group"] = solution.get("matched_group") or ""
            variants.append(variant)

        return variants

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
        active = (result.get("current") or result.get("active_guess") or "").strip().upper()
        hold = (result.get("hold") or "").strip().upper()
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
            print("[SETUP] skipped reason=pc_round_unavailable")
            print(
                "[SETUP]",
                f"pieces_count={result.get('pieces_count')}",
                f"linesCleared={result.get('lines_cleared')}",
                f"fixedCells={result.get('fixed_cells')}",
                f"stateRevision={result.get('state_revision')}",
            )
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
                if cell != ".":
                    count += 1
        return count


    def should_show_setup_recommendation(self, result):
        board = result.get("board") or []
        current = (result.get("current") or result.get("active_guess") or "").strip().upper()
        if not current:
            return False

        round_info = self.get_pc_round_info(result)
        fixed_cells = result.get("fixed_cells")
        if fixed_cells is None:
            fixed_cells = self.count_visible_cells(board)

        if fixed_cells > 4:
            return False
        if round_info["pc_round"] != 1:
            return False
        if round_info["pc_progress"] is None:
            return False
        return round_info["pc_progress"] <= 1

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

    def _log_state_result(self, result):
        print(
            "[STATE RESULT]",
            "pieceCounter=", result.get("piece_counter"),
            "pieceCounterSource=", result.get("piece_counter_source"),
            "linesCleared=", result.get("lines_cleared"),
            "derivedPlacedPieces=", result.get("derived_placed_pieces"),
            "stateRevision=", result.get("state_revision"),
            "fixedCells=", result.get("fixed_cells"),
            "pieceProgressSource=", result.get("piece_progress_source"),
            "pieces_count=", result.get("pieces_count"),
            "pc_round=", result.get("pc_round"),
            "current=", result.get("current"),
            "hold=", result.get("hold"),
            "queue=", result.get("queue"),
        )

    def _scan_worker(self, scan_target, show_popup):
        try:
            print("[STATE] read start")
            self.state_source.wait_until_connected(timeout_sec=5.0)
            result = self.state_source.get_latest_result(allow_start=False)
            if result is None:
                detail = self.state_source.get_status(allow_start=False).get("detail") or "Waiting for game state"
                raise RuntimeError(detail)

            self._log_state_result(result)

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

        self._log_state_result(result)
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
            self.update_action_buttons_state()
            if self.auto_scan_enabled:
                self.schedule_auto_scan()

    def _run_pc_solver_worker(self, board, active_piece, hold, queue, show_popup):
        try:
            queue_text = "".join(piece for piece in (queue or []) if piece)
            print("[PC SOLVER] worker start")
            print(
                f"[PC SOLVER] request active={active_piece or '-'} "
                f"hold={hold or '-'} queue={queue_text or '-'}"
            )
            result = run_gomen_solver(
                board=board,
                active=active_piece,
                hold=hold,
                queue=queue,
                manual_see="",
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
            print(
                f"[PC SOLVER] response ok={bool(result.get('ok'))} "
                f"total={result.get('total')} shown_total={result.get('shown_total')} "
                f"solutions={len(result.get('solutions') or [])}"
            )
            print(f"[PC SOLVER] render variants={len(result.get('variants') or [])}")
            self._post_to_ui(self._on_pc_solver_success, result)
        except GomenError as exc:
            print(f"[PC SOLVER ERROR] {exc}")
            traceback.print_exc()
            self._post_to_ui(self._on_pc_solver_error, str(exc), show_popup)
        except Exception as exc:
            print(f"[PC SOLVER ERROR] {exc}")
            traceback.print_exc()
            self._post_to_ui(self._on_pc_solver_error, str(exc), show_popup)
        finally:
            print("[PC SOLVER] worker finished")
            self._post_to_ui(self._on_pc_solver_finished)

    def _on_pc_solver_success(self, result):
        variants = result.get("variants") or []
        raw_total = int(result.get("total") or 0)
        shown_total = int(result.get("shown_total") or raw_total or 0)
        solutions = result.get("solutions") or []
        visible_total = len(variants[:6])
        if not variants and not solutions and result.get("display_message"):
            message = str(result.get("display_message"))
            self.pc_solver_status_var.set(message)
            self.print_message(message)
            self.status_label.config(text="PC 솔버 유효 해법 없음")
            return
        print(
            f"[PC SOLVER] success raw_total={raw_total} "
            f"shown_total={shown_total} solutions={len(solutions)} variants={len(variants)}"
        )

        if variants:
            self.pc_solver_status_var.set(f"PC SOLVER: 해법 {visible_total}개")
            self.render_pc_solution_groups(variants, solver_result=result)
            self.status_label.config(text="PC 해법 계산 완료")
            return

        if solutions:
            self.pc_solver_status_var.set("PC SOLVER 오류: 카드 변환 실패")
            self.print_message("PC 해법은 찾았지만 카드 변환에 실패했습니다.")
            self.status_label.config(text="PC 해법 카드 변환 실패")
            return

        self.pc_solver_status_var.set("PC SOLVER: 해법 없음")
        self.print_message("PC 해법 없음")
        self.status_label.config(text="PC 해법 없음")

    def _on_pc_solver_error(self, error_text, show_popup):
        headline = error_text.splitlines()[0] if error_text else "알 수 없는 오류"
        self.pc_solver_status_var.set(f"PC SOLVER 오류: {headline}")
        self.status_label.config(text="PC 해법 계산 실패")
        self.print_message("PC 해법 계산에 실패했습니다.")

        if show_popup:
            messagebox.showerror("PC Solver 오류", error_text)

    def _on_pc_solver_finished(self):
        self.is_pc_solving = False
        if not self.is_closing:
            self.update_action_buttons_state()

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

    def _pc_solver_warmup_worker(self):
        try:
            warm_gomen_session(timeout_sec=30)
            self._post_to_ui(self._on_pc_solver_warmup_success)
        except Exception as exc:
            print(f"[PC SOLVER ERROR] warmup failed: {exc}")
            traceback.print_exc()
            self._post_to_ui(self._on_pc_solver_warmup_error, str(exc))

    def _on_pc_solver_warmup_success(self):
        self.pc_solver_warm_ready = True
        self.pc_solver_status_var.set("PC SOLVER: 대기 중")
        self._on_pc_solver_warmup_finished()

    def _on_pc_solver_warmup_error(self, error_text):
        headline = (error_text or "알 수 없는 오류").splitlines()[0]
        self.pc_solver_warm_ready = False
        self.pc_solver_status_var.set(f"PC SOLVER 오류: {headline}")
        self._on_pc_solver_warmup_finished()

    def _on_pc_solver_warmup_finished(self):
        self.is_pc_solver_warming = False


if __name__ == "__main__":
    ensure_runtime_file("config.json")
    root = tk.Tk()
    app = TetrisScannerApp(root)
    root.mainloop()
