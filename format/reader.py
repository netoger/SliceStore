# format/reader.py
import struct
from typing import List, Optional, Dict, Any

from .constants import MAGIC, BLOCK_METADATA, BLOCK_LAYER
from .blocks import LayerData, decode_metadata, compute_block_hash
from .compressor import decompress


class SliceReader:
    def __init__(self, path: str):
        self.path = path
        self._file = None
        self._toc: List[Dict] = []
        self._metadata: Optional[Dict] = None
        self._layer_cache: Dict[int, LayerData] = {}

    def __enter__(self):
        self._file = open(self.path, "rb")
        self._parse_header()
        self._parse_toc()
        return self

    def __exit__(self, *args):
        if self._file:
            self._file.close()

    def _parse_header(self):
        self._file.seek(0)
        magic = self._file.read(9)
        if magic != MAGIC:
            raise ValueError(f"Неверная сигнатура файла: {magic!r}")
        major, minor = struct.unpack("<BB", self._file.read(2))
        self.format_version = (major, minor)
        self.compression = struct.unpack("<B", self._file.read(1))[0]
        self.block_count = struct.unpack("<I", self._file.read(4))[0]
        self.toc_offset = struct.unpack("<Q", self._file.read(8))[0]
        self.created_at = struct.unpack("<Q", self._file.read(8))[0]

    def _parse_toc(self):
        self._file.seek(self.toc_offset)
        self._toc = []
        for _ in range(self.block_count):
            data = self._file.read(16)
            btype, boffset, bsize = struct.unpack("<HxxIQ", data)
            self._toc.append(
                {"type": btype, "offset": boffset, "compressed_size": bsize}
            )

    def _read_block(self, offset: int) -> bytes:
        self._file.seek(offset)
        hdr = self._file.read(24)
        btype, _, orig_size, comp_size, block_hash, algo = struct.unpack(
            "<HHIIQi", hdr
        )
        compressed = self._file.read(comp_size)
        actual_hash = compute_block_hash(compressed)
        if actual_hash != block_hash:
            raise ValueError(f"Ошибка целостности блока по смещению {offset}!")
        return decompress(compressed, algo)

    def get_metadata(self) -> Dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        for entry in self._toc:
            if entry["type"] == BLOCK_METADATA:
                raw = self._read_block(entry["offset"])
                self._metadata = decode_metadata(raw)
                return self._metadata
        return {}

    def get_layer(self, index: int) -> Optional[LayerData]:
        if index in self._layer_cache:
            return self._layer_cache[index]

        layer_entries = [e for e in self._toc if e["type"] == BLOCK_LAYER]
        if index >= len(layer_entries):
            return None

        raw = self._read_block(layer_entries[index]["offset"])
        layer = LayerData.decode(raw)
        self._layer_cache[index] = layer
        return layer

    @property
    def layer_count(self) -> int:
        return sum(1 for e in self._toc if e["type"] == BLOCK_LAYER)
