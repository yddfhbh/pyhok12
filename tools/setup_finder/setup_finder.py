import json
import sys
from pathlib import Path


VALID_PIECES = set("IJLOSTZ")

# Setup Konbini 쪽 정렬 기준.
# Hydra의 IJLOSTZ랑 다르니까 섞으면 안 됨.
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


def find_7th(queue):
    q = normalize_queue(queue)

    if len(q) < 3:
        return {
            "ok": False,
            "pc": 7,
            "queue": q,
            "message": "7th PC는 최소 3개 큐가 필요합니다.",
            "result": None,
        }

    setup_id = kjsort(q[:3])
    data = load_setup_data()

    rows = data.get("seventh", [])
    by_id = {row.get("id"): row for row in rows}

    row = by_id.get(setup_id)
    if not row:
        return {
            "ok": False,
            "pc": 7,
            "queue": q,
            "id": setup_id,
            "message": f"7th PC 결과 없음: {setup_id}",
            "result": None,
        }

    return {
        "ok": True,
        "pc": 7,
        "queue": q,
        "id": setup_id,
        "message": "7th PC 결과 찾음",
        "result": {
            "id": row.get("id", ""),
            "sol": row.get("sol", ""),
            "fumen": row.get("fumen", ""),
            "imgur": imgur_url(row.get("imgur", "")),
        },
    }


def find_setups(queue):
    q = normalize_queue(queue)

    return {
        "queue": q,
        "seventh": find_7th(q),
    }


def format_setup_summary(queue):
    result = find_setups(queue)
    q = result["queue"]
    seventh = result["seventh"]

    lines = []
    lines.append(f"SETUP FINDER: queue={q or '-'}")

    if seventh["ok"]:
        item = seventh["result"]
        lines.append(f"7th PC: {seventh['id']} / SOL={item['sol']}")
        if item["imgur"]:
            lines.append(f"IMG: {item['imgur']}")
        if item["fumen"]:
            lines.append(f"FUMEN: {item['fumen']}")
    else:
        lines.append(f"7th PC: {seventh['message']}")

    return "\n".join(lines)


def main():
    queue = sys.argv[1] if len(sys.argv) >= 2 else "ztsoitlj"
    print(format_setup_summary(queue))


if __name__ == "__main__":
    main()