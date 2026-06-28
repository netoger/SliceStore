# format/gcode_streamer.py
"""Потоковая запись G-code в .sls контейнер."""
import struct
import re
import time
import zstandard as zstd
from pathlib import Path
from typing import Dict, List, Tuple

from .constants import (
    CHUNK_SIZE,
    BLOCK_GCODE_CHUNK,
    BLOCK_LAYER_INDEX,
    BLOCK_METADATA,
    COMPRESS_ZSTD,
    MAGIC,
    FORMAT_MAJOR,
    FORMAT_MINOR,
)
from .blocks import encode_metadata, compute_block_hash

LAYER_COMMENT_RE = re.compile(
    rb";(?:LAYER|layer|Layer)[:\s]+(\d+)"
    rb"|;Z:[\d.]+\s"
    rb"|G0\s.*?Z([\d.]+)",
    re.MULTILINE,
)


class GCodeStreamer:
    def __init__(self, output_path: str, chunk_size: int = CHUNK_SIZE):
        self.output_path = output_path
        self.chunk_size = chunk_size

    def write_gcode(self, gcode_path: str, metadata: dict) -> dict:
        gcode_path = Path(gcode_path)
        file_size = gcode_path.stat().st_size

        cctx = zstd.ZstdCompressor(level=3, threads=-1)
        layer_index: Dict[int, int] = {}
        total_compressed = 0
        gcode_offset = 0
        chunk_num = 0

        with open(gcode_path, "rb") as src, open(self.output_path, "wb") as dst:
            dst.write(b"\x00" * 48)
            toc_entries: List[Tuple[int, int, int]] = []

            meta_payload = encode_metadata(
                {
                    **metadata,
                    "source_file": gcode_path.name,
                    "source_size_bytes": file_size,
                    "format": "gcode",
                }
            )
            meta_compressed = cctx.compress(meta_payload)
            blk_offset = dst.tell()
            dst.write(
                struct.pack(
                    "<HHIIQi",
                    BLOCK_METADATA,
                    0,
                    len(meta_payload),
                    len(meta_compressed),
                    compute_block_hash(meta_compressed),
                    COMPRESS_ZSTD,
                )
            )
            dst.write(meta_compressed)
            toc_entries.append((BLOCK_METADATA, blk_offset, len(meta_compressed)))

            buf = b""
            while True:
                read = src.read(self.chunk_size - len(buf))
                if not read and not buf:
                    break
                buf += read

                for m in LAYER_COMMENT_RE.finditer(buf):
                    layer_num_str = m.group(1)
                    if layer_num_str:
                        layer_index[int(layer_num_str)] = gcode_offset + m.start()

                if len(buf) >= self.chunk_size or not read:
                    compressed_chunk = cctx.compress(buf)
                    blk_offset = dst.tell()
                    dst.write(
                        struct.pack(
                            "<HHIIQi",
                            BLOCK_GCODE_CHUNK,
                            chunk_num,
                            len(buf),
                            len(compressed_chunk),
                            compute_block_hash(compressed_chunk),
                            COMPRESS_ZSTD,
                        )
                    )
                    dst.write(compressed_chunk)
                    toc_entries.append(
                        (BLOCK_GCODE_CHUNK, blk_offset, len(compressed_chunk))
                    )
                    total_compressed += len(compressed_chunk)
                    gcode_offset += len(buf)
                    chunk_num += 1
                    buf = b""

                if not read:
                    break

            index_payload = struct.pack("<I", len(layer_index))
            for layer_num in sorted(layer_index):
                index_payload += struct.pack("<IQ", layer_num, layer_index[layer_num])
            index_compressed = cctx.compress(index_payload)
            blk_offset = dst.tell()
            dst.write(
                struct.pack(
                    "<HHIIQi",
                    BLOCK_LAYER_INDEX,
                    0,
                    len(index_payload),
                    len(index_compressed),
                    compute_block_hash(index_compressed),
                    COMPRESS_ZSTD,
                )
            )
            dst.write(index_compressed)
            toc_entries.append((BLOCK_LAYER_INDEX, blk_offset, len(index_compressed)))

            toc_offset = dst.tell()
            for btype, boff, bsize in toc_entries:
                dst.write(struct.pack("<HxxIQ", btype, boff, bsize))

            dst.seek(0)
            header = MAGIC
            header += struct.pack("<BB", FORMAT_MAJOR, FORMAT_MINOR)
            header += struct.pack("<B", COMPRESS_ZSTD)
            header += struct.pack("<I", len(toc_entries))
            header += struct.pack("<Q", toc_offset)
            header += struct.pack("<Q", int(time.time()))
            header += b"\x00" * (48 - len(header))
            dst.write(header)

        ratio = total_compressed / file_size if file_size else 1
        return {
            "source_size_gb": file_size / 1e9,
            "compressed_size_gb": total_compressed / 1e9,
            "ratio": ratio,
            "chunks": chunk_num,
            "layers_indexed": len(layer_index),
            "layer_index": layer_index,
        }
