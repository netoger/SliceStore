# format/hatch_generator.py
"""Генератор паттернов штриховки по параметрам."""
import math
from typing import List, Tuple

from .blocks import HatchParams, HatchStrategy, Point, InfillPath


def generate_meander(
    bbox: Tuple[float, float, float, float],
    params: HatchParams,
) -> List[InfillPath]:
    x_min, y_min, x_max, y_max = bbox
    spacing_mm = params.hatch_spacing_um / 1000.0
    angle_rad = math.radians(params.rotation_deg)
    paths = []
    y = y_min
    left_to_right = True

    while y <= y_max:
        x_start = x_min if left_to_right else x_max
        x_end = x_max if left_to_right else x_min

        def rotate(x, y_coord):
            rx = x * math.cos(angle_rad) - y_coord * math.sin(angle_rad)
            ry = x * math.sin(angle_rad) + y_coord * math.cos(angle_rad)
            return rx, ry

        p1 = rotate(x_start, y)
        p2 = rotate(x_end, y)
        paths.append(
            InfillPath(
                points=[
                    Point(int(p1[0] * 1000), int(p1[1] * 1000)),
                    Point(int(p2[0] * 1000), int(p2[1] * 1000)),
                ],
                speed_mms=params.scan_speed_mms,
            )
        )
        y += spacing_mm
        left_to_right = not left_to_right

    return paths


def generate_island(
    bbox: Tuple[float, float, float, float],
    params: HatchParams,
    island_size_mm: float = 5.0,
) -> List[InfillPath]:
    x_min, y_min, x_max, y_max = bbox
    all_paths = []
    island_row = 0
    y = y_min
    while y < y_max:
        x = x_min
        island_col = 0
        while x < x_max:
            local_angle = params.rotation_deg + (
                0 if (island_row + island_col) % 2 == 0 else 90
            )
            island_params = HatchParams(
                technology=params.technology,
                strategy=HatchStrategy.MEANDER,
                hatch_spacing_um=params.hatch_spacing_um,
                rotation_deg=local_angle,
                scan_speed_mms=params.scan_speed_mms,
                laser_power_w=params.laser_power_w,
            )
            island_bbox = (
                x,
                y,
                min(x + island_size_mm, x_max),
                min(y + island_size_mm, y_max),
            )
            all_paths.extend(generate_meander(island_bbox, island_params))
            x += island_size_mm
            island_col += 1
        y += island_size_mm
        island_row += 1
    return all_paths


def hatch_for_layer(
    bbox: Tuple[float, float, float, float],
    params: HatchParams,
    layer_index: int,
) -> List[InfillPath]:
    rotated_params = HatchParams(
        technology=params.technology,
        strategy=params.strategy,
        hatch_spacing_um=params.hatch_spacing_um,
        rotation_deg=(
            params.rotation_deg + layer_index * params.layer_rotation_increment_deg
        )
        % 360,
        scan_speed_mms=params.scan_speed_mms,
        laser_power_w=params.laser_power_w,
        infill_density_pct=params.infill_density_pct,
        layer_rotation_increment_deg=params.layer_rotation_increment_deg,
    )
    if params.strategy == HatchStrategy.ISLAND:
        return generate_island(bbox, rotated_params)
    return generate_meander(bbox, rotated_params)
