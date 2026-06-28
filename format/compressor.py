# format/compressor.py
import lz4.frame
import zstandard as zstd
import lzma
from .constants import COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD, COMPRESS_LZMA


def compress(data: bytes, algorithm: int) -> bytes:
    """Сжать данные выбранным алгоритмом."""
    if algorithm == COMPRESS_NONE:
        return data
    elif algorithm == COMPRESS_LZ4:
        return lz4.frame.compress(data)
    elif algorithm == COMPRESS_ZSTD:
        cctx = zstd.ZstdCompressor(level=3)
        return cctx.compress(data)
    elif algorithm == COMPRESS_LZMA:
        return lzma.compress(data, preset=6)
    else:
        raise ValueError(f"Неизвестный алгоритм компрессии: {algorithm}")


def decompress(data: bytes, algorithm: int) -> bytes:
    """Распаковать данные."""
    if algorithm == COMPRESS_NONE:
        return data
    elif algorithm == COMPRESS_LZ4:
        return lz4.frame.decompress(data)
    elif algorithm == COMPRESS_ZSTD:
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    elif algorithm == COMPRESS_LZMA:
        return lzma.decompress(data)
    else:
        raise ValueError(f"Неизвестный алгоритм компрессии: {algorithm}")
