import os
import shutil
import threading
import unittest
from unittest import mock

import gomen_helper
from gomen_helper import (
    GomenError,
    GomenSession,
    board_to_gomen_garbage,
    calculate_solver_piece_limit,
    build_gomen_branches,
    count_pc_solver_fixed_cells,
    format_gomen_garbage_bits,
    get_gomen_bottom_rows,
    get_gomen_solver_path,
    get_node_executable,
    gomen_garbage_to_bottom_rows,
    run_gomen_solver,
)

try:
    from py_fumen_py import decode as decode_fumen
except Exception:
    decode_fumen = None


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
        replacement = FakeProcess(stdout_lines=["{\"kind\":\"ready\"}\n"], exit_code=None)

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

        new_proc = FakeProcess(stdout_lines=["{\"kind\":\"ready\"}\n"], exit_code=None)
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

    def decode_bottom_rows_from_fumen(self, fumen_text):
        page = decode_fumen(fumen_text)[0]
        field = page.field
        rows = []
        for y in range(3, -1, -1):
            cells = []
            for x in range(10):
                cells.append("." if str(field.at(x, y)) == "0" else "X")
            rows.append("".join(cells))
        return rows

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

    @unittest.skipUnless(decode_fumen is not None, "py_fumen_py is unavailable")
    def test_setup_finder_board_roundtrips_through_gomen_garbage(self):
        rows = self.decode_bottom_rows_from_fumen("v115@9gwwIexwCeglDewwR4ilDeR4zhNeAgH")
        board = self.make_board(rows)

        self.assertEqual(get_gomen_bottom_rows(board), rows)
        self.assertEqual(gomen_garbage_to_bottom_rows(board_to_gomen_garbage(board)), rows)

    def test_run_gomen_solver_logs_request_payload(self):
        board = self.make_board(
            [
                "XXXX......",
                "XXXX......",
                "XXXX......",
                "XXXX......",
            ]
        )
        session = mock.Mock()
        session.solve.side_effect = [
            {"ok": True, "total": 0, "shown_total": 0, "exact_match_used": False, "solutions": []},
            {"ok": True, "total": 3, "shown_total": 2, "exact_match_used": True, "solutions": []},
        ]

        with (
            mock.patch("gomen_helper.get_gomen_session", return_value=session),
            mock.patch("builtins.print") as print_mock,
        ):
            result = run_gomen_solver(
                board=board,
                active="T",
                hold="L",
                queue=["J", "O", "S", "Z", "I", "L"],
                limit=6,
                timeout_sec=1,
            )

        messages = [args[0] for args, _ in print_mock.call_args_list if args]
        self.assertTrue(any("bottom_row_1=XXXX......" in message for message in messages))
        self.assertTrue(any("fixed_cells=16" in message for message in messages))
        self.assertTrue(any("placements_needed=6" in message for message in messages))
        self.assertTrue(any("solver_piece_limit=7" in message for message in messages))
        self.assertTrue(any("garbage_dec=" in message for message in messages))
        self.assertTrue(any("garbage_bits=" in message for message in messages))
        self.assertTrue(any("active=T" in message for message in messages))
        self.assertTrue(any("hold=L" in message for message in messages))
        self.assertTrue(any("raw_queue=JOSZIL" in message for message in messages))
        self.assertTrue(any("state_queue=TJOSZIL" in message for message in messages))
        self.assertTrue(any("queue_text=TJOSZIL" in message for message in messages))
        self.assertTrue(any("queue_text=LTJOSZI" in message for message in messages))
        self.assertTrue(any("queue_length=7" in message for message in messages))
        self.assertTrue(any("use_hold=true" in message for message in messages))
        self.assertEqual(result["branch_name"], "active-first+hold-first")

    def test_run_gomen_solver_tries_active_first_before_hold_first(self):
        board = self.make_board(
            [
                "XXXX......",
                "XXXX......",
                "XXXX......",
                "XXXX......",
            ]
        )
        session = mock.Mock()
        session.solve.side_effect = [
            {"ok": True, "total": 0, "shown_total": 0, "exact_match_used": False, "solutions": []},
            {"ok": True, "total": 5, "shown_total": 4, "exact_match_used": True, "solutions": [{"id": "a"}]},
        ]

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            result = run_gomen_solver(
                board=board,
                active="T",
                hold="L",
                queue=["J", "O", "S", "Z", "I", "L"],
                limit=6,
                timeout_sec=1,
            )

        first_call = session.solve.call_args_list[0].kwargs
        second_call = session.solve.call_args_list[1].kwargs
        self.assertEqual(first_call["queue_text"], "TJOSZIL")
        self.assertEqual(first_call["target_queue"], "TJOSZIL")
        self.assertTrue(first_call["hold"])
        self.assertEqual(second_call["queue_text"], "LTJOSZI")
        self.assertEqual(second_call["target_queue"], "LTJOSZI")
        self.assertTrue(second_call["hold"])
        self.assertEqual(result["branch_name"], "active-first+hold-first")
        self.assertTrue(result["branch_results"][0]["engine_zero"])
        self.assertFalse(result["branch_results"][0]["exact_queue_filter_miss"])

    def test_build_gomen_branches_keeps_single_branch_without_hold(self):
        state_queue, branches = build_gomen_branches("T", "", ["L", "S", "I"], solver_piece_limit=4)

        self.assertEqual(state_queue, "TLSI")
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["name"], "active-first")
        self.assertEqual(branches[0]["queue_text"], "TLSI")

    def test_solver_piece_limit_empty_board_is_eleven(self):
        board = self.make_board(["..........", "..........", "..........", ".........."])
        info = calculate_solver_piece_limit(board)

        self.assertEqual(info["fixed_cells"], 0)
        self.assertEqual(info["placements_needed"], 10)
        self.assertEqual(info["solver_piece_limit"], 11)

    def test_solver_piece_limit_three_placements_is_eight(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", ".........."])
        info = calculate_solver_piece_limit(board)

        self.assertEqual(info["fixed_cells"], 12)
        self.assertEqual(info["placements_needed"], 7)
        self.assertEqual(info["solver_piece_limit"], 8)

    def test_solver_piece_limit_four_placements_is_seven(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        info = calculate_solver_piece_limit(board)

        self.assertEqual(info["fixed_cells"], 16)
        self.assertEqual(info["placements_needed"], 6)
        self.assertEqual(info["solver_piece_limit"], 7)

    def test_solver_piece_limit_seven_placements_is_four(self):
        board = self.make_board(["XXXXXXXXXX", "XXXXXXXXXX", "XXXXXXXX..", ".........."])
        info = calculate_solver_piece_limit(board)

        self.assertEqual(info["fixed_cells"], 28)
        self.assertEqual(info["placements_needed"], 3)
        self.assertEqual(info["solver_piece_limit"], 4)

    def test_count_pc_solver_fixed_cells_uses_bottom_four_locked_rows(self):
        board = self.make_board(["XXXX......", ".XXX......", "..XX......", "...X......"])
        self.assertEqual(count_pc_solver_fixed_cells(board), 10)

    def test_build_gomen_branches_trims_next_queue_to_solver_limit_without_hold(self):
        state_queue, branches = build_gomen_branches(
            "O",
            "",
            list("JJOZILTSQ".replace("Q", "")),
            solver_piece_limit=7,
        )

        self.assertEqual(state_queue, "OJJOZILTS")
        self.assertEqual(branches[0]["queue_text"], "OJJOZIL")
        self.assertEqual(len(branches[0]["queue_text"]), 7)

    def test_build_gomen_branches_trims_active_first_with_hold(self):
        _, branches = build_gomen_branches(
            "O",
            "T",
            list("JJOZILTS"),
            solver_piece_limit=7,
        )

        self.assertEqual(branches[0]["queue_text"], "OJJOZIL")
        self.assertEqual(len(branches[0]["queue_text"]), 7)

    def test_build_gomen_branches_trims_hold_first_with_hold(self):
        _, branches = build_gomen_branches(
            "O",
            "T",
            list("JJOZILTS"),
            solver_piece_limit=7,
        )

        self.assertEqual(branches[1]["queue_text"], "TOJJOZI")
        self.assertEqual(len(branches[1]["queue_text"]), 7)

    def test_build_gomen_branches_never_generates_nine_piece_queue_when_limit_is_eight(self):
        _, branches = build_gomen_branches(
            "O",
            "T",
            list("JJOZILTSX".replace("X", "")),
            solver_piece_limit=8,
        )

        self.assertEqual(len(branches[0]["queue_text"]), 8)
        self.assertEqual(len(branches[1]["queue_text"]), 8)

    def test_run_gomen_solver_reports_queue_shortage(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])

        with self.assertRaisesRegex(GomenError, r"PC SOLVER: NEXT 큐 부족\n필요=7, 확보=3"):
            run_gomen_solver(
                board=board,
                active="O",
                hold="",
                queue=["J", "J"],
                timeout_sec=1,
            )

    def test_run_gomen_solver_uses_tetrio_physics_by_default(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve.return_value = {
            "ok": True,
            "total": 1,
            "matched_total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [{"cells": "." * 40}],
        }

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            run_gomen_solver(
                board=board,
                active="O",
                hold="",
                queue=list("JJOZIL"),
                timeout_sec=1,
            )

        self.assertEqual(session.solve.call_args.kwargs["physics"], "TETRIO")

    def test_run_gomen_solver_merges_branch_results_and_deduplicates_solutions(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve.side_effect = [
            {
                "ok": True,
                "total": 2,
                "matched_total": 2,
                "shown_total": 2,
                "exact_match_used": True,
                "solutions": [
                    {"cells": "A" * 40, "matched_group": "without_hold"},
                    {"cells": "B" * 40, "matched_group": "without_hold"},
                ],
            },
            {
                "ok": True,
                "total": 3,
                "matched_total": 2,
                "shown_total": 2,
                "exact_match_used": True,
                "solutions": [
                    {"cells": "B" * 40, "matched_group": "with_hold"},
                    {"cells": "C" * 40, "matched_group": "with_hold"},
                ],
            },
        ]

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            result = run_gomen_solver(
                board=board,
                active="O",
                hold="T",
                queue=list("JJOZIL"),
                timeout_sec=1,
            )

        self.assertEqual(result["total"], 5)
        self.assertEqual(result["shown_total"], 3)
        self.assertEqual(len(result["solutions"]), 3)

    def test_run_gomen_solver_survives_one_branch_error_if_other_branch_succeeds(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve.side_effect = [
            GomenError("branch boom"),
            {
                "ok": True,
                "total": 1,
                "matched_total": 1,
                "shown_total": 1,
                "exact_match_used": True,
                "solutions": [{"cells": "A" * 40}],
            },
        ]

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            result = run_gomen_solver(
                board=board,
                active="O",
                hold="T",
                queue=list("JJOZIL"),
                timeout_sec=1,
            )

        self.assertEqual(result["shown_total"], 1)
        self.assertEqual(len(result["branch_results"]), 2)
        self.assertTrue(any(not item.get("ok", True) for item in result["branch_results"]))

    def test_run_gomen_solver_returns_no_solution_only_when_both_branches_are_zero(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve.side_effect = [
            {"ok": True, "total": 0, "matched_total": 0, "shown_total": 0, "exact_match_used": False, "solutions": []},
            {"ok": True, "total": 0, "matched_total": 0, "shown_total": 0, "exact_match_used": False, "solutions": []},
        ]

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            result = run_gomen_solver(
                board=board,
                active="O",
                hold="T",
                queue=list("JJOZIL"),
                timeout_sec=1,
            )

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["shown_total"], 0)

    def test_branch_cache_key_uses_trimmed_queue_and_limit(self):
        board = self.make_board(["XXXX......", "XXXX......", "XXXX......", "XXXX......"])
        session = mock.Mock()
        session.solve.return_value = {
            "ok": True,
            "total": 1,
            "matched_total": 1,
            "shown_total": 1,
            "exact_match_used": True,
            "solutions": [{"cells": "A" * 40}],
        }

        with mock.patch("gomen_helper.get_gomen_session", return_value=session):
            first = run_gomen_solver(
                board=board,
                active="O",
                hold="",
                queue=list("JJOZILTS"),
                timeout_sec=1,
            )
            second = run_gomen_solver(
                board=board,
                active="O",
                hold="",
                queue=list("JJOZIL"),
                timeout_sec=1,
            )

        self.assertEqual(first["shown_total"], 1)
        self.assertEqual(second["shown_total"], 1)
        self.assertEqual(session.solve.call_count, 1)


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
    def test_real_solver_handles_two_requests_on_same_process(self):
        session = GomenSession()
        try:
            response1 = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )
            first_proc = session.proc

            self.assertIsNotNone(first_proc)
            self.assertIsNone(first_proc.poll())
            self.assertTrue(response1["ok"])
            self.assertIn("solutions", response1)

            response2 = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )

            self.assertTrue(response2["ok"])
            self.assertIs(session.proc, first_proc)
            self.assertIsNone(first_proc.poll())
        finally:
            session.close()

    def test_next_solve_restarts_after_forced_process_exit(self):
        session = GomenSession()
        try:
            session.ensure_started(timeout_sec=30)
            first_proc = session.proc

            self.assertIsNotNone(first_proc)
            first_proc.kill()
            first_proc.wait(timeout=5)

            if session.reader_thread is not None:
                session.reader_thread.join(timeout=5)

            response = session.solve(
                queue_text="TIJLOSZ",
                garbage=0,
                timeout_sec=30,
                target_queue="TIJLOSZ",
            )

            self.assertTrue(response["ok"])
            self.assertIsNotNone(session.proc)
            self.assertIsNot(session.proc, first_proc)
        finally:
            session.close()

    @unittest.skipUnless(decode_fumen is not None, "py_fumen_py is unavailable")
    def test_known_pc_hold_empty_current_transform_matches_gomen_mapping(self):
        page = decode_fumen("v115@JhglGeilBeR4wwBezhR4ywKeAgH")[0]
        field = page.field
        board = [["." for _ in range(10)] for _ in range(20)]
        for y in range(4):
            for x in range(10):
                board[19 - y][x] = "." if str(field.at(x, y)) == "0" else "X"

        def transform(mode):
            bottom = [row[:] for row in board[-4:]]
            if "v" in mode:
                bottom = list(reversed(bottom))
            if "h" in mode:
                bottom = [list(reversed(row)) for row in bottom]
            out = [["." for _ in range(10)] for _ in range(20)]
            for index, row in enumerate(bottom):
                out[16 + index] = row
            return out

        session = GomenSession()
        try:
            totals = {}
            for mode in ("current", "h", "v", "vh"):
                response = session.solve(
                    queue_text="LTSI",
                    garbage=board_to_gomen_garbage(transform(mode)),
                    hold=True,
                    timeout_sec=30,
                    target_queue="",
                )
                totals[mode] = int(response.get("total") or 0)
        finally:
            session.close()

        self.assertGreater(totals["current"], 0)
        self.assertEqual(totals["h"], 0)
        self.assertEqual(totals["v"], 0)
        self.assertEqual(totals["vh"], 0)

    @unittest.skipUnless(decode_fumen is not None, "py_fumen_py is unavailable")
    def test_known_pc_hold_filled_active_first_branch_avoids_false_negative(self):
        page = decode_fumen("v115@9gQ4IeR4Heg0Q4BtDeRpi0BtCeRpJeAgH")[0]
        field = page.field
        board = [["." for _ in range(10)] for _ in range(20)]
        for y in range(4):
            for x in range(10):
                board[19 - y][x] = "." if str(field.at(x, y)) == "0" else "X"

        session = GomenSession()
        try:
            transform_totals = {}
            for mode in ("current", "h", "v", "vh"):
                bottom = [row[:] for row in board[-4:]]
                if "v" in mode:
                    bottom = list(reversed(bottom))
                if "h" in mode:
                    bottom = [list(reversed(row)) for row in bottom]
                transformed = [["." for _ in range(10)] for _ in range(20)]
                for index, row in enumerate(bottom):
                    transformed[16 + index] = row
                garbage = board_to_gomen_garbage(transformed)
                approx = session.solve(
                    queue_text="LTJOSZ",
                    garbage=garbage,
                    hold=True,
                    timeout_sec=30,
                    target_queue="",
                )
                active_first = session.solve(
                    queue_text="TJOSZ",
                    garbage=garbage,
                    hold=True,
                    timeout_sec=30,
                    target_queue="",
                )
                transform_totals[mode] = {
                    "approx": int(approx.get("total") or 0),
                    "active_first": int(active_first.get("total") or 0),
                }
        finally:
            session.close()

        self.assertEqual(transform_totals["current"]["approx"], 0)
        self.assertGreater(transform_totals["current"]["active_first"], 0)
        self.assertEqual(set(transform_totals), {"current", "h", "v", "vh"})

    @unittest.skipUnless(decode_fumen is not None, "py_fumen_py is unavailable")
    def test_known_pc_hold_filled_trimmed_queue_recovers_solution(self):
        page = decode_fumen("v115@9gQ4IeR4Heg0Q4BtDeRpi0BtCeRpJeAgH")[0]
        field = page.field
        board = [["." for _ in range(10)] for _ in range(20)]
        for y in range(4):
            for x in range(10):
                board[19 - y][x] = "." if str(field.at(x, y)) == "0" else "X"

        session = GomenSession()
        try:
            long_queue = session.solve(
                queue_text="LTJOSZ",
                garbage=board_to_gomen_garbage(board),
                hold=True,
                physics="TETRIO",
                timeout_sec=30,
                target_queue="",
            )
            trimmed_queue = session.solve(
                queue_text="TJOSZ",
                garbage=board_to_gomen_garbage(board),
                hold=True,
                physics="TETRIO",
                timeout_sec=30,
                target_queue="",
            )
        finally:
            session.close()

        self.assertEqual(int(long_queue.get("total") or 0), 0)
        self.assertGreater(int(trimmed_queue.get("total") or 0), 0)


if __name__ == "__main__":
    unittest.main()
