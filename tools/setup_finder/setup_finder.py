import json
import sys
from collections import Counter
from pathlib import Path


VALID_PIECES = set("IJLOSTZ")

# Setup Konbini 쪽 정렬 기준.
KJ_SORT_ORDER = "TILJSZO"

_DATA_CACHE = None


def normalize_queue(queue):
    if isinstance(queue, (list, tuple)):
        text = "".join(str(piece) for piece in queue)
    else:
        text = str(queue or "")

    text = text.upper().strip()
    return "".join(ch for ch in text if ch in VALID_PIECES)


def normalize_priority_text(text):
    return normalize_queue(text)


def kjsort(text):
    text = normalize_queue(text)
    return "".join(piece * text.count(piece) for piece in KJ_SORT_ORDER)


def imgur_url(imgur_id):
    imgur_id = str(imgur_id or "").strip()
    if not imgur_id:
        return ""

    if imgur_id.startswith("http://") or imgur_id.startswith("https://"):
        return imgur_id

    return f"https://i.imgur.com/{imgur_id}.png"


def get_data_path():
    return Path(__file__).resolve().parent / "setup_data.json"


def load_setup_data():
    global _DATA_CACHE

    if _DATA_CACHE is not None:
        return _DATA_CACHE

    path = get_data_path()
    if not path.exists():
        raise RuntimeError(
            f"setup_data.json이 없습니다: {path}\n"
            "먼저 setup_converter.py를 실행해서 데이터를 생성해야 합니다."
        )

    _DATA_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _DATA_CACHE


def make_result(ok, pc, queue, setup_id="", message="", row=None):
    result = None

    if row:
        result = {
            "id": row.get("id", ""),
            "sol": row.get("sol", ""),
            "sol_full": row.get("sol_full", row.get("sol", "")),
            "fumen": row.get("fumen", ""),
            "imgur": imgur_url(row.get("imgur", "")),
            "match": row.get("match", ""),
            "percent": row.get("percent", ""),
            "option_label": row.get("option_label", ""),
        }

    return {
        "ok": ok,
        "pc": pc,
        "queue": queue,
        "id": setup_id,
        "message": message,
        "result": result,
    }


def parse_setup_score(value):
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value or "").strip()
    if not text:
        return -1.0

    try:
        return float(text)
    except ValueError:
        pass

    if "%" in text:
        number_text = text.rsplit("%", 1)[0].split()[-1]
        try:
            return float(number_text) / 100.0
        except ValueError:
            return -1.0

    return -1.0


def counts_cover(source_text, target_text):
    source = Counter(normalize_queue(source_text))
    target = Counter(normalize_queue(target_text))
    return all(source[piece] >= need for piece, need in target.items())


def get_round_options(options, pc_round):
    if not isinstance(options, dict):
        return {}
    return dict(options.get(str(pc_round)) or options.get(pc_round) or {})


def priority_key(sequence, priority_text):
    priority = normalize_priority_text(priority_text)
    if not priority:
        return ()

    order = {piece: index for index, piece in enumerate(priority)}
    sequence = normalize_queue(sequence)
    return tuple(order.get(piece, len(priority) + 10) for piece in sequence)


def choose_best_row(matches, priority_text="", sequence_getter=None, prefer_score=True):
    sorted_rows = sort_candidate_rows(
        matches,
        priority_text=priority_text,
        sequence_getter=sequence_getter,
        prefer_score=prefer_score,
    )
    return sorted_rows[0] if sorted_rows else None


def sort_candidate_rows(matches, priority_text="", sequence_getter=None, prefer_score=True):
    if not matches:
        return []

    if sequence_getter is None:
        sequence_getter = lambda row: row.get("id", "")

    def sort_key(row):
        score = parse_setup_score(row.get("sol"))
        sequence = sequence_getter(row)
        pkey = priority_key(sequence, priority_text)
        base = (
            -score,
            pkey,
            -len(normalize_queue(sequence)),
            str(row.get("id", "")),
        )
        if prefer_score:
            return base
        return (
            pkey,
            -score,
            -len(normalize_queue(sequence)),
            str(row.get("id", "")),
        )

    return sorted(matches, key=sort_key)


