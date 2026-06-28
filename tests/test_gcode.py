# tests/test_gcode.py
"""Тесты G-code, штриховки и больших файлов."""
import os
import tempfile
import pytest

from format import (
    GCodeStreamer,
    GCodeReader,
    HatchParams,
    HatchStrategy,
    PrintTechnology,
)
from format.hatch_generator import hatch_for_layer, generate_island
from format.versioning import gcode_delta_or_full, apply_gcode_patch


def _make_gcode(path: str, layers: int = 5):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(layers):
            f.write(f";LAYER:{i}\n")
            f.write(f"G0 Z{0.2 * (i + 1)}\n")
            f.write("G1 X10 Y10 F3000\n")
            f.write("G1 X90 Y10\n")


class TestGCodeStreamer:
    def test_write_and_read_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            gcode = os.path.join(tmp, "test.gcode")
            sls = os.path.join(tmp, "test.sls")
            _make_gcode(gcode, layers=8)
            stats = GCodeStreamer(sls).write_gcode(
                gcode, {"printer_type": "fdm", "name": "test"}
            )
            assert stats["layers_indexed"] >= 1
            assert os.path.getsize(sls) > 48
            with GCodeReader(sls) as reader:
                assert reader.layer_count >= 1
                layer0 = reader.get_layer(0)
                assert layer0 is not None
                assert b"LAYER" in layer0 or b"G1" in layer0


class TestHatch:
    def test_hatch_encode_decode(self):
        params = HatchParams(
            technology=PrintTechnology.SLS,
            strategy=HatchStrategy.MEANDER,
            hatch_spacing_um=100,
            rotation_deg=67.0,
            scan_speed_mms=3000.0,
            laser_power_w=150.0,
        )
        raw = params.encode()
        decoded = HatchParams.decode(raw)
        assert decoded.technology == PrintTechnology.SLS
        assert decoded.laser_power_w == 150.0

    def test_hatch_rotation_per_layer(self):
        params = HatchParams(
            technology=PrintTechnology.SLM,
            strategy=HatchStrategy.MEANDER,
            hatch_spacing_um=80,
            rotation_deg=0.0,
            scan_speed_mms=2000.0,
            layer_rotation_increment_deg=67.0,
        )
        p0 = hatch_for_layer((0, 0, 10, 10), params, 0)
        p3 = hatch_for_layer((0, 0, 10, 10), params, 3)
        assert len(p0) > 0
        coords0 = [[(point.x, point.y) for point in path.points] for path in p0]
        coords3 = [[(point.x, point.y) for point in path.points] for path in p3]
        assert coords0 != coords3

    def test_island_pattern(self):
        params = HatchParams(
            technology=PrintTechnology.SLS,
            strategy=HatchStrategy.ISLAND,
            hatch_spacing_um=100,
            rotation_deg=0.0,
            scan_speed_mms=1500.0,
        )
        paths = generate_island((0, 0, 20, 20), params, island_size_mm=5.0)
        assert len(paths) > 4


class TestGCodeVersioning:
    def test_delta_small_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old.gcode")
            new = os.path.join(tmp, "new.gcode")
            _make_gcode(old)
            _make_gcode(new)
            with open(new, "a", encoding="utf-8") as f:
                f.write("; minor tweak\n")
            decision = gcode_delta_or_full(old, new)
            assert decision["type"] in ("delta", "full")

    def test_delta_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old.gcode")
            new = os.path.join(tmp, "new.gcode")
            out = os.path.join(tmp, "out.gcode")
            _make_gcode(old)
            _make_gcode(new)
            with open(new, "a", encoding="utf-8") as f:
                f.write("; tweak\n")
            decision = gcode_delta_or_full(old, new)
            if decision["type"] == "delta":
                apply_gcode_patch(old, decision["patch"], out)
                with open(new, "rb") as f:
                    expected = f.read()
                with open(out, "rb") as f:
                    assert f.read() == expected
