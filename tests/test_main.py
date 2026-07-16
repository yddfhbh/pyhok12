import tkinter as tk
import unittest
from unittest import mock

import main


class FakeStateSource:
    def __init__(self, _config_path):
        self.started = False

    def get_status(self):
        return {
            "browser_status": "Connected",
            "game_state": "Playing",
            "piece_counter": 4,
            "current": "T",
            "hold": "I",
            "queue": "LSZOJ",
            "last_update_age_ms": 25,
            "detail": "ready",
        }

    def start(self):
        self.started = True

    def close(self):
        return None

    def reload_config(self):
        return None


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


class MainUiTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            mock.patch.object(main, "load_config", return_value={}),
            mock.patch.object(main, "TetrioStateSource", FakeStateSource),
            mock.patch.object(main.TetrisScannerApp, "register_global_hotkeys", autospec=True, return_value=None),
            mock.patch.object(main.TetrisScannerApp, "start_browser_source", autospec=True, return_value=None),
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

    def test_hydra_controls_are_removed_and_action_buttons_remain(self):
        self.assertFalse(hasattr(self.app, "hydra_frame"))
        self.assertFalse(hasattr(self.app, "active_var"))
        self.assertFalse(hasattr(self.app, "manual_see_var"))
        self.assertFalse(hasattr(self.app, "bag_var"))
        self.assertFalse(hasattr(self.app, "mode_var"))
        self.assertFalse(hasattr(self.app, "identity_var"))
        self.assertFalse(hasattr(self.app, "board_size_var"))
        self.assertEqual(self.app.pc_scan_button.cget("text"), "상태 읽기 ->\nPC 해법 찾기\n[Del]")
        self.assertEqual(self.app.setup_scan_button.cget("text"), "상태 읽기 ->\n최적 셋업 찾기\n[End]")

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

    def test_window_height_is_reduced_without_clipping(self):
        geometry = self.root.geometry().split("+")[0]
        width_text, height_text = geometry.split("x")
        actual_width = int(width_text)
        actual_height = int(height_text)

        self.assertEqual(actual_width, 620)
        self.assertGreaterEqual(actual_height, 870)
        self.assertLessEqual(actual_height, 900)
        self.assertGreaterEqual(actual_height, self.root.winfo_reqheight())

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