def parse_sol_patterns(sol_text):
    patterns = []

    for chunk in str(sol_text or "").split(","):
        text = chunk.strip()
        if not text:
            continue

        seq_text, _, percent_text = text.partition("-")
        seq = normalize_queue(seq_text)
        if not seq:
            continue

        patterns.append(
            {
                "seq": seq,
                "percent": percent_text.strip(),
                "raw": text,
            }
        )

    return patterns


def unique_rows_by_key(rows, key_getter=None):
    key_getter = key_getter or (lambda row: row.get("id", ""))
    unique = []
    seen = set()
    for row in rows or []:
        key = key_getter(row)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def collect_1st_matches(queue, options=None):
    q = normalize_queue(queue)
    target = q[:5]
    matches = []

    for row in load_setup_data().get("first", []):
        patterns = parse_sol_patterns(row.get("sol"))
        for pattern in patterns:
            if pattern["seq"] != target:
                continue

            matched_row = dict(row)
            matched_row["sol_full"] = row.get("sol", "")
            matched_row["sol"] = pattern["raw"]
            matched_row["match"] = pattern["seq"]
            matched_row["percent"] = pattern["percent"]
            matched_row["option_label"] = (get_round_options(options, 1).get("mode") or "Simple")
            matches.append(matched_row)

    return unique_rows_by_key(
        sort_candidate_rows(matches, sequence_getter=lambda row: row.get("id", ""), prefer_score=True),
        key_getter=lambda row: (row.get("id", ""), row.get("sol", "")),
    )


def collect_2nd_matches(queue, options=None):
    round_options = get_round_options(options, 2)
    priority = round_options.get("priority", "")
    mode = (round_options.get("mode") or "Advanced").strip().lower()
    q = normalize_queue(queue)
    setup_id = kjsort(q[:4])
    matches = [
        dict(row, option_label=round_options.get("mode") or "Advanced")
        for row in load_setup_data().get("second", [])
        if str(row.get("id", "")).upper() == setup_id
    ]
    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text=priority,
            sequence_getter=lambda row: row.get("id", ""),
            prefer_score=(mode == "simple"),
        )
    )


def collect_3rd_matches(queue, options=None):
    q = normalize_queue(queue)
    round_options = get_round_options(options, 3)
    priority = round_options.get("priority", "")
    mode = (round_options.get("mode") or "Advanced").strip().lower()
    matches = []

    for row in load_setup_data().get("third", []):
        setup_id = normalize_queue(row.get("id", ""))
        if not setup_id or len(setup_id) > len(q):
            continue
        if setup_id[:1] != q[:1]:
            continue

        prefix = q[:min(len(q), len(setup_id) + 1)]
        if not counts_cover(prefix, setup_id):
            continue

        candidate = dict(row)
        candidate["match"] = setup_id
        candidate["option_label"] = round_options.get("mode") or "Advanced"
        matches.append(candidate)

    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text=priority,
            sequence_getter=lambda item: item.get("id", ""),
            prefer_score=(mode == "simple"),
        )
    )


def collect_4th_matches(queue, options=None):
    q = normalize_queue(queue)
    target = kjsort(q[:5])
    round_options = get_round_options(options, 4)
    priority = round_options.get("priority", "")
    matches = []

    for row in load_setup_data().get("fourth", []):
        combined = normalize_queue(str(row.get("pieces", "")) + str(row.get("held", "")))
        if kjsort(combined) != target:
            continue

        candidate = dict(row)
        candidate["match"] = combined
        candidate["option_label"] = priority or "ORDER"
        matches.append(candidate)

    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text=priority,
            sequence_getter=lambda item: f"{item.get('held', '')}{item.get('pieces', '')}",
            prefer_score=False,
        )
    )


