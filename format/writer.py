# format/writer.py
import struct
import io
import time
from typing import List, Dict, Any

from .constants import (
    MAGIC,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    BLOCK_METADATA,
    BLOCK_LAYER,
    COMPRESS_ZSTD,
)
from .blocks import LayerData, encode_metadata, compute_block_hash
from .compressor import compress


class SliceWriter:
    def __init__(self, path: str, compression: int = COMPRESS_ZSTD):
        self.path = path
        self.compression = compression
        self._metadata: Dict[str, Any] = {}
        self._layers: List[LayerData] = []

    def set_metadata(self, meta: dict) -> None:
        self._metadata = meta

    def add_layer(self, layer: LayerData) -> None:
        self._layers.append(layer)

    def save(self) -> None:
        buf = io.BytesIO()
        buf.write(b"\x00" * 48)

        toc_entries = []

        meta_raw = encode_metadata(
            {
                **self._metadata,
                "layer_count": len(self._layers),
                "created_at": int(time.time()),
                "compression": self.compression,
            }
        )
        meta_compressed = compress(meta_raw, self.compression)
        meta_hash = compute_block_hash(meta_compressed)

        offset = buf.tell()
        buf.write(
            struct.pack(
                "<HHIIQi",
                BLOCK_METADATA,
                0,
                len(meta_raw),
                len(meta_compressed),
                meta_hash,
                self.compression,
            )
        )
        buf.write(meta_compressed)
        toc_entries.append((BLOCK_METADATA, offset, len(meta_compressed)))

        for layer in self._layers:
            layer_raw = layer.encode()
            layer_compressed = compress(layer_raw, self.compression)
            layer_hash = compute_block_hash(layer_compressed)

            offset = buf.tell()
            buf.write(
                struct.pack(
                    "<HHIIQi",
                    BLOCK_LAYER,
                    0,
                    len(layer_raw),
                    len(layer_compressed),
                    layer_hash,
                    self.compression,
                )
            )
            buf.write(layer_compressed)
            toc_entries.append((BLOCK_LAYER, offset, len(layer_compressed)))

        toc_offset = buf.tell()
        toc_raw = b""
        for btype, boffset, bsize in toc_entries:
            toc_raw += struct.pack("<HxxIQ", btype, boffset, bsize)
        buf.write(toc_raw)

        buf.seek(0)
        header = MAGIC
        header += struct.pack("<BB", FORMAT_MAJOR, FORMAT_MINOR)
        header += struct.pack("<B", self.compression)
        header += struct.pack("<I", len(toc_entries))
        header += struct.pack("<Q", toc_offset)
        header += struct.pack("<Q", int(time.time()))
        header += b"\x00" * (48 - len(header))
        buf.write(header)

        with open(self.path, "wb") as f:
            f.write(buf.getvalue())

        print(
            f"[SliceWriter] Сохранено: {self.path} "
            f"({len(self._layers)} слоёв, {len(buf.getvalue())} байт)"
        )
