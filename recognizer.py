import json
import cv2
import numpy as np


PIECE_ORDER = ["I", "J", "L", "O", "S", "T", "Z", "G", "ghost"]
VISIBLE_FIELD_PIECES = set("IJLOSTZ")
PREVIEW_SHAPE_TEMPLATES = {
    "J": [[1, 0, 0], [1, 1, 1]],
    "L": [[0, 0, 1], [1, 1, 1]],
    "S": [[0, 1, 1], [1, 1, 0]],
    "T": [[0, 1, 0], [1, 1, 1]],
    "Z": [[1, 1, 0], [0, 1, 1]],
}
TETROMINO_BASE_COORDS = {
    "I": [(0, 0), (0, 1), (0, 2), (0, 3)],
    "J": [(0, 0), (1, 0), (1, 1), (1, 2)],
    "L": [(0, 2), (1, 0), (1, 1), (1, 2)],
    "O": [(0, 0), (0, 1), (1, 0), (1, 1)],
    "S": [(0, 1), (0, 2), (1, 0), (1, 1)],
    "T": [(0, 1), (1, 0), (1, 1), (1, 2)],
    "Z": [(0, 0), (0, 1), (1, 1), (1, 2)],
}


def normalize_coords(coords):
    min_r = min(r for r, _ in coords)
    min_c = min(c for _, c in coords)
    return tuple(sorted((r - min_r, c - min_c) for r, c in coords))


def rotate_coords(coords):
    return [(-c, r) for r, c in coords]


def build_tetromino_shape_signatures():
    signatures = {}

    for piece, coords in TETROMINO_BASE_COORDS.items():
        variants = set()
        rotated = list(coords)
        for _ in range(4):
            variants.add(normalize_coords(rotated))
            rotated = rotate_coords(rotated)
        signatures[piece] = variants

    return signatures


TETROMINO_SHAPE_SIGNATURES = build_tetromino_shape_signatures()
DIGIT_TEMPLATE_SIZE = (26, 40)


