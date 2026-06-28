# format/gcode_reader.py
"""Ленивое чтение G-code слоёв из .sls контейнера."""
import struct
import zstandard as zstd
from typing import Dict, List, Optional, Tuple

from .constants import (
    MAGIC,
    BLOCK_GCODE_CHUNK,
    BLOCK_LAYER_INDEX,
    COMPRESS_ZSTD,
)
from .blocks import compute_block_hash


class GCodeReader:
    def __init__(self, path: str):
        self.path = path
        self._file = None
        self._toc: List[dict] = []
        self._layer_index: Dict[int, int] = {}
        self._chunk_map: List[Tuple[int, int, int]] = []  # chunk_num, offset, comp_size
        self._source_size = 0

    def __enter__(self):
        self._file = open(self.path, "rb")
        self._parse_header()
        self._parse_toc()
        self._load_layer_index()
        return self

    def __exit__(self, *args):
        if self._file:
            self._file.close()

    def _parse_header(self):
        self._file.seek(0)
        magic = self._file.read(9)
        if magic != MAGIC:
            raise ValueError(f"Неверная сигнатура файла: {magic!r}")
        self._file.read(2)
        self._file.read(1)
        self.block_count = struct.unpack("<I", self._file.read(4))[0]
        self.toc_offset = struct.unpack("<Q", self._file.read(8))[0]
        self._file.read(8)

    def _parse_toc(self):
        self._file.seek(self.toc_offset)
        self._toc = []
        for _ in range(self.block_count):
            data = self._file.read(16)
            btype, boffset, bsize = struct.unpack("<HxxIQ", data)
            self._toc.append({"type": btype, "offset": boffset, "size": bsize})

    def _read_block_raw(self, offset: int) -> Tuple[bytes, int, int]:
        self._file.seek(offset)
        hdr = self._file.read(24)
        btype, block_id, orig_size, comp_size, block_hash, algo = struct.unpack(
            "<HHIIQi", hdr
        )
        compressed = self._file.read(comp_size)
        if compute_block_hash(compressed) != block_hash:
            raise ValueError(f"Ошибка целостности блока по смещению {offset}")
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(compressed), btype, block_id

    def _load_layer_index(self):
        dctx = zstd.ZstdDecompressor()
        chunk_orig_offset = 0
        for entry in self._toc:
            if entry["type"] == BLOCK_GCODE_CHUNK:
                self._file.seek(entry["offset"])
                hdr = self._file.read(24)
                _, chunk_id, orig_size, comp_size, _, _ = struct.unpack("<HHIIQi", hdr)
                self._chunk_map.append((chunk_id, chunk_orig_offset, orig_size))
                chunk_orig_offset += orig_size
                self._source_size = chunk_orig_offset
            elif entry["type"] == BLOCK_LAYER_INDEX:
                raw, _, _ = self._read_block_raw(entry["offset"])
                count = struct.unpack_from("<I", raw, 0)[0]
                off = 4
                for _ in range(count):
                    layer_num, byte_off = struct.unpack_from("<IQ", raw, off)
                    self._layer_index[layer_num] = byte_off
                    off += 12

    def get_layer(self, layer_num: int) -> Optional[bytes]:
        if layer_num not in self._layer_index:
            return None
        start = self._layer_index[layer_num]
        sorted_layers = sorted(self._layer_index.items())
        end = self._source_size
        for num, off in sorted_layers:
            if num > layer_num:
                end = off
                break

        parts = []
        for chunk_id, chunk_start, chunk_len in self._chunk_map:
            chunk_end = chunk_start + chunk_len
            if chunk_end <= start or chunk_start >= end:
                continue
            for entry in self._toc:
                if entry["type"] != BLOCK_GCODE_CHUNK:
                    continue
                self._file.seek(entry["offset"])
                hdr = self._file.read(24)
                btype, bid, orig_size, comp_size, _, algo = struct.unpack(
                    "<HHIIQi", hdr
                )
                if bid != chunk_id:
                    continue
                compressed = self._file.read(comp_size)
                dctx = zstd.ZstdDecompressor()
                data = dctx.decompress(compressed)
                local_start = max(0, start - chunk_start)
                local_end = min(len(data), end - chunk_start)
                if local_start < local_end:
                    parts.append(data[local_start:local_end])
                break

        return b"".join(parts) if parts else None

    @property
    def layer_count(self) -> int:
        return len(self._layer_index)
