import json
import sys
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
            "fumen": row.get("fumen", ""),
            "imgur": imgur_url(row.get("imgur", "")),
        }

    return {
        "ok": ok,
        "pc": pc,
        "queue": queue,
        "id": setup_id,
        "message": message,
        "result": result,
    }


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
    data = load_setup_data()

    rows = data.get(data_key, [])
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


def find_1st(queue):
    # 1st PC는 우선 앞 5개 기준으로 연결.
    # 만약 결과가 너무 안 나오면 6개 기준으로 바꿔야 함.
    return find_by_data_key(queue, pc=1, data_key="first", take_count=6)


def find_7th(queue):
    return find_by_data_key(queue, pc=7, data_key="seventh", take_count=3)


def find_setup_for_pc(queue, pc_round):
    try:
        pc_round = int(pc_round)
    except (TypeError, ValueError):
        return make_result(False, None, normalize_queue(queue), message="PC 회차 인식 실패")

    if pc_round == 1:
        return find_1st(queue)

    if pc_round == 7:
        return find_7th(queue)

    return make_result(
        False,
        pc_round,
        normalize_queue(queue),
        message=f"{pc_round}회차 PC 데이터는 아직 연결되지 않았습니다.",
    )


def find_setups(queue):
    q = normalize_queue(queue)

    return {
        "queue": q,
        "first": find_1st(q),
        "seventh": find_7th(q),
    }


def format_setup_summary(queue):
    result = find_setups(queue)
    q = result["queue"]
    first = result["first"]
    seventh = result["seventh"]

    lines = []
    lines.append(f"SETUP FINDER: queue={q or '-'}")

    for label, item in (("1st PC", first), ("7th PC", seventh)):
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