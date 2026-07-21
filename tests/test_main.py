import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import main


class FakeStateSource:
    def __init__(self, _config_path):
        self.started = 0
        self.closed = 0
        self.running = False
        self.wait_calls = 0
        self.ensure_connected_calls = 0
        self.prepare_calls = []
        self.latest_result_calls = []
        self.latest_result = None
        self.status = {
            "browser_status": "Disconnected",
            "game_state": "Waiting",
            "piece_counter": None,
            "current": "-",
            "hold": "-",
            "queue": "-",
            "last_update_age_ms": None,
            "detail": "브라우저 열기를 눌러주세요",
        }

    def get_status(self, allow_start=False):
        self.last_allow_start = allow_start
        return dict(self.status)

    def get_latest_result(self, allow_start=False):
        self.latest_result_calls.append(allow_start)
        return self.latest_result

    def start(self):
        self.started += 1
        self.running = True

    def is_running(self):
        return self.running

    def is_process_alive(self):
        return self.running

    def is_reader_alive(self):
        return self.running

    def ensure_connected(self, timeout_sec=10):
        self.ensure_connected_calls += 1
        if not self.running:
            self.started += 1
            self.running = True
        return True

    def wait_until_connected(self, timeout_sec=10):
        self.wait_calls += 1
        return True

    def prepare_result_for_action(self, action_name, *, action_started_at_ms, timeout_sec=8.0):
        self.prepare_calls.append(
            {
                "action_name": action_name,
                "action_started_at_ms": action_started_at_ms,
                "timeout_sec": timeout_sec,
            }
        )
        return self.latest_result

    def mark_browser_closed(self, reason="브라우저 열기를 먼저 눌러주세요."):
        self.status["detail"] = reason
        self.running = False

    def close(self):
        self.closed += 1
        self.running = False
        return None

    def reload_config(self):
        return None


def make_config():
    return {
        "tetrio_cdp": {
            "port": 9222,
            "url": "https://tetr.io/",
        },
        "pc1_mode": "Simple",
        "pc2_mode": "Advanced",
        "pc_queue_overrides": {
            "1": "LITZS",
            "2": "TLSOZJI",
        },
        "setup_options": {
            "allow_3p": True,
            "allow_4p": True,
            "allow_bd": True,
            "specific_sol": True,
        },
    }


def make_result(current="T", active_guess="Z", hold="I", queue=None):
    return {
        "board": [["." for _ in range(10)] for _ in range(20)],
        "current": current,
        "active_guess": active_guess,
        "hold": hold,
        "queue": list(queue or ["L", "S", "Z", "O", "J"]),
        "pieces_count": 4,
        "pc_round": 1,
        "piece_counter": 4,
        "piece_counter_source": "stats.piecesPlaced",
        "lines_cleared": 0,
        "derived_placed_pieces": None,
        "fixed_cells": 0,
        "piece_progress_source": "piece-counter",
        "pc_failure_reason": None,
        "state_revision": 4,
    }


class FakeThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class MainHelperTests(unittest.TestCase):
    def test_resolve_browser_executable_prefers_bundled_then_config_then_chrome_then_edge(self):
        config = {"tetrio_cdp": {"browser_path": r"D:\custom\browser.exe"}}
        bundled = str(main.get_resource_path("runtime", "chromium", "chrome.exe"))

        resolved = main.resolve_browser_executable(
            config=config,
            env={"LOCALAPPDATA": r"C:\Users\MSI\AppData\Local"},
            exists_fn=lambda candidate: str(candidate) == bundled,
        )

        self.assertEqual(resolved["executable"], bundled)
        self.assertEqual(resolved["attempted_paths"], [bundled])

    def test_build_browser_launch_command_uses_cdp_port_profile_and_url(self):
        config = make_config()
        profile_dir = Path(r"C:\Users\MSI\AppData\Local\TetrioPcHelper\browser-profile")

        command = main.build_browser_launch_command(
            config,
            browser_executable=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            profile_dir=profile_dir,
        )

        self.assertEqual(command[0], r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertIn("--remote-debugging-port=9222", command)
        self.assertIn(f"--user-data-dir={profile_dir}", command)
        self.assertIn("--no-first-run", command)
        self.assertIn("--no-default-browser-check", command)
        self.assertIn("--new-window", command)
        self.assertEqual(command[-1], "https://tetr.io/")

    def test_build_browser_launch_command_raises_clear_error_when_browser_missing(self):
        with mock.patch.object(main, "resolve_browser_executable", return_value={"executable": None, "attempted_paths": []}):
            with self.assertRaisesRegex(RuntimeError, "사용 가능한 Chrome 또는 Edge 브라우저를 찾지 못했습니다."):
                main.build_browser_launch_command(make_config())

    def test_wait_for_cdp_endpoint_polls_until_ready(self):
        calls = []

        def probe(_port):
            calls.append(True)
            return len(calls) >= 3

        self.assertTrue(main.wait_for_cdp_endpoint(9222, timeout_sec=1.0, poll_interval_sec=0.01, probe_fn=probe))
        self.assertEqual(len(calls), 3)


class MainUiTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            mock.patch.object(main, "load_config", return_value=make_config()),
            mock.patch.object(main, "TetrioStateSource", FakeStateSource),
            mock.patch.object(main.TetrisScannerApp, "register_global_hotkeys", autospec=True, return_value=None),
            mock.patch.object(main.TetrisScannerApp, "start_pc_solver_warmup", autospec=True, return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()

        self.root = tk.Tk()
        self.app = main.TetrisScannerApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self.app.is_closing = True
            for after_id in self.root.tk.call("after", "info"):
                self.root.after_cancel(after_id)
            self.root.destroy()
        except Exception:
            pass
        for patcher in reversed(self.patchers):
            patcher.stop()

    def get_canvas_texts(self):
        texts = []
        for item_id in self.app.output.find_all():
            if self.app.output.type(item_id) == "text":
                texts.append(self.app.output.itemcget(item_id, "text"))
        return texts

    def test_app_start_does_not_auto_start_browser_or_helper(self):
        self.assertEqual(self.app.state_source.started, 0)
        self.assertFalse(self.app.browser_launch_in_progress)
        self.assertEqual(self.app.status_label.cget("text"), "브라우저 열기를 눌러주세요")
        self.assertEqual(self.app.detail_var.get(), "Detail: 브라우저 열기를 눌러주세요")

    def test_top_row_contains_only_three_centered_action_buttons(self):
        children = self.app.button_frame.winfo_children()
        texts = [child.cget("text") for child in children]
        self.assertEqual(texts, ["PC 해법 찾기", "최적 셋업 찾기", "브라우저 열기"])
        self.assertFalse(hasattr(self.app, "reload_button"))
        self.assertFalse(hasattr(self.app, "connect_button"))

    def test_browser_state_panel_is_slimmed_down(self):
        self.assertEqual(len(self.app.state_frame.winfo_children()), 6)
        values = [
            self.app.browser_status_var.get(),
            self.app.game_state_var.get(),
            self.app.counter_var.get(),
            self.app.queue_status_var.get(),
            self.app.age_var.get(),
            self.app.detail_var.get(),
        ]
        self.assertTrue(all(value for value in values))

    def test_manual_setup_options_frame_and_widgets_are_removed(self):
        self.assertFalse(hasattr(self.app, "setup_option_frame"))
        all_texts = []
        pending = [self.root]
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            try:
                all_texts.append(widget.cget("text"))
            except tk.TclError:
                continue

        joined = "\n".join(text for text in all_texts if text)
        self.assertNotIn("SETUP OPTIONS", joined)
        self.assertNotIn("1st PC", joined)
        self.assertNotIn("7th PC", joined)
        self.assertNotIn("Simple", joined)
        self.assertNotIn("Advanced", joined)
        self.assertNotIn("3P", joined)
        self.assertNotIn("4P", joined)
        self.assertNotIn("BD", joined)
        self.assertNotIn("큐는 최신 CDP snapshot 값을 자동 사용합니다.", joined)

    def test_manual_setup_state_members_and_helpers_are_removed(self):
        self.assertFalse(hasattr(self.app, "setup_option_refresh_job"))
        self.assertFalse(hasattr(self.app, "setup_option_vars"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "get_default_setup_option_config"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "build_setup_option_controls"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "create_setup_option_value_label"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "get_setup_options_by_round"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "on_setup_option_changed"))
        self.assertFalse(hasattr(main.TetrisScannerApp, "refresh_setup_options_preview"))

    def test_legacy_manual_setup_config_keys_are_ignored(self):
        self.assertEqual(self.app.config["pc1_mode"], "Simple")
        self.assertEqual(self.app.config["pc2_mode"], "Advanced")
        self.assertIn("pc_queue_overrides", self.app.config)
        self.assertIn("setup_options", self.app.config)
        self.assertEqual(self.app.setup_scan_button.cget("text"), "최적 셋업 찾기")
        self.assertEqual(self.app.pc_scan_button.cget("text"), "PC 해법 찾기")

    def test_window_height_is_reduced_without_clipping(self):
        geometry = self.root.geometry().split("+")[0]
        width_text, height_text = geometry.split("x")
        actual_width = int(width_text)
        actual_height = int(height_text)

        self.assertEqual(actual_width, 620)
        self.assertLess(actual_height, 870)
        self.assertGreaterEqual(actual_height, self.root.winfo_reqheight())

    def test_pc_button_routes_through_common_browser_action(self):
        with mock.patch.object(self.app, "run_action_with_browser_source") as action_mock:
            self.app.on_pc_solver_requested(show_popup=False)

        action_mock.assert_called_once()
        self.assertEqual(action_mock.call_args.args[0], "pc-solver")
        self.assertEqual(action_mock.call_args.args[1], self.app.execute_pc_solver)
        self.assertEqual(action_mock.call_args.kwargs["show_popup"], False)

    def test_setup_button_routes_through_common_browser_action(self):
        with mock.patch.object(self.app, "run_action_with_browser_source") as action_mock:
            self.app.on_setup_requested(show_popup=False)

        action_mock.assert_called_once()
        self.assertEqual(action_mock.call_args.args[0], "setup-solver")
        self.assertEqual(action_mock.call_args.args[1], self.app.execute_setup_solver)
        self.assertEqual(action_mock.call_args.kwargs["show_popup"], False)

    def test_delete_hotkey_uses_common_pc_request_path(self):
        with mock.patch.object(self.app, "on_pc_solver_requested") as request_mock:
            result = self.app.on_local_delete_hotkey()

        request_mock.assert_called_once_with(show_popup=False)
        self.assertEqual(result, "break")

    def test_end_hotkey_uses_common_setup_request_path(self):
        with mock.patch.object(self.app, "on_setup_requested") as request_mock:
            result = self.app.on_local_end_hotkey()

        request_mock.assert_called_once_with(show_popup=False)
        self.assertEqual(result, "break")

    def test_open_browser_worker_uses_existing_cdp_without_relaunch(self):
        self.app._post_to_ui = lambda callback, *args: callback(*args)

        with (
            mock.patch.object(main, "is_cdp_endpoint_available", return_value=True),
            mock.patch("subprocess.Popen") as popen_mock,
        ):
            self.app._open_tetrio_browser_worker()

        popen_mock.assert_not_called()
        self.assertEqual(self.app.state_source.started, 1)
        self.assertEqual(self.app.status_label.cget("text"), "브라우저 연결됨")

    def test_open_browser_worker_does_not_duplicate_reader_when_already_running(self):
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        self.app.state_source.running = True

        with (
            mock.patch.object(main, "is_cdp_endpoint_available", return_value=True),
            mock.patch("subprocess.Popen") as popen_mock,
        ):
            self.app._open_tetrio_browser_worker()

        popen_mock.assert_not_called()
        self.assertEqual(self.app.state_source.started, 0)

    def test_browser_open_button_spawns_single_worker_while_in_progress(self):
        fake_thread = FakeThread()

        with mock.patch.object(main.threading, "Thread", return_value=fake_thread) as thread_mock:
            self.app.open_tetrio_browser()
            self.app.open_tetrio_browser()

        self.assertEqual(thread_mock.call_count, 1)
        self.assertTrue(fake_thread.started)
        self.assertEqual(self.app.browser_open_button.cget("state"), "disabled")

    def test_browser_open_failure_surfaces_gui_error(self):
        with mock.patch.object(main.messagebox, "showerror") as error_mock:
            self.app._on_browser_open_error("사용 가능한 Chrome 또는 Edge 브라우저를 찾지 못했습니다.")

        error_mock.assert_called_once()
        self.assertEqual(self.app.status_label.cget("text"), "브라우저 연결 실패")
        self.assertIn("Chrome 또는 Edge", self.app.detail_var.get())

    def test_refresh_browser_status_marks_existing_cdp_as_open(self):
        with mock.patch.object(main, "is_cdp_endpoint_available", return_value=True):
            self.app.refresh_browser_status()

        self.assertEqual(self.app.browser_status_var.get(), "Browser: Open")
        self.assertIn("브라우저가 열려 있습니다", self.app.detail_var.get())

    def test_browser_action_path_prompts_when_cdp_port_is_closed(self):
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        callback = mock.Mock()

        with mock.patch.object(main, "is_cdp_endpoint_available", return_value=False):
            self.app._run_action_with_browser_source_worker(
                "pc-solver",
                callback,
                False,
                123456,
            )

        callback.assert_not_called()
        self.assertEqual(self.app.status_label.cget("text"), "브라우저 열기를 먼저 눌러주세요.")
        self.assertEqual(self.app.state_source.prepare_calls, [])

    def test_browser_action_path_uses_state_source_prepare_once(self):
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        prepared = make_result(current="L", hold="S", queue=["O", "I", "Z", "J", "T"])
        self.app.state_source.latest_result = prepared
        callback = mock.Mock()

        with mock.patch.object(main, "is_cdp_endpoint_available", return_value=True):
            self.app._run_action_with_browser_source_worker(
                "pc-solver",
                callback,
                False,
                987654,
            )

        callback.assert_called_once_with(prepared)
        self.assertEqual(len(self.app.state_source.prepare_calls), 1)
        self.assertEqual(self.app.state_source.prepare_calls[0]["action_name"], "pc-solver")
        self.assertEqual(self.app.status_label.cget("text"), "브라우저 연결됨")

    def test_browser_action_path_does_not_duplicate_when_already_preparing(self):
        self.app.browser_action_in_progress = True
        callback = mock.Mock()

        with mock.patch.object(main.threading, "Thread") as thread_mock:
            self.app.run_action_with_browser_source("pc-solver", callback, show_popup=False)

        thread_mock.assert_not_called()
        callback.assert_not_called()

    def test_browser_action_failure_reenables_buttons(self):
        self.app.browser_action_in_progress = True
        self.app.update_action_buttons_state()

        self.app._on_browser_action_error("TETR.IO 게임 상태를 읽지 못했습니다.", False)

        self.assertFalse(self.app.browser_action_in_progress)
        self.assertEqual(self.app.status_label.cget("text"), "TETR.IO 게임 상태를 읽지 못했습니다.")
        self.assertEqual(self.app.pc_scan_button.cget("state"), "normal")
        self.assertEqual(self.app.setup_scan_button.cget("state"), "normal")

    def test_execute_setup_solver_uses_prepared_snapshot_without_rescan(self):
        result = make_result(current="T", hold="I", queue=["L", "S", "O", "Z", "J"])
        variants = [{"title": "1. ACTIVE 시작", "setup": {"id": "abc"}, "queue_text": "TLSZOJ"}]

        with (
            mock.patch.object(self.app, "build_setup_variants", return_value=variants),
            mock.patch.object(self.app, "render_setup_groups") as render_mock,
        ):
            self.app.execute_setup_solver(result)

        render_mock.assert_called_once_with(variants)
        self.assertEqual(self.app.last_result, result)

    def test_execute_pc_solver_uses_prepared_snapshot_without_rescan(self):
        result = make_result(current="T", hold="I", queue=["L", "S", "O", "Z", "J"])

        with mock.patch.object(self.app, "run_pc_solver_now") as solver_mock:
            self.app.execute_pc_solver(result)

        solver_mock.assert_called_once_with(show_popup=False, force=True)
        self.assertEqual(self.app.last_result, result)

    def test_run_pc_solver_uses_snapshot_current_hold_and_queue(self):
        fake_thread = FakeThread()
        self.app.last_result = make_result(current="T", active_guess="Z", hold="I", queue=["L", "S", "O"])

        with mock.patch.object(main.threading, "Thread", return_value=fake_thread) as thread_mock:
            self.app.run_pc_solver_now(show_popup=False, force=True)

        self.assertEqual(thread_mock.call_count, 1)
        worker_args = thread_mock.call_args.kwargs["args"]
        self.assertEqual(worker_args[1], "T")
        self.assertEqual(worker_args[2], "I")
        self.assertEqual(worker_args[3], ["L", "S", "O"])
        self.assertTrue(fake_thread.started)
        self.assertEqual(self.app.pc_scan_button.cget("state"), "disabled")
        self.assertEqual(self.app.setup_scan_button.cget("state"), "disabled")

    def test_run_pc_solver_waits_for_current_piece(self):
        self.app.last_result = make_result(current="", active_guess="", hold="I")

        with mock.patch.object(main.threading, "Thread") as thread_mock:
            self.app.run_pc_solver_now(show_popup=False, force=True)

        self.assertEqual(thread_mock.call_count, 0)
        self.assertEqual(self.app.pc_solver_status_var.get(), "PC SOLVER: Current piece 대기 중")
        self.assertEqual(self.app.status_label.cget("text"), "PC 해법 대기 - current 없음")

    def test_pc_solver_success_renders_variants(self):
        result = {
            "total": 3,
            "shown_total": 1,
            "solutions": [{"cells": "." * 40}],
            "variants": [
                {
                    "title": "1. ACTIVE T",
                    "preview_rows": [".........." for _ in range(4)],
                    "queue_text": "TLSZOJ",
                    "state_queue": "TLSZOJ",
                }
            ],
        }

        with mock.patch.object(self.app, "render_pc_solution_groups") as render_mock:
            self.app._on_pc_solver_success(result)

        render_mock.assert_called_once_with(result["variants"], solver_result=result)
        self.assertEqual(self.app.pc_solver_status_var.get(), "PC SOLVER: 해법 1개")
        self.assertEqual(self.app.status_label.cget("text"), "PC 해법 계산 완료")

    def test_build_pc_solver_variants_minimal_marks_hold_start_only(self):
        result = {
            "state_queue": "SOIZJLL",
            "solutions": [
                {
                    "cells": "." * 40,
                    "placements": ["S", "O", "I"],
                    "hold_actions": [False, False, False],
                    "final_hold": "T",
                },
                {
                    "cells": "." * 40,
                    "placements": ["T", "O", "I"],
                    "hold_actions": [True, False, False],
                    "final_hold": "S",
                },
            ],
        }

        variants = self.app.build_pc_solver_variants(result, active="S", hold="T")

        self.assertEqual([variant["title"] for variant in variants], ["1.", "2."])
        self.assertFalse(variants[0]["hold_start"])
        self.assertTrue(variants[1]["hold_start"])
        self.assertIn("solution", variants[0])
        self.assertNotIn("next_text", variants[0])
        self.assertNotIn("placements_text", variants[0])
        self.assertNotIn("first_move_text", variants[0])
        self.assertNotIn("final_hold", variants[0])

    def test_render_pc_solution_groups_hides_header_and_debug_text(self):
        self.app.last_result = make_result(current="S", hold="T", queue=["O", "I", "Z", "J", "L", "L"])
        variants = [
            {
                "title": "1.",
                "preview_rows": [".........." for _ in range(4)],
                "hold_start": False,
            },
            {
                "title": "2.",
                "preview_rows": [".........." for _ in range(4)],
                "hold_start": True,
            },
        ]

        self.app.render_pc_solution_groups(variants, solver_result={"state_queue": "SOIZJLL"})
        texts = self.get_canvas_texts()

        self.assertIn("1.", texts)
        self.assertIn("2.", texts)
        self.assertTrue(any("HOLD" in text for text in texts))
        self.assertFalse(any("ACTIVE" in text for text in texts))
        self.assertFalse(any("NEXT" in text for text in texts))
        self.assertFalse(any("STATE" in text for text in texts))
        self.assertFalse(any("ORDER" in text for text in texts))
        self.assertFalse(any("첫 수:" in text for text in texts))

    def test_pc_solver_success_reports_no_solutions(self):
        self.app._on_pc_solver_success(
            {
                "total": 0,
                "shown_total": 0,
                "solutions": [],
                "variants": [],
            }
        )

        self.assertEqual(self.app.pc_solver_status_var.get(), "PC SOLVER: 해법 없음")
        self.assertEqual(self.app.status_label.cget("text"), "PC 해법 없음")

    def test_pc_solver_success_distinguishes_variant_conversion_failure(self):
        self.app._on_pc_solver_success(
            {
                "total": 2,
                "shown_total": 2,
                "solutions": [{"cells": "." * 40}, {"cells": "." * 40}],
                "variants": [],
            }
        )

        self.assertEqual(self.app.pc_solver_status_var.get(), "PC SOLVER 오류: 카드 변환 실패")
        self.assertEqual(self.app.status_label.cget("text"), "PC 해법 카드 변환 실패")

    def test_pc_solver_finish_reenables_buttons(self):
        self.app.is_pc_solving = True
        self.app.update_action_buttons_state()

        self.assertEqual(self.app.pc_scan_button.cget("state"), "disabled")
        self.app._on_pc_solver_finished()

        self.assertFalse(self.app.is_pc_solving)
        self.assertEqual(self.app.pc_scan_button.cget("state"), "normal")
        self.assertEqual(self.app.setup_scan_button.cget("state"), "normal")

    def test_setup_scan_path_still_renders_variants(self):
        result = make_result()
        variants = [{"title": "1. ACTIVE 시작", "setup": {"id": "abc"}, "queue_text": "TLSZOJ"}]

        with (
            mock.patch.object(self.app, "draw_board"),
            mock.patch.object(self.app, "build_setup_variants", return_value=variants),
            mock.patch.object(self.app, "render_setup_groups") as render_mock,
        ):
            self.app._on_scan_success(result, "setup")

        render_mock.assert_called_once_with(variants)
        self.assertIn("최적 셋업", self.app.status_label.cget("text"))

    def test_render_setup_card_shows_only_short_success_rate_text(self):
        variant = {
            "title": "1. ACTIVE 시작",
            "queue_text": "TLSZOJ",
            "lookup_queue_text": "TLSZOJ",
            "display_lookup_text": "TLSZOJ",
            "round_text": "1라운드 | 4/10p | B1",
            "preview_rows": [".........." for _ in range(4)],
            "setup": {
                "id": "4-05M",
                "match": "TLSZO",
                "percent": "98.51%",
                "sol": "TLSZO-98.51%",
                "option_label": "Specific Sol%",
            },
        }

        self.app.render_setup_groups([variant])
        texts = self.get_canvas_texts()

        self.assertIn("1. ACTIVE 시작", texts)
        self.assertIn("1라운드 | 4/10p | B1", texts)
        self.assertIn("SEQ", texts)
        self.assertIn("4-05M", texts)
        self.assertIn("성공 확률 98.51%", texts)
        self.assertFalse(any("MATCH" in text for text in texts))
        self.assertFalse(any("SOL " in text for text in texts))
        self.assertFalse(any("STATE " in text for text in texts))
        self.assertFalse(any("Specific Sol%" in text for text in texts))
        self.assertEqual(variant["setup"]["match"], "TLSZO")
        self.assertEqual(variant["setup"]["sol"], "TLSZO-98.51%")
        self.assertEqual(variant["queue_text"], "TLSZOJ")

    def test_format_setup_success_rate_text_handles_100_and_invalid_values(self):
        self.assertEqual(self.app.format_setup_success_rate_text("100.00%"), "성공 확률 100%")
        self.assertEqual(self.app.format_setup_success_rate_text("100%"), "성공 확률 100%")
        self.assertEqual(self.app.format_setup_success_rate_text(""), "성공 확률 계산 불가")
        self.assertEqual(self.app.format_setup_success_rate_text("N/A"), "성공 확률 계산 불가")

    def test_build_setup_variants_uses_current_on_empty_locked_board(self):
        result = make_result(current="I", active_guess="", hold="", queue=["T", "L", "S", "O", "Z"])
        result["pieces_count"] = 0
        result["piece_counter"] = None
        result["piece_counter_source"] = "derived-revision"
        result["piece_progress_source"] = "derived-spawn-counter"
        result["state_revision"] = 0

        with mock.patch.object(
            main,
            "find_setup_candidates_for_pc",
            return_value=[{"ok": True, "queue": "ITLSOZ", "result": {"id": "setup-1", "fumen": "", "sol": "100%"}}],
        ) as finder_mock:
            variants = self.app.build_setup_variants(result)

        self.assertEqual(finder_mock.call_args.args[1], 1)
        self.assertEqual(
            finder_mock.call_args.kwargs["options"],
            main.AUTOMATIC_SETUP_OPTIONS_BY_ROUND,
        )
        self.assertEqual(len(variants), 1)

    def test_should_show_setup_recommendation_accepts_current_separate_from_locked_board(self):
        result = make_result(current="I", active_guess="", hold="", queue=["T", "L", "S", "O", "Z"])
        result["pieces_count"] = 0
        result["piece_counter"] = None
        result["piece_counter_source"] = "derived-revision"
        result["piece_progress_source"] = "derived-spawn-counter"
        result["state_revision"] = 0

        self.assertTrue(self.app.should_show_setup_recommendation(result))

    def test_build_setup_variants_logs_when_pc_round_is_unavailable(self):
        result = make_result(current="I", active_guess="", hold="", queue=["T", "L", "S", "O", "Z"])
        result["pieces_count"] = None
        result["pc_round"] = None
        result["piece_counter"] = None
        result["piece_counter_source"] = "derived-revision"
        result["lines_cleared"] = None
        result["fixed_cells"] = 0
        result["pc_failure_reason"] = "pieceCounter/linesCleared 없음"
        result["state_revision"] = 0

        with mock.patch("builtins.print") as print_mock:
            variants = self.app.build_setup_variants(result)

        self.assertEqual(variants, [])
        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[SETUP] skipped reason=pc_round_unavailable", printed)
        self.assertIn("pieces_count=None", printed)
        self.assertIn("linesCleared=None", printed)
        self.assertIn("fixedCells=0", printed)
        self.assertIn("stateRevision=0", printed)

    def test_format_pc_round_info_shows_failure_reason(self):
        result = make_result()
        result["pieces_count"] = None
        result["pc_round"] = None
        result["piece_counter"] = None
        result["piece_counter_source"] = "derived-revision"
        result["lines_cleared"] = None
        result["pc_failure_reason"] = "진행값 불일치"

        self.assertEqual(self.app.format_pc_round_info(result), "현재 PC 인식 실패: 진행값 불일치")

    def test_warmup_failure_sets_status_and_later_solve_can_start(self):
        fake_thread = FakeThread()
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        self.app.is_pc_solver_warming = True
        self.app.last_result = make_result()

        with mock.patch.object(main, "warm_gomen_session", side_effect=RuntimeError("warmup boom")):
            self.app._pc_solver_warmup_worker()

        self.assertFalse(self.app.is_pc_solver_warming)
        self.assertFalse(self.app.pc_solver_warm_ready)
        self.assertEqual(self.app.pc_solver_status_var.get(), "PC SOLVER 오류: warmup boom")

        with mock.patch.object(main.threading, "Thread", return_value=fake_thread) as thread_mock:
            self.app.run_pc_solver_now(show_popup=False, force=True)

        self.assertEqual(thread_mock.call_count, 1)
        self.assertTrue(fake_thread.started)


if __name__ == "__main__":
    unittest.main()
