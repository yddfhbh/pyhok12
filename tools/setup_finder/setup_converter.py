import json
import sys
from pathlib import Path


def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def convert_7th_data(workbook_path):
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl이 필요합니다. 터미널에서 `py -m pip install openpyxl` 실행 후 다시 시도하세요."
        ) from exc

    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=False)

    if "7th Data" not in wb.sheetnames:
        raise RuntimeError("엑셀에 '7th Data' 시트가 없습니다.")

    ws = wb["7th Data"]

    rows = []
    # 7th Data 기준:
    # B열 ID, C열 SOL, D열 FUMEN, E열 Imgur
    for row in ws.iter_rows(min_row=3, values_only=True):
        setup_id = clean_text(row[1] if len(row) > 1 else "")
        sol = row[2] if len(row) > 2 else None
        fumen = clean_text(row[3] if len(row) > 3 else "")
        imgur = clean_text(row[4] if len(row) > 4 else "")

        if not setup_id:
            continue

        rows.append(
            {
                "id": setup_id.upper(),
                "sol": sol,
                "fumen": fumen,
                "imgur": imgur,
            }
        )

    return rows


def main():
    base_dir = Path(__file__).resolve().parents[2]

    if len(sys.argv) >= 2:
        workbook_path = Path(sys.argv[1]).resolve()
    else:
        workbook_path = base_dir / "Setup Konbini.xlsx"

    if not workbook_path.exists():
        raise RuntimeError(f"엑셀 파일을 찾지 못했습니다: {workbook_path}")

    data = {
        "version": 1,
        "source": workbook_path.name,
        "seventh": convert_7th_data(workbook_path),
    }

    out_path = Path(__file__).resolve().parent / "setup_data.json"
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"완료: {out_path}")
    print(f"7th Data rows: {len(data['seventh'])}")


if __name__ == "__main__":
    main()