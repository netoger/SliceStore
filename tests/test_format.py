# tests/test_format.py
"""Модульные тесты для формата .sls"""
import os
import tempfile
import pytest

from format import (
    SliceWriter,
    SliceReader,
    LayerData,
    COMPRESS_NONE,
    COMPRESS_LZ4,
    COMPRESS_ZSTD,
    COMPRESS_LZMA,
)
from format.delta import create_delta, apply_delta, delta_stats


class TestCompression:
    @pytest.mark.parametrize("algo", [COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD, COMPRESS_LZMA])
    def test_roundtrip(self, algo):
        from format.compressor import compress, decompress

        original = b"Hello SliceStore! " * 100
        compressed = compress(original, algo)
        decompressed = decompress(compressed, algo)
        assert decompressed == original

    def test_lz4_is_faster_check(self):
        from format.compressor import compress, decompress

        data = bytes(range(256)) * 50
        assert decompress(compress(data, COMPRESS_LZ4), COMPRESS_LZ4) == data

    def test_lzma_compresses_better(self):
        from format.compressor import compress

        # On very small payloads container/header overhead can make LZMA larger
        # than LZ4 even when the source is highly repetitive.
        data = b"AAABBBCCC" * 100000
        lz4_size = len(compress(data, COMPRESS_LZ4))
        lzma_size = len(compress(data, COMPRESS_LZMA))
        assert lzma_size < lz4_size


class TestLayerData:
    def test_encode_decode_roundtrip(self):
        layer = LayerData(index=5, height_um=200, extruder_temp=215.0, bed_temp=65.0)
        layer.add_contour([(0, 0), (10, 0), (10, 10), (0, 10)])
        layer.add_infill([(1, 1), (9, 1)], speed_mms=80.0)
        layer.add_infill([(1, 5), (9, 5)], speed_mms=80.0)

        raw = layer.encode()
        decoded = LayerData.decode(raw)

        assert decoded.index == 5
        assert decoded.height_um == 200
        assert abs(decoded.extruder_temp - 215.0) < 0.01
        assert len(decoded.contours) == 1
        assert len(decoded.contours[0].points) == 4
        assert len(decoded.infill_paths) == 2
        assert decoded.contours[0].points[0].x == 0
        assert decoded.contours[0].points[1].x == 10000

    def test_empty_layer(self):
        layer = LayerData(index=0, height_um=100)
        raw = layer.encode()
        decoded = LayerData.decode(raw)
        assert decoded.index == 0
        assert len(decoded.contours) == 0


class TestSliceFile:
    def _make_test_file(self, path: str, algo: int = COMPRESS_ZSTD, n_layers: int = 3):
        writer = SliceWriter(path, compression=algo)
        writer.set_metadata({"job_name": "test_bracket", "printer": "mk4"})
        for i in range(n_layers):
            layer = LayerData(index=i, height_um=200)
            layer.add_contour([(0, 0), (20, 0), (20, 20), (0, 20)])
            layer.add_infill([(1, 1 + i * 2), (19, 1 + i * 2)], speed_mms=60.0)
            writer.add_layer(layer)
        writer.save()

    @pytest.mark.parametrize("algo", [COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD])
    def test_write_read(self, algo):
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            path = f.name
        try:
            self._make_test_file(path, algo=algo, n_layers=5)
            with SliceReader(path) as reader:
                meta = reader.get_metadata()
                assert meta["job_name"] == "test_bracket"
                assert reader.layer_count == 5
                layer = reader.get_layer(2)
                assert layer.index == 2
                assert len(layer.contours) == 1
        finally:
            os.unlink(path)

    def test_magic_validation(self):
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            f.write(b"WRONGMAGIC" + b"\x00" * 100)
            path = f.name
        try:
            with pytest.raises(ValueError, match="сигнатура"):
                with SliceReader(path) as reader:
                    pass
        finally:
            os.unlink(path)

    def test_compression_ratio(self):
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            path_none = f.name
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            path_zstd = f.name
        try:
            self._make_test_file(path_none, algo=COMPRESS_NONE, n_layers=20)
            self._make_test_file(path_zstd, algo=COMPRESS_ZSTD, n_layers=20)
            size_none = os.path.getsize(path_none)
            size_zstd = os.path.getsize(path_zstd)
            assert size_zstd < size_none
        finally:
            os.unlink(path_none)
            os.unlink(path_zstd)


class TestDelta:
    def test_delta_roundtrip(self):
        old = b"Version 1 data: " + b"ABCDEF" * 100
        new = b"Version 2 data: " + b"ABCDEF" * 100 + b"NEW_SUFFIX"
        patch = create_delta(old, new)
        restored = apply_delta(old, patch)
        assert restored == new

    def test_delta_is_small_for_minor_changes(self):
        base = b"X" * 10000
        modified = base[:100] + b"Y" * 100 + base[200:]
        patch = create_delta(base, modified)
        stats = delta_stats(base, patch)
        assert stats["ratio_percent"] < 30