def collect_5th_matches(queue, options=None):
    q = normalize_queue(queue)
    round_options = get_round_options(options, 5)
    allow_3p = bool(round_options.get("allow_3p", True))
    allow_4p = bool(round_options.get("allow_4p", True))
    allow_bd = bool(round_options.get("allow_bd", True))
    base = kjsort(q[:2])
    tail = q[2:]
    matches = []

    option_bits = []
    if allow_3p:
        option_bits.append("3P")
    if allow_4p:
        option_bits.append("4P")
    if allow_bd:
        option_bits.append("BD")
    option_label = " ".join(option_bits) or "OFF"

    for row in load_setup_data().get("fifth", []):
        if str(row.get("base", "")).upper() != base:
            continue

        piece_count = int(row.get("piece_count", 0) or 0)
        if piece_count == 3 and not allow_3p:
            continue
        if piece_count == 4 and not allow_4p:
            continue
        if piece_count >= 7 and not allow_bd:
            continue

        suffix = suffix_after_base(row.get("id", ""), base)
        if not suffix:
            continue

        window = tail[:min(len(tail), len(suffix) + 1)]
        if not counts_cover(window, suffix):
            continue

        candidate = dict(row)
        candidate["match"] = suffix
        candidate["option_label"] = option_label
        matches.append(candidate)

    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text="",
            sequence_getter=lambda item: item.get("id", ""),
            prefer_score=True,
        )
    )


def collect_6th_matches(queue, options=None):
    q = normalize_queue(queue)
    target = kjsort(q[:6])
    round_options = get_round_options(options, 6)
    priority = round_options.get("priority", "")
    specific_sol = bool(round_options.get("specific_sol", True))
    matches = []

    for row in load_setup_data().get("sixth", []):
        combined = normalize_queue(id_without_missing_piece(row.get("id", "")) + str(row.get("id2", "")))
        if kjsort(combined) != target:
            continue

        candidate = dict(row)
        candidate["match"] = combined
        candidate["option_label"] = "Specific Sol%" if specific_sol else (priority or "ORDER")
        candidate["id"] = f"{candidate.get('id', '')}-{candidate.get('id2', '')}"
        matches.append(candidate)

    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text=priority,
            sequence_getter=lambda item: item.get("match", ""),
            prefer_score=specific_sol,
        )
    )


def collect_7th_matches(queue, options=None):
    round_options = get_round_options(options, 7)
    priority = round_options.get("priority", "")
    q = normalize_queue(queue)
    setup_id = kjsort(q[:3])
    matches = [
        dict(row, option_label=priority or "DEFAULT")
        for row in load_setup_data().get("seventh", [])
        if str(row.get("id", "")).upper() == setup_id
    ]
    return unique_rows_by_key(
        sort_candidate_rows(
            matches,
            priority_text=priority,
            sequence_getter=lambda row: row.get("id", ""),
            prefer_score=True,
        )
    )


def find_setup_candidates_for_pc(queue, pc_round, options=None, limit=6):
    q = normalize_queue(queue)
    try:
        pc_round = int(pc_round)
    except (TypeError, ValueError):
        return []

    if pc_round == 1:
        rows = collect_1st_matches(q, options=options) if len(q) >= 5 else []
        setup_id = q[:5]
    elif pc_round == 2:
        rows = collect_2nd_matches(q, options=options) if len(q) >= 4 else []
        setup_id = kjsort(q[:4])
    elif pc_round == 3:
        rows = collect_3rd_matches(q, options=options) if len(q) >= 4 else []
        setup_id = q[:4]
    elif pc_round == 4:
        rows = collect_4th_matches(q, options=options) if len(q) >= 5 else []
        setup_id = kjsort(q[:5])
    elif pc_round == 5:
        rows = collect_5th_matches(q, options=options) if len(q) >= 3 else []
        setup_id = kjsort(q[:2])
    elif pc_round == 6:
        rows = collect_6th_matches(q, options=options) if len(q) >= 6 else []
        setup_id = kjsort(q[:6])
    elif pc_round == 7:
        rows = collect_7th_matches(q, options=options) if len(q) >= 3 else []
        setup_id = kjsort(q[:3])
    else:
        return []

    candidates = []
    for row in (rows or [])[: max(1, int(limit or 1))]:
        candidates.append(
            make_result(
                True,
                pc_round,
                q,
                setup_id=row.get("id", "") or setup_id,
                message=f"{pc_round}회차 PC 결과 찾음",
                row=row,
            )
        )
    return candidates


