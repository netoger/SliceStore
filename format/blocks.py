# format/blocks.py
import struct
import xxhash
import msgpack
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List


@dataclass
class Point:
    """Точка в микрометрах (int32)."""
    x: int
    y: int

    def encode(self) -> bytes:
        return struct.pack("<ii", self.x, self.y)

    @classmethod
    def decode(cls, data: bytes, offset: int) -> "Point":
        x, y = struct.unpack_from("<ii", data, offset)
        return cls(x, y)


@dataclass
class Contour:
    """Замкнутый полигон (периметр слоя)."""
    points: List[Point] = field(default_factory=list)

    def encode(self) -> bytes:
        result = struct.pack("<I", len(self.points))
        if not self.points:
            return result
        result += self.points[0].encode()
        for i in range(1, len(self.points)):
            dx = self.points[i].x - self.points[i - 1].x
            dy = self.points[i].y - self.points[i - 1].y
            result += struct.pack("<ii", dx, dy)
        return result

    @classmethod
    def decode(cls, data: bytes, offset: int):
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        points = []
        if count == 0:
            return cls(points), offset
        p = Point.decode(data, offset)
        points.append(p)
        offset += 8
        for _ in range(count - 1):
            dx, dy = struct.unpack_from("<ii", data, offset)
            offset += 8
            points.append(Point(points[-1].x + dx, points[-1].y + dy))
        return cls(points), offset


@dataclass
class InfillPath:
    """Траектория штриховки (открытая полилиния)."""
    points: List[Point] = field(default_factory=list)
    speed_mms: float = 60.0

    def encode(self) -> bytes:
        result = struct.pack("<I", len(self.points))
        result += struct.pack("<f", self.speed_mms)
        if not self.points:
            return result
        result += self.points[0].encode()
        for i in range(1, len(self.points)):
            dx = self.points[i].x - self.points[i - 1].x
            dy = self.points[i].y - self.points[i - 1].y
            result += struct.pack("<ii", dx, dy)
        return result

    @classmethod
    def decode(cls, data: bytes, offset: int):
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        speed = struct.unpack_from("<f", data, offset)[0]
        offset += 4
        points = []
        if count > 0:
            p = Point.decode(data, offset)
            points.append(p)
            offset += 8
            for _ in range(count - 1):
                dx, dy = struct.unpack_from("<ii", data, offset)
                offset += 8
                points.append(Point(points[-1].x + dx, points[-1].y + dy))
        return cls(points, speed), offset


@dataclass
class LayerData:
    """Данные одного слоя."""
    index: int
    height_um: int
    contours: List[Contour] = field(default_factory=list)
    infill_paths: List[InfillPath] = field(default_factory=list)
    extruder_temp: float = 210.0
    bed_temp: float = 60.0

    def add_contour(self, points_mm: List[tuple]) -> None:
        pts = [Point(int(x * 1000), int(y * 1000)) for x, y in points_mm]
        self.contours.append(Contour(pts))

    def add_infill(self, points_mm: List[tuple], speed_mms: float = 60.0) -> None:
        pts = [Point(int(x * 1000), int(y * 1000)) for x, y in points_mm]
        self.infill_paths.append(InfillPath(pts, speed_mms))

    def encode(self) -> bytes:
        result = struct.pack("<IiI", self.index, self.height_um, len(self.contours))
        result += struct.pack("<ff", self.extruder_temp, self.bed_temp)
        for c in self.contours:
            result += c.encode()
        result += struct.pack("<I", len(self.infill_paths))
        for ip in self.infill_paths:
            result += ip.encode()
        return result

    @classmethod
    def decode(cls, data: bytes) -> "LayerData":
        offset = 0
        index, height_um, n_contours = struct.unpack_from("<IiI", data, offset)
        offset += 12
        e_temp, b_temp = struct.unpack_from("<ff", data, offset)
        offset += 8
        contours = []
        for _ in range(n_contours):
            c, offset = Contour.decode(data, offset)
            contours.append(c)
        n_infill = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        infill = []
        for _ in range(n_infill):
            ip, offset = InfillPath.decode(data, offset)
            infill.append(ip)
        obj = cls(index=index, height_um=height_um, extruder_temp=e_temp, bed_temp=b_temp)
        obj.contours = contours
        obj.infill_paths = infill
        return obj


def compute_block_hash(data: bytes) -> int:
    return xxhash.xxh64(data).intdigest()


def encode_metadata(meta: dict) -> bytes:
    return msgpack.packb(meta, use_bin_type=True)


def decode_metadata(data: bytes) -> dict:
    return msgpack.unpackb(data, raw=False)


class PrintTechnology(IntEnum):
    FDM = 1
    SLS = 2
    SLM = 3


class HatchStrategy(IntEnum):
    MEANDER = 1
    ISLAND = 2
    STRIPE = 3
    GYROID = 4
    HONEYCOMB = 5


@dataclass
class HatchParams:
    technology: PrintTechnology
    strategy: HatchStrategy
    hatch_spacing_um: int
    rotation_deg: float
    scan_speed_mms: float
    laser_power_w: float = 0.0
    infill_density_pct: float = 100.0
    layer_rotation_increment_deg: float = 67.0

    def encode(self) -> bytes:
        return struct.pack(
            "<BBIffff",
            int(self.technology),
            int(self.strategy),
            self.hatch_spacing_um,
            self.rotation_deg,
            self.scan_speed_mms,
            self.laser_power_w,
            self.infill_density_pct,
        )

    @classmethod
    def decode(cls, data: bytes, offset: int = 0) -> "HatchParams":
        tech, strat, spacing, rot, speed, power, density = struct.unpack_from(
            "<BBIffff", data, offset
        )
        return cls(
            technology=PrintTechnology(tech),
            strategy=HatchStrategy(strat),
            hatch_spacing_um=spacing,
            rotation_deg=rot,
            scan_speed_mms=speed,
            laser_power_w=power,
            infill_density_pct=density,
        )