def normalize_digit_mask(mask):
    if mask is None or mask.size == 0:
        return np.zeros((DIGIT_TEMPLATE_SIZE[1], DIGIT_TEMPLATE_SIZE[0]), dtype=np.uint8)

    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)

    points = cv2.findNonZero(mask)
    if points is None:
        return np.zeros((DIGIT_TEMPLATE_SIZE[1], DIGIT_TEMPLATE_SIZE[0]), dtype=np.uint8)

    x, y, w, h = cv2.boundingRect(points)
    roi = mask[y:y + h, x:x + w]
    pad = max(2, int(round(max(w, h) * 0.18)))
    roi = cv2.copyMakeBorder(roi, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    resized = cv2.resize(roi, DIGIT_TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
    return (resized >= 96).astype(np.uint8)


def build_digit_templates():
    fonts = [
        cv2.FONT_HERSHEY_SIMPLEX,
        cv2.FONT_HERSHEY_DUPLEX,
        cv2.FONT_HERSHEY_TRIPLEX,
        cv2.FONT_HERSHEY_COMPLEX,
        cv2.FONT_HERSHEY_PLAIN,
    ]
    scales = [1.2, 1.4, 1.6, 1.8]
    thicknesses = [2, 3]
    templates = {}

    for digit in "0123456789":
        variants = []
        for font in fonts:
            for scale in scales:
                for thickness in thicknesses:
                    canvas = np.zeros((80, 64), dtype=np.uint8)
                    (text_w, text_h), baseline = cv2.getTextSize(digit, font, scale, thickness)
                    x = max(0, (canvas.shape[1] - text_w) // 2)
                    y = max(text_h + 2, (canvas.shape[0] + text_h) // 2 - baseline)
                    cv2.putText(
                        canvas,
                        digit,
                        (x, y),
                        font,
                        scale,
                        255,
                        thickness,
                        cv2.LINE_AA,
                    )
                    variants.append(normalize_digit_mask(canvas))
        templates[digit] = variants

    return templates


DIGIT_TEMPLATES = build_digit_templates()


def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def crop_region(img, region):
    img_h, img_w = img.shape[:2]
    x = max(0, int(region["x"]))
    y = max(0, int(region["y"]))
    w = max(1, int(region["w"]))
    h = max(1, int(region["h"]))
    x2 = min(img_w, x + w)
    y2 = min(img_h, y + h)

    return img[y:y2, x:x2]


def get_normalized_field_region(field):
    cols = int(field.get("cols", 10))
    rows = int(field.get("rows", 20))

    raw_cell_w = float(field["w"]) / cols
    raw_cell_h = float(field["h"]) / rows
    cell_size = max(raw_cell_w, raw_cell_h)

    target_w = cell_size * cols
    target_h = cell_size * rows

    return {
        "x": int(round(field["x"] - (target_w - field["w"]) / 2)),
        "y": int(round(field["y"] - (target_h - field["h"]))),
        "w": int(round(target_w)),
        "h": int(round(target_h)),
        "cols": cols,
        "rows": rows
    }


def rgb_distance(c1, c2):
    c1 = np.array(c1, dtype=np.float32)
    c2 = np.array(c2, dtype=np.float32)
    return float(np.linalg.norm(c1 - c2))


def get_inner_crop(img, ratio=0.42, x_bias=0.0, y_bias=0.0):
    """
    중앙 부분만 잘라서 반환.
    ratio=0.42면 중앙 42% 영역만 사용.
    블록 테두리/하이라이트 영향을 줄이기 위함.
    """
    h, w = img.shape[:2]

    ratio = max(0.1, min(1.0, ratio))

    inner_w = int(w * ratio)
    inner_h = int(h * ratio)

    margin_x = max(0, w - inner_w)
    margin_y = max(0, h - inner_h)

    x_bias = max(-1.0, min(1.0, x_bias))
    y_bias = max(-1.0, min(1.0, y_bias))

    x1 = int(round(margin_x * (0.5 + 0.5 * x_bias)))
    y1 = int(round(margin_y * (0.5 + 0.5 * y_bias)))
    x2 = min(w, x1 + inner_w)
    y2 = min(h, y1 + inner_h)

    return img[y1:y2, x1:x2]


def average_rgb_from_bgr(img):
    """
    OpenCV BGR 이미지를 받아 평균 RGB 반환.
    """
    if img.size == 0:
        return [0, 0, 0]

    avg_bgr = img.mean(axis=(0, 1))

    return [
        float(avg_bgr[2]),
        float(avg_bgr[1]),
        float(avg_bgr[0])
    ]


def color_spread(rgb):
    return float(max(rgb) - min(rgb))


def is_probable_colored_ghost(rgb, target_rgb, recog):
    observed_brightness = sum(rgb) / 3
    target_brightness = sum(target_rgb) / 3

    if target_brightness <= 0:
        return False

    observed_spread = color_spread(rgb)
    target_spread = color_spread(target_rgb)

    brightness_ratio = observed_brightness / target_brightness
    spread_ratio = observed_spread / target_spread if target_spread > 0 else 1.0

    max_brightness_ratio = recog.get("ghost_piece_brightness_ratio", 0.78)
    max_spread_ratio = recog.get("ghost_piece_spread_ratio", 0.72)

    return brightness_ratio <= max_brightness_ratio and spread_ratio <= max_spread_ratio


def build_preview_mask(img):
    if img.size == 0:
        return np.zeros((0, 0), dtype=np.uint8)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blurred, 52, 255, cv2.THRESH_BINARY)

    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def largest_component(mask, min_area=80):
    if mask.size == 0:
        return None

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return None

    best = None
    best_area = 0

    for idx in range(1, count):
        x, y, w, h, area = stats[idx]
        if area < min_area or area <= best_area:
            continue

        best = (idx, x, y, w, h, area)
        best_area = area

    if best is None:
        return None

    idx, x, y, w, h, _ = best
    component = (labels[y:y + h, x:x + w] == idx).astype(np.uint8)
    return component


def make_occupancy_grid(mask, rows, cols, threshold=0.2):
    if mask is None or mask.size == 0:
        return None

    h, w = mask.shape
    grid = []

    for r in range(rows):
        row = []
        for c in range(cols):
            y1 = int(r * h / rows)
            y2 = int((r + 1) * h / rows)
            x1 = int(c * w / cols)
            x2 = int((c + 1) * w / cols)

            cell = mask[y1:y2, x1:x2]
            filled = float(cell.mean()) if cell.size else 0.0
            row.append(1 if filled >= threshold else 0)

        grid.append(row)

    return grid


def classify_preview_by_shape(img):
    mask = build_preview_mask(img)
    component = largest_component(mask)
    if component is None:
        return None

    h, w = component.shape
    if h == 0 or w == 0:
        return None

    aspect = w / h

    if aspect >= 2.2:
        return "I"

    grid_2x2 = make_occupancy_grid(component, 2, 2, threshold=0.28)
    if 0.8 <= aspect <= 1.25 and grid_2x2 == [[1, 1], [1, 1]]:
        return "O"

    grid_2x3 = make_occupancy_grid(component, 2, 3, threshold=0.22)
    if grid_2x3 is None:
        return None

    best_piece = None
    best_score = -1

    for piece, template in PREVIEW_SHAPE_TEMPLATES.items():
        score = 0
        for r in range(2):
            for c in range(3):
                if grid_2x3[r][c] == template[r][c]:
                    score += 1

        if score > best_score:
            best_piece = piece
            best_score = score

    if best_score >= 5:
        return best_piece

    return None


def classify_color(rgb, config, threshold, detect_colored_ghost=False):
    """
    rgb 색을 가장 가까운 미노 색으로 분류.
    """
    colors = config["colors"]
    recog = config.get("recognition", {})

    empty_brightness = recog.get("empty_brightness", 35)

    # 너무 어두우면 빈칸으로 처리
    if sum(rgb) / 3 < empty_brightness:
        return "empty"

    best_piece = "empty"
    best_dist = 999999.0

    for piece in PIECE_ORDER:
        target = colors[piece]
        dist = rgb_distance(rgb, target)

        if dist < best_dist:
            best_dist = dist
            best_piece = piece

    if best_dist > threshold:
        return "empty"

    if detect_colored_ghost and best_piece not in ("empty", "ghost", "G"):
        target_rgb = colors[best_piece]
        if is_probable_colored_ghost(rgb, target_rgb, recog):
            return "ghost"

    return best_piece


def recognize_field(img, config):
    """
    10x20 필드 인식.
    반환값:
      2차원 배열
    """
    field = get_normalized_field_region(config["field"])
    recog = config.get("recognition", {})

    threshold = recog.get("field_threshold", 85)
    sample_ratio = recog.get("center_sample_ratio", 0.42)
    top_spawn_sample_ratio = recog.get("top_spawn_sample_ratio", max(sample_ratio, 0.52))
    top_spawn_y_bias = recog.get("top_spawn_y_bias", 0.38)

    crop = crop_region(img, field)

    cols = int(field.get("cols", 10))
    rows = int(field.get("rows", 20))

    cell_w = field["w"] / cols
    cell_h = field["h"] / rows

    board = []
    cell_rgbs = []

    for r in range(rows):
        row = []
        rgb_row = []

        for c in range(cols):
            x1 = int(round(c * cell_w))
            y1 = int(round(r * cell_h))
            x2 = int(round((c + 1) * cell_w))
            y2 = int(round((r + 1) * cell_h))

            cell = crop[y1:y2, x1:x2]
            if r == 0:
                center = get_inner_crop(cell, top_spawn_sample_ratio, y_bias=top_spawn_y_bias)
            else:
                center = get_inner_crop(cell, sample_ratio)

            avg_rgb = average_rgb_from_bgr(center)
            rgb_row.append(avg_rgb)
            piece = classify_color(avg_rgb, config, threshold)

            if piece == "empty":
                row.append(".")
            elif piece == "ghost":
                # 고스트는 필드 상황에서는 빈칸처럼 취급
                row.append(".")
            elif piece == "G":
                # garbage는 X로 표시
                row.append("X")
            else:
                row.append(piece)

        board.append(row)
        cell_rgbs.append(rgb_row)

    board = normalize_mixed_tetromino_components(board)
    return board

def board_to_text(board):
    return "\n".join("".join(row) for row in board)


def find_occupied_components(board):
    rows = len(board)
    cols = len(board[0]) if rows else 0
    visited = [[False for _ in range(cols)] for _ in range(rows)]
    components = []

    for r in range(rows):
        for c in range(cols):
            if visited[r][c]:
                continue

            piece = board[r][c]
            if piece not in VISIBLE_FIELD_PIECES:
                continue

            stack = [(r, c)]
            visited[r][c] = True
            cells = []

            while stack:
                cr, cc = stack.pop()
                cells.append((cr, cc))

                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr = cr + dr
                    nc = cc + dc

                    if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                        continue

                    if visited[nr][nc]:
                        continue

                    if board[nr][nc] not in VISIBLE_FIELD_PIECES:
                        continue

                    visited[nr][nc] = True
                    stack.append((nr, nc))

            components.append(cells)

    return components


def normalize_mixed_tetromino_components(board):
    if not board:
        return board

    cleaned = [row[:] for row in board]

    for cells in find_occupied_components(cleaned):
        if len(cells) != 4:
            continue

        piece_counts = {}
        for r, c in cells:
            piece = cleaned[r][c]
            piece_counts[piece] = piece_counts.get(piece, 0) + 1

        if len(piece_counts) != 2:
            continue

        majority_piece, majority_count = max(piece_counts.items(), key=lambda item: item[1])
        minority_piece, minority_count = min(piece_counts.items(), key=lambda item: item[1])

        if majority_count < 3 or minority_count != 1:
            continue

        if majority_piece not in TETROMINO_SHAPE_SIGNATURES:
            continue

        signature = normalize_coords(cells)
        if signature not in TETROMINO_SHAPE_SIGNATURES[majority_piece]:
            continue

        if minority_piece == majority_piece:
            continue

        for r, c in cells:
            cleaned[r][c] = majority_piece

    return cleaned


def guess_active_piece(board, max_rows=None):
    if not board:
        return None

    rows = len(board)
    cols = len(board[0]) if rows else 0

    # 아래 5칸은 이미 놓인 블럭/고스트가 많이 잡히는 영역으로 보고 active 후보에서 제외
    active_search_bottom = max(0, rows - 5)

    visited = [[False for _ in range(cols)] for _ in range(rows)]
    candidates = []

    for r in range(active_search_bottom):
        for c in range(cols):
            if visited[r][c]:
                continue

            piece = board[r][c]
            if piece not in VISIBLE_FIELD_PIECES:
                continue

            stack = [(r, c)]
            visited[r][c] = True
            cells = []

            while stack:
                cr, cc = stack.pop()
                cells.append((cr, cc))

                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr = cr + dr
                    nc = cc + dc

                    if nr < 0 or nr >= active_search_bottom or nc < 0 or nc >= cols:
                        continue
                    if visited[nr][nc]:
                        continue
                    if board[nr][nc] != piece:
                        continue

                    visited[nr][nc] = True
                    stack.append((nr, nc))

            count = len(cells)
            if 1 <= count <= 4:
                min_r = min(x[0] for x in cells)
                max_r = max(x[0] for x in cells)
                avg_r = sum(x[0] for x in cells) / count

                candidates.append(
                    {
                        "piece": piece,
                        "count": count,
                        "min_r": min_r,
                        "max_r": max_r,
                        "avg_r": avg_r,
                    }
                )

    if not candidates:
        return None

    # 4칸짜리 온전한 미노를 우선, 그다음 위쪽에 있는 후보 우선
    candidates.sort(
        key=lambda item: (
            item["count"] == 4,
            -item["count"],
            -item["min_r"],
        ),
        reverse=True,
    )

    return candidates[0]["piece"]

def find_piece_components(board, target_piece):
    """
    같은 미노 문자로 연결된 덩어리들을 찾음.
    4방향 연결 기준.
    """
    rows = len(board)
    cols = len(board[0]) if rows else 0

    visited = [[False for _ in range(cols)] for _ in range(rows)]
    components = []

    for r in range(rows):
        for c in range(cols):
            if visited[r][c]:
                continue

            if board[r][c] != target_piece:
                continue

            stack = [(r, c)]
            visited[r][c] = True
            cells = []

            while stack:
                cr, cc = stack.pop()
                cells.append((cr, cc))

                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr = cr + dr
                    nc = cc + dc

                    if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                        continue

                    if visited[nr][nc]:
                        continue

                    if board[nr][nc] != target_piece:
                        continue

                    visited[nr][nc] = True
                    stack.append((nr, nc))

            components.append({
                "piece": target_piece,
                "cells": cells,
                "count": len(cells),
                "min_r": min(x[0] for x in cells),
                "max_r": max(x[0] for x in cells),
                "avg_r": sum(x[0] for x in cells) / len(cells),
                "min_c": min(x[1] for x in cells),
                "max_c": max(x[1] for x in cells),
                "avg_c": sum(x[1] for x in cells) / len(cells)
            })

    return components


def is_probable_ghost_component(component, cell_rgbs, spread_max=60):
    spreads = [
        color_spread(cell_rgbs[r][c])
        for r, c in component["cells"]
    ]

    if not spreads:
        return False

    avg_spread = sum(spreads) / len(spreads)
    return avg_spread <= spread_max


def component_shape_signature(component):
    return tuple(sorted(
        (r - component["min_r"], c - component["min_c"])
        for r, c in component["cells"]
    ))


def component_signature_with_column_anchor(component, col_anchor):
    return tuple(sorted(
        (r - component["min_r"], c - col_anchor)
        for r, c in component["cells"]
    ))


def build_visible_shape_variants(component):
    signature = component_shape_signature(component)
    if not signature:
        return set()

    max_row = max(r for r, _ in signature)
    variants = set()

    for clipped_rows in range(max_row + 1):
        visible = [
            (r - clipped_rows, c)
            for r, c in signature
            if r >= clipped_rows
        ]

        if visible:
            variants.add(tuple(sorted(visible)))

    return variants


def is_top_spawn_piece_cell(rgb, target_rgb, recog):
    target_brightness = sum(target_rgb) / 3
    observed_brightness = sum(rgb) / 3

    if target_brightness <= 0:
        return False

    brightness_ratio = observed_brightness / target_brightness
    distance = rgb_distance(rgb, target_rgb)

    min_brightness_ratio = recog.get("top_spawn_min_brightness_ratio", 0.62)
    max_distance = recog.get("top_spawn_color_distance", 52.0)

    return brightness_ratio >= min_brightness_ratio and distance <= max_distance


def has_top_spawn_color_match(lower, cell_rgbs, colors, recog):
    rows = len(cell_rgbs)
    cols = len(cell_rgbs[0]) if rows else 0
    if rows == 0 or cols == 0:
        return False

    piece = lower["piece"]
    target_rgb = colors.get(piece)
    if not target_rgb:
        return False

    top_rows = min(rows, recog.get("top_spawn_rows", 2))
    variants = build_visible_shape_variants(lower)
    if not variants:
        return False

    colored_cells = set()
    for r in range(top_rows):
        for c in range(cols):
            if is_top_spawn_piece_cell(cell_rgbs[r][c], target_rgb, recog):
                colored_cells.add((r, c))

    if not colored_cells:
        return False

    for variant in variants:
        if len(variant) >= lower["count"]:
            continue

        max_variant_row = max(r for r, _ in variant)
        for row_offset in range(0, top_rows):
            if row_offset + max_variant_row >= top_rows:
                continue

            shifted = {
                (row_offset + r, lower["min_c"] + c)
                for r, c in variant
            }

            if shifted.issubset(colored_cells):
                return True

    return False


def component_avg_brightness(component, cell_rgbs):
    values = [
        sum(cell_rgbs[r][c]) / 3
        for r, c in component["cells"]
    ]
    return sum(values) / len(values) if values else 0.0


def component_avg_spread(component, cell_rgbs):
    values = [
        color_spread(cell_rgbs[r][c])
        for r, c in component["cells"]
    ]
    return sum(values) / len(values) if values else 0.0


def component_avg_rgb(component, cell_rgbs):
    values = [cell_rgbs[r][c] for r, c in component["cells"]]
    if not values:
        return [0.0, 0.0, 0.0]

    return [
        sum(rgb[i] for rgb in values) / len(values)
        for i in range(3)
    ]


def has_matching_ghost_layout(upper, lower, recog):
    if lower["count"] <= 0:
        return False

    if upper["min_c"] < lower["min_c"] or upper["max_c"] > lower["max_c"]:
        return False

    lower_variants = build_visible_shape_variants(lower)
    upper_signature = component_signature_with_column_anchor(upper, lower["min_c"])

    if upper_signature not in lower_variants:
        return False

    if upper["count"] == lower["count"]:
        return True

    partial_max_row = recog.get("ghost_partial_max_row", 1)
    return upper["min_r"] <= partial_max_row


def has_clear_drop_path(upper, lower, board):
    upper_cells = set(upper["cells"])
    lower_cells = set(lower["cells"])
    delta_rows = lower["min_r"] - upper["min_r"]

    for step in range(1, delta_rows):
        for r, c in upper["cells"]:
            nr = r + step
            cell = board[nr][c]

            if cell == ".":
                continue

            if (nr, c) in upper_cells:
                continue

            if (nr, c) in lower_cells:
                continue

            return False

    return True


def is_matching_ghost_pair(upper, lower, board, cell_rgbs, colors, recog):
    if not has_matching_ghost_layout(upper, lower, recog):
        return False

    if not has_clear_drop_path(upper, lower, board):
        return False

    upper_brightness = component_avg_brightness(upper, cell_rgbs)
    lower_brightness = component_avg_brightness(lower, cell_rgbs)
    upper_spread = component_avg_spread(upper, cell_rgbs)
    lower_spread = component_avg_spread(lower, cell_rgbs)
    target_rgb = colors.get(upper["piece"], [0, 0, 0])
    upper_distance = rgb_distance(component_avg_rgb(upper, cell_rgbs), target_rgb)
    lower_distance = rgb_distance(component_avg_rgb(lower, cell_rgbs), target_rgb)

    brightness_ratio = lower_brightness / upper_brightness if upper_brightness > 0 else 1.0
    spread_ratio = lower_spread / upper_spread if upper_spread > 0 else 1.0
    distance_delta = lower_distance - upper_distance

    max_relative_brightness = recog.get("ghost_pair_brightness_ratio", 0.98)
    max_relative_spread = recog.get("ghost_pair_spread_ratio", 0.98)
    min_distance_delta = recog.get("ghost_pair_distance_delta", 8.0)

    return (
        distance_delta >= min_distance_delta
        or (brightness_ratio <= max_relative_brightness and spread_ratio <= max_relative_spread)
    )


def remove_ghost_duplicates(board, cell_rgbs, config=None):
    """
    TETR.IO 고스트 제거용.

    현재 조작 중인 미노와 고스트는 보통 같은 미노가
    위쪽과 아래쪽에 동시에 잡힘.

    같은 미노 덩어리가 2개 이상 있고,
    아래쪽 덩어리가 4칸 이하이면 고스트로 보고 제거.
    """
    if not board:
        return board

    cleaned = [row[:] for row in board]
    config = config or {}
    colors = config.get("colors", {})
    recog = config.get("recognition", {})

    for piece in ["I", "J", "L", "O", "S", "T", "Z"]:
        components = find_piece_components(cleaned, piece)
        small_components = [
            comp for comp in components
            if 2 <= comp["count"] <= 4
        ]

        if len(small_components) < 1:
            continue

        small_components.sort(key=lambda comp: comp["avg_r"])

        removed = False

        for lower in reversed(small_components):
            if has_top_spawn_color_match(lower, cell_rgbs, colors, recog):
                for r, c in lower["cells"]:
                    cleaned[r][c] = "."
                removed = True
                break

        if removed:
            continue

        for lower_index in range(len(small_components) - 1, 0, -1):
            lower = small_components[lower_index]

            for upper in small_components[:lower_index]:
                if upper["max_r"] >= lower["min_r"]:
                    continue

                if not has_matching_ghost_layout(upper, lower, recog):
                    continue

                if (
                    is_matching_ghost_pair(upper, lower, cleaned, cell_rgbs, colors, recog)
                    or is_probable_ghost_component(lower, cell_rgbs)
                    or has_top_spawn_color_match(lower, cell_rgbs, colors, recog)
                ):
                    for r, c in lower["cells"]:
                        cleaned[r][c] = "."
                    removed = True
                    break

            if removed:
                break

    return cleaned


def recognize_piece_in_box(img, region, config):
    """
    HOLD / NEXT 큐처럼 미노 하나가 들어있는 박스 인식.
    영역 안에서 가장 많이 감지되는 미노 색을 반환.
    """
    recog = config.get("recognition", {})

    threshold = recog.get("box_threshold", 90)

    crop = crop_region(img, region)

    if crop.size == 0:
        return None

    # 박스 가장자리 제거
    h, w = crop.shape[:2]
    x1 = int(w * 0.12)
    x2 = int(w * 0.88)
    y1 = int(h * 0.12)
    y2 = int(h * 0.88)

    inner = crop[y1:y2, x1:x2]

    # BGR -> RGB
    inner_rgb = cv2.cvtColor(inner, cv2.COLOR_BGR2RGB)
    pixels = inner_rgb.reshape(-1, 3)

    counts = {}

    # 전부 검사하면 느릴 수 있어서 일부 픽셀만 샘플링
    for rgb in pixels[::6]:
        rgb = [float(rgb[0]), float(rgb[1]), float(rgb[2])]
        piece = classify_color(rgb, config, threshold)

        if piece in ("empty", "ghost", "G"):
            continue

        counts[piece] = counts.get(piece, 0) + 1

    if not counts:
        return classify_preview_by_shape(inner)

    return max(counts, key=counts.get)


def recognize_hold_and_queue(img, config):
    hold = recognize_piece_in_box(img, config["hold"], config)

    queue = []
    for region in config["queue"]:
        queue.append(recognize_piece_in_box(img, region, config))

    return hold, queue


def infer_pieces_counter_region(config):
    region = config.get("pieces_counter")
    if region:
        return region

    field = get_normalized_field_region(config["field"])

    # TETR.IO 좌측 통계에서 INPUTS가 아니라 PIECIES 숫자 라인을 잡는다.
    # 기존 y=0.56 부근은 INPUTS 라인에 가까워서 0을 읽는 경우가 있음.
    return {
        "x": int(round(field["x"] - field["w"] * 0.33)),
        "y": int(round(field["y"] + field["h"] * 0.61)),
        "w": int(round(field["w"] * 0.18)),
        "h": int(round(field["h"] * 0.12)),
    }


def classify_digit_mask(mask):
    normalized = normalize_digit_mask(mask)
    best_digit = None
    best_score = 0.0

    for digit, variants in DIGIT_TEMPLATES.items():
        for template in variants:
            union = np.logical_or(normalized, template).sum()
            if union == 0:
                continue

            score = np.logical_and(normalized, template).sum() / union
            if score > best_score:
                best_score = score
                best_digit = digit

    if best_score < 0.34:
        return None

    return best_digit


def recognize_pieces_count(img, config):
    region = infer_pieces_counter_region(config)
    crop = crop_region(img, region)
    if crop.size == 0:
        return None

    recog = config.get("recognition", {})
    top_ratio = recog.get("pieces_counter_top_ratio", 1.0)
    threshold = recog.get("pieces_counter_threshold", 170)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scan_h = max(1, int(round(gray.shape[0] * top_ratio)))
    gray = gray[:scan_h, :]
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, mask = cv2.threshold(blurred, threshold, 255, cv2.THRESH_BINARY)

    kernel = np.ones((2, 2), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.dilate(mask, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    min_area = mask.shape[0] * mask.shape[1] * 0.015
    min_height = mask.shape[0] * 0.38

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < min_area:
            continue
        if h < min_height:
            continue
        boxes.append((x, y, w, h))

    if not boxes:
        return None

    boxes.sort(key=lambda item: item[0])
    digits = []

    for x, y, w, h in boxes[:3]:
        digit_mask = mask[y:y + h, x:x + w]
        digit = classify_digit_mask(digit_mask)
        if digit is None:
            continue
        digits.append(digit)

    if not digits:
        return None

    try:
        return int("".join(digits))
    except ValueError:
        return None


def calculate_pc_round(pieces_count):
    if pieces_count is None or pieces_count < 0:
        return None

    return (pieces_count // 10) % 7 + 1


def recognize_all(img, config):
    board = recognize_field(img, config)
    hold, queue = recognize_hold_and_queue(img, config)
    active_guess = guess_active_piece(board)
    pieces_count = recognize_pieces_count(img, config)
    pc_round = calculate_pc_round(pieces_count)

    return {
        "board": board,
        "active_guess": active_guess,
        "hold": hold,
        "queue": queue,
        "pieces_count": pieces_count,
        "pc_round": pc_round,
    }