def find_by_data_key(queue, pc, data_key, take_count):
    q = normalize_queue(queue)

    if len(q) < take_count:
        return make_result(
            False,
            pc,
            q,
            message=f"{pc}회차 PC는 최소 {take_count}개 큐가 필요합니다.",
        )

    setup_id = kjsort(q[:take_count])
    rows = load_setup_data().get(data_key, [])
    by_id = {row.get("id"): row for row in rows}

    row = by_id.get(setup_id)
    if not row:
        return make_result(
            False,
            pc,
            q,
            setup_id=setup_id,
            message=f"{pc}회차 PC 결과 없음: {setup_id}",
        )

    return make_result(
        True,
        pc,
        q,
        setup_id=setup_id,
        message=f"{pc}회차 PC 결과 찾음",
        row=row,
    )


def find_best_by_data_key(
    queue,
    pc,
    data_key,
    take_count,
    priority_text="",
    sequence_getter=None,
    prefer_score=True,
):
    q = normalize_queue(queue)

    if len(q) < take_count:
        return make_result(
            False,
            pc,
            q,
            message=f"{pc}회차 PC는 최소 {take_count}개 큐가 필요합니다.",
        )

    setup_id = kjsort(q[:take_count])
    rows = load_setup_data().get(data_key, [])
    matches = [row for row in rows if str(row.get("id", "")).upper() == setup_id]

    if not matches:
        return make_result(
            False,
            pc,
            q,
            setup_id=setup_id,
            message=f"{pc}회차 PC 결과 없음: {setup_id}",
        )

    row = choose_best_row(
        matches,
        priority_text=priority_text,
        sequence_getter=sequence_getter,
        prefer_score=prefer_score,
    )
    return make_result(
        True,
        pc,
        q,
        setup_id=setup_id,
        message=f"{pc}회차 PC 결과 찾음",
        row=row,
    )


def find_1st_by_sol(queue, options=None):
    q = normalize_queue(queue)
    take_count = 5

    if len(q) < take_count:
        return make_result(
            False,
            1,
            q,
            message=f"1회차 PC는 최소 {take_count}개 큐가 필요합니다.",
        )

    target = q[:take_count]
    rows = collect_1st_matches(q, options=options)
    if rows:
        row = rows[0]
        return make_result(
            True,
            1,
            q,
            setup_id=row.get("id", ""),
            message=f"1회차 PC 결과 찾음: {row.get('match', '')} -> {row.get('id', '')}",
            row=row,
        )

    return make_result(
        False,
        1,
        q,
        setup_id=target,
        message=f"1회차 PC 결과 없음: {target}",
    )


def find_2nd(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 4:
        return make_result(False, 2, q, message="2회차 PC는 최소 4개 큐가 필요합니다.")
    rows = collect_2nd_matches(q, options=options)
    if not rows:
        setup_id = kjsort(q[:4])
        return make_result(False, 2, q, setup_id=setup_id, message=f"2회차 PC 결과 없음: {setup_id}")
    row = rows[0]
    return make_result(True, 2, q, setup_id=row.get("id", ""), message="2회차 PC 결과 찾음", row=row)


def find_3rd(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 4:
        return make_result(False, 3, q, message="3회차 PC는 최소 4개 큐가 필요합니다.")
    rows = collect_3rd_matches(q, options=options)
    row = rows[0] if rows else None
    if not row:
        return make_result(False, 3, q, message="3회차 PC 결과 없음")

    return make_result(
        True,
        3,
        q,
        setup_id=row.get("id", ""),
        message="3회차 PC 결과 찾음",
        row=row,
    )


def find_4th(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 5:
        return make_result(False, 4, q, message="4회차 PC는 최소 5개 큐가 필요합니다.")
    target = kjsort(q[:5])
    rows = collect_4th_matches(q, options=options)
    row = rows[0] if rows else None
    if not row:
        return make_result(False, 4, q, setup_id=target, message=f"4회차 PC 결과 없음: {target}")
    return make_result(
        True,
        4,
        q,
        setup_id=row.get("id", ""),
        message="4회차 PC 결과 찾음",
        row=row,
    )


def suffix_after_base(setup_id, base):
    setup_id = str(setup_id or "").upper()
    base = str(base or "").upper()
    prefix = f"{base}-"
    if setup_id.startswith(prefix):
        return normalize_queue(setup_id[len(prefix):])
    return ""


def find_5th(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 3:
        return make_result(False, 5, q, message="5회차 PC는 최소 3개 큐가 필요합니다.")
    base = kjsort(q[:2])
    rows = collect_5th_matches(q, options=options)
    row = rows[0] if rows else None
    if not row:
        return make_result(False, 5, q, setup_id=base, message=f"5회차 PC 결과 없음: {base}")
    return make_result(
        True,
        5,
        q,
        setup_id=row.get("id", ""),
        message="5회차 PC 결과 찾음",
        row=row,
    )


def id_without_missing_piece(setup_id):
    text = str(setup_id or "").upper()
    if "-" not in text:
        return normalize_queue(text)
    _missing, _sep, remainder = text.partition("-")
    return normalize_queue(remainder)


def find_6th(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 6:
        return make_result(False, 6, q, message="6회차 PC는 최소 6개 큐가 필요합니다.")
    target = kjsort(q[:6])
    rows = collect_6th_matches(q, options=options)
    row = rows[0] if rows else None
    if not row:
        return make_result(False, 6, q, setup_id=target, message=f"6회차 PC 결과 없음: {target}")
    return make_result(
        True,
        6,
        q,
        setup_id=row.get("id", ""),
        message="6회차 PC 결과 찾음",
        row=row,
    )


def find_7th(queue, options=None):
    q = normalize_queue(queue)
    if len(q) < 3:
        return make_result(False, 7, q, message="7회차 PC는 최소 3개 큐가 필요합니다.")
    rows = collect_7th_matches(q, options=options)
    if not rows:
        setup_id = kjsort(q[:3])
        return make_result(False, 7, q, setup_id=setup_id, message=f"7회차 PC 결과 없음: {setup_id}")
    row = rows[0]
    return make_result(True, 7, q, setup_id=row.get("id", ""), message="7회차 PC 결과 찾음", row=row)


def find_setup_for_pc(queue, pc_round, options=None):
    try:
        pc_round = int(pc_round)
    except (TypeError, ValueError):
        return make_result(False, None, normalize_queue(queue), message="PC 회차 인식 실패")

    if pc_round == 1:
        return find_1st_by_sol(queue, options=options)
    if pc_round == 2:
        return find_2nd(queue, options=options)
    if pc_round == 3:
        return find_3rd(queue, options=options)
    if pc_round == 4:
        return find_4th(queue, options=options)
    if pc_round == 5:
        return find_5th(queue, options=options)
    if pc_round == 6:
        return find_6th(queue, options=options)
    if pc_round == 7:
        return find_7th(queue, options=options)

    return make_result(
        False,
        pc_round,
        normalize_queue(queue),
        message=f"{pc_round}회차 PC 데이터는 아직 연결되지 않았습니다.",
    )


def find_setups(queue, options=None):
    q = normalize_queue(queue)

    return {
        "queue": q,
        "first": find_1st_by_sol(q, options=options),
        "second": find_2nd(q, options=options),
        "third": find_3rd(q, options=options),
        "fourth": find_4th(q, options=options),
        "fifth": find_5th(q, options=options),
        "sixth": find_6th(q, options=options),
        "seventh": find_7th(q, options=options),
    }


def format_setup_summary(queue, options=None):
    result = find_setups(queue, options=options)
    q = result["queue"]

    lines = [f"SETUP FINDER: queue={q or '-'}"]
    for label, key in (
        ("1st PC", "first"),
        ("2nd PC", "second"),
        ("3rd PC", "third"),
        ("4th PC", "fourth"),
        ("5th PC", "fifth"),
        ("6th PC", "sixth"),
        ("7th PC", "seventh"),
    ):
        item = result[key]
        if item["ok"]:
            setup = item["result"]
            lines.append(f"{label}: {item['id']} / SOL={setup['sol']}")
            if setup["imgur"]:
                lines.append(f"IMG: {setup['imgur']}")
            if setup["fumen"]:
                lines.append(f"FUMEN: {setup['fumen']}")
        else:
            lines.append(f"{label}: {item['message']}")

    return "\n".join(lines)


def main():
    queue = sys.argv[1] if len(sys.argv) >= 2 else "ijtzo"
    pc = int(sys.argv[2]) if len(sys.argv) >= 3 else 1

    print(format_setup_summary(queue))
    print()
    print(f"SELECTED {pc}회차:")
    selected = find_setup_for_pc(queue, pc)
    print(json.dumps(selected, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
