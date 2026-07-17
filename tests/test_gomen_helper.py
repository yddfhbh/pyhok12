import os
import shutil
import threading
import unittest
from unittest import mock

import gomen_helper
from gomen_helper import (
    GomenError,
    GomenSession,
    _filter_valid_solutions,
    _solution_cache_key,
    _validate_solution_sequence,
    board_to_gomen_garbage,
    build_gomen_branches,
    calculate_solver_piece_limit,
    count_pc_solver_fixed_cells,
    format_gomen_garbage_bits,
    get_gomen_bottom_rows,
    get_gomen_solver_path,
    get_node_executable,
    gomen_garbage_to_bottom_rows,
    run_gomen_solver,
)


class FakeProcess:
    _pid = 2000

    def __init__(self, stdout_lines=None, stderr_lines=None, exit_code=None, wait_hook=None):
        FakeProcess._pid += 1
        self.pid = FakeProcess._pid
        self.stdout = iter(stdout_lines or [])
        self.stderr = iter(stderr_lines or [])
        self.stdin = mock.Mock()
        self._exit_code = exit_code
        self.wait_hook = wait_hook
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        if self.wait_hook is not None:
            self.wait_hook(self)
            self.wait_hook = None
        if self._exit_code is None:
            self._exit_code = 0
        return self._exit_code

    def terminate(self):
        self.terminate_calls += 1
        if self._exit_code is None:
            self._exit_code = 0

    def kill(self):
        self.kill_calls += 1
        if self._exit_code is None:
            self._exit_code = -9


class GomenSessionTests(unittest.TestCase):
    def test_stdout_eof_alive_process_does_not_retry_forever(self):
        session = GomenSession()
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        reader = threading.Thread(target=session._reader_loop, args=(proc,), daemon=True)
        reader.start()
        reader.join(1.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertIsNone(session.proc)
        self.assertEqual(session.last_exit_code, 0)

    def test_stdout_eof_logs_once(self):
        session = GomenSession()
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        with mock.patch("builtins.print") as print_mock:
            session._reader_loop(proc)

        messages = [args[0] for args, _ in print_mock.call_args_list if args]
        self.assertEqual(
            sum("stdout closed while process is still alive" in message for message in messages),
            1,
        )

    def test_old_reader_does_not_clear_new_process_identity(self):
        session = GomenSession()
        replacement = FakeProcess(stdout_lines=['{"kind":"ready"}\n'], exit_code=None)

        def replace_current(_proc):
            session.proc = replacement

        old_proc = FakeProcess(stdout_lines=[], exit_code=None, wait_hook=replace_current)
        session.proc = old_proc

        session._reader_loop(old_proc)

        self.assertIs(session.proc, replacement)

    def test_next_ensure_started_restarts_helper_once_after_eof(self):
        session = GomenSession()
        old_proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = old_proc
        session._reader_loop(old_proc)

        new_proc = FakeProcess(stdout_lines=['{"kind":"ready"}\n'], exit_code=None)
        with (
            mock.patch("gomen_helper.get_gomen_solver_path", return_value="solver.js"),
            mock.patch("gomen_helper.get_gomen_js_path", return_value="gomen.js"),
            mock.patch("gomen_helper.get_gomen_wasm_path", return_value="gomen.wasm"),
            mock.patch("gomen_helper.get_legal_boards_path", return_value="legal.leb128"),
            mock.patch("gomen_helper.get_node_executable", return_value="node"),
            mock.patch("gomen_helper.get_tools_dir", return_value="."),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("subprocess.Popen", return_value=new_proc) as popen_mock,
            mock.patch.object(session, "_read_json", return_value={"kind": "ready"}),
        ):
            session.ensure_started(timeout_sec=1)

        self.assertEqual(popen_mock.call_count, 1)
        self.assertIs(session.proc, new_proc)

    def test_app_close_eof_is_quiet(self):
        session = GomenSession()
        session.closing = True
        proc = FakeProcess(stdout_lines=[], exit_code=None)
        session.proc = proc

        with mock.patch("builtins.print") as print_mock:
            session._reader_loop(proc)

        self.assertEqual(print_mock.call_count, 0)
        self.assertEqual(session.last_reader_warning, "")


class GomenHelperTests(unittest.TestCase):
    def setUp(self):
        gomen_helper._RESULT_CACHE.clear()

    def make_board(self, bottom_rows):
        board = [["." for _ in range(10)] for _ in range(20)]
        for offset, row_text in enumerate(bottom_rows[-4:]):
            board[16 + offset] = list((row_text + "." * 10)[:10])
        return board

    def test_board_to_gomen_garbage_bit_mapping(self):
        board = self.make_board(
            [
                "X.........",
                "..........",
                "X.........",
                "X........X",
            ]
        )

        garbage = board_to_gomen_garbage(board)

        self.assertEqual(garbage, (1 << 30) | (1 << 10) | (1 << 0) | (1 << 9))
        self.assertEqual(format_gomen_garbage_bits(garbage), format(garbage, "040b"))
        self.assertEqual(
            gomen_garbage_to_bottom_rows(garbage),
            [
                "X.........",
                "..........",
                "X.........",
                "X........X",
            ],
        )

    def test_count_pc_solver_fixed_cells_uses_bottom_four_locked_rows(self):
        board = self.make_board(["XXXX......", ".XXX......", "..XX......", "...X......"])
        self.assertEqual(count_pc_solver_fixed_cells(board), 10)

    def test_solver_piece_limit_empty_board_is_eleven(self):
        info = calculate_solver_piece_limit(self.make_board([".........."] * 4))
        self.assertEqual(info["fixed_cells"], 0)
        self.assertEqual(info["placements_needed"], 10)
        self.assertEqual(info["solver_piece_limit"], 11)

    def test_solver_piece_limit_three_placements_is_eight(self):
        info = calculate_solver_piece_limit(self.make_board(["XXXX......", "XXXX......", "XXXX......", ".........."]))
        self.assertEqual(info["fixed_cells"], 12)
        self.assertEqual(info["placements_needed"], 7)
        self.assertEqual(info["solver_piece_limit"], 8)

    def test_solver_piece_limit_four_placements_is_seven(self):
        info = calculate_solver_piece_limit(self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"]))
        self.assertEqual(info["fixed_cells"], 16)
        self.assertEqual(info["placements_needed"], 6)
        self.assertEqual(info["solver_piece_limit"], 7)

    def test_solver_piece_limit_seven_placements_is_four(self):
        info = calculate_solver_piece_limit(self.make_board(["XXXXXXXXXX", "XXXXXXXXXX", "XXXXXXXX..", ".........."]))
        self.assertEqual(info["fixed_cells"], 28)
        self.assertEqual(info["placements_needed"], 3)
        self.assertEqual(info["solver_piece_limit"], 4)

    def test_build_gomen_branches_exact_state_without_hold(self):
        state_queue, branches = build_gomen_branches("O", "", list("JJOZILTS"), solver_piece_limit=7)
        self.assertEqual(state_queue, "OJJOZILTS")
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["name"], "exact-state")
        self.assertEqual(branches[0]["current"], "O")
        self.assertEqual(branches[0]["initial_hold"], "")
        self.assertEqual(branches[0]["next_queue"], "JJOZIL")

    def test_build_gomen_branches_exact_state_with_hold(self):
        state_queue, branches = build_gomen_branches("S", "T", list("OIZJLL"), solver_piece_limit=7)
        self.assertEqual(state_queue, "SOIZJLL")
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["current"], "S")
        self.assertEqual(branches[0]["initial_hold"], "T")
        self.assertEqual(branches[0]["next_queue"], "OIZJL")
        self.assertEqual(branches[0]["required_total"], 7)
        self.assertEqual(branches[0]["available_total"], 8)

    def test_build_gomen_branches_never_generates_nine_piece_total_when_limit_is_eight(self):
        _, branches = build_gomen_branches("O", "T", list("JJOZILTS"), solver_piece_limit=8)
        branch = branches[0]
        total_accessible = 1 + len(branch["next_queue"]) + 1
        self.assertEqual(total_accessible, 8)

    def test_validate_solution_sequence_preserves_current_hold_next_state(self):
        solution = {
            "initial_current": "S",
            "initial_hold": "T",
            "placements": ["S", "O", "I"],
            "hold_actions": [False, False, False],
            "final_hold": "T",
            "consumed_next_count": 2,
            "physics": "TETRIO",
        }
        is_valid, reason, normalized = _validate_solution_sequence(solution, "S", "T", "OIZJLL")
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")
        self.assertEqual(normalized["placements"], ["S", "O", "I"])

    def test_validate_solution_sequence_accepts_hold_first_path(self):
        solution = {
            "initial_current": "S",
            "initial_hold": "T",
            "placements": ["T", "O", "I"],
            "hold_actions": [True, False, False],
            "final_hold": "S",
            "consumed_next_count": 2,
            "physics": "TETRIO",
        }
        is_valid, reason, normalized = _validate_solution_sequence(solution, "S", "T", "OIZJLL")
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")
        self.assertEqual(normalized["final_hold"], "S")

    def test_validate_solution_sequence_rejects_empty_hold_style_path_when_hold_is_filled(self):
        solution = {
            "initial_current": "S",
            "initial_hold": "T",
            "placements": ["O"],
            "hold_actions": [True],
            "final_hold": "S",
            "consumed_next_count": 1,
            "physics": "TETRIO",
        }
        is_valid, reason, _ = _validate_solution_sequence(solution, "S", "T", "OIZJLL")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "invalid_hold_transition")

    def test_validate_solution_sequence_rejects_next_skip(self):
        solution = {
            "initial_current": "S",
            "initial_hold": "T",
            "placements": ["S", "I"],
            "hold_actions": [False, False],
            "final_hold": "T",
            "consumed_next_count": 1,
            "physics": "TETRIO",
        }
        is_valid, reason, _ = _validate_solution_sequence(solution, "S", "T", "OIZJLL")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "invalid_next_order")

    def test_filter_keeps_same_cells_with_different_hold_order_as_distinct(self):
        result = {
            "solutions": [
                {
                    "cells": "X" * 40,
                    "placements": ["S", "O", "I"],
                    "hold_actions": [False, False, False],
                    "final_hold": "T",
                    "consumed_next_count": 2,
                    "physics": "TETRIO",
                },
                {
                    "cells": "X" * 40,
                    "placements": ["T", "O", "I"],
                    "hold_actions": [True, False, False],
                    "final_hold": "S",
                    "consumed_next_count": 2,
                    "physics": "TETRIO",
                },
            ]
        }
        filtered = _filter_valid_solutions(result, current="S", hold="T", next_queue="OIZJLL")
        self.assertEqual(len(filtered), 2)
        self.assertNotEqual(_solution_cache_key(filtered[0]), _solution_cache_key(filtered[1]))

    def test_run_gomen_solver_logs_request_payload(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve_state.return_value = {
            "ok": True,
            "total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [
                {
                    "cells": "." * 40,
                    "placements": ["T", "J"],
                    "hold_actions": [False, False],
                    "final_hold": "L",
                    "consumed_next_count": 1,
                    "physics": "TETRIO",
                }
            ],
        }

        with (
            mock.patch("gomen_helper.get_gomen_session", return_value=session),
            mock.patch("builtins.print") as print_mock,
        ):
            result = run_gomen_solver(
                board=board,
                active="T",
                hold="L",
                queue=["J", "O", "S", "Z", "I", "L"],
                timeout_sec=1,
            )

        self.assertEqual(result["branch_name"], "exact-state")
        messages = [args[0] for args, _ in print_mock.call_args_list if args]
        self.assertTrue(any("fixed_cells=16" in message for message in messages))
        self.assertTrue(any("placements_needed=6" in message for message in messages))
        self.assertTrue(any("solver_piece_limit=7" in message for message in messages))
        self.assertTrue(any("active=T" in message for message in messages))
        self.assertTrue(any("hold=L" in message for message in messages))
        self.assertTrue(any("raw_queue=JOSZIL" in message for message in messages))
        self.assertTrue(any("state_queue=TJOSZIL" in message for message in messages))
        self.assertTrue(any("next_queue=JOSZI" in message for message in messages))

    def test_run_gomen_solver_uses_tetrio_physics_by_default(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve_state.return_value = {
            "ok": True,
            "total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [
                {
                    "cells": "." * 40,
                    "placements": ["O"],
                    "hold_actions": [False],
                    "final_hold": "",
                    "consumed_next_count": 0,
                    "physics": "TETRIO",
                }
            ],
        }

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            run_gomen_solver(board=board, active="O", hold="", queue=list("JJOZIL"), timeout_sec=1)

        self.assertEqual(session.solve_state.call_args.kwargs["physics"], "TETRIO")

    def test_run_gomen_solver_reports_queue_shortage(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])

        with self.assertRaisesRegex(GomenError, r"PC SOLVER: NEXT 큐 부족\n필요=7, 확보=3"):
            run_gomen_solver(board=board, active="O", hold="", queue=["J", "J"], timeout_sec=1)

    def test_branch_cache_key_uses_trimmed_next_queue_and_limit(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve_state.return_value = {
            "ok": True,
            "total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [
                {
                    "cells": "." * 40,
                    "placements": ["O", "J"],
                    "hold_actions": [False, False],
                    "final_hold": "",
                    "consumed_next_count": 1,
                    "physics": "TETRIO",
                }
            ],
        }

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            first = run_gomen_solver(board=board, active="O", hold="", queue=list("JJOZILTS"), timeout_sec=1)
            second = run_gomen_solver(board=board, active="O", hold="", queue=list("JJOZIL"), timeout_sec=1)

        self.assertEqual(first["shown_total"], 1)
        self.assertEqual(second["shown_total"], 1)
        self.assertEqual(session.solve_state.call_count, 1)

    def test_run_gomen_solver_filters_invalid_solution_and_sets_message(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve_state.return_value = {
            "ok": True,
            "total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [
                {
                    "cells": "." * 40,
                    "placements": ["O"],
                    "hold_actions": [True],
                    "final_hold": "S",
                    "consumed_next_count": 1,
                    "physics": "TETRIO",
                }
            ],
        }

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            result = run_gomen_solver(board=board, active="S", hold="T", queue=list("OIZJLL"), timeout_sec=1)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["shown_total"], 0)
        self.assertEqual(result["display_message"], "PC SOLVER: 유효한 실제 게임 순서 해법 없음")


def has_node_runtime():
    executable = get_node_executable()
    if os.path.isabs(executable):
        return os.path.exists(executable)
    return shutil.which(executable) is not None


@unittest.skipUnless(
    os.path.exists(get_gomen_solver_path()) and has_node_runtime(),
    "node runtime or gomen solver assets are unavailable",
)
class GomenSessionIntegrationTests(unittest.TestCase):
    def test_real_solver_handles_two_legacy_requests_on_same_process(self):
        session = GomenSession()
        try:
            response1 = session.solve(queue_text="TIJLOSZ", garbage=0, timeout_sec=30, target_queue="TIJLOSZ")
            first_proc = session.proc

            self.assertIsNotNone(first_proc)
            self.assertIsNone(first_proc.poll())
            self.assertTrue(response1["ok"])
            self.assertIn("solutions", response1)

            response2 = session.solve(queue_text="TIJLOSZ", garbage=0, timeout_sec=30, target_queue="TIJLOSZ")
            self.assertTrue(response2["ok"])
            self.assertIs(session.proc, first_proc)
            self.assertIsNone(first_proc.poll())
        finally:
            session.close()

    def test_real_solver_supports_exact_state_api(self):
        session = GomenSession()
        try:
            response = session.solve_state(
                current="S",
                initial_hold="T",
                next_queue="OIZJL",
                garbage=0,
                physics="TETRIO",
                timeout_sec=30,
            )
        finally:
            session.close()

        self.assertTrue(response["ok"])
        self.assertIn("solutions", response)


if __name__ == "__main__":
    unittest.main()
