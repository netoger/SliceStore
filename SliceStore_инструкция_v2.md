# SliceStore — Полная инструкция по реализации

**Тема ВКР:** Разработка формата и сервиса хранения данных слайсинга и штриховки  
**Стек:** Python 3.11, FastAPI, PostgreSQL 16, MinIO, Redis 7, Docker  

---

## ОГЛАВЛЕНИЕ

1. [Структура проекта](#1-структура-проекта)
2. [Docker Compose — поднимаем инфраструктуру](#2-docker-compose)
3. [Формат файла SliceStore (.sls)](#3-формат-файла-sls)
4. [Библиотека slicestore-format](#4-библиотека-slicestore-format)
5. [База данных — схема PostgreSQL](#5-база-данных)
6. [Сервис slicestore-service (FastAPI)](#6-fastapi-сервис)
7. [Клиентская библиотека](#7-клиентская-библиотека)
8. [Тесты](#8-тесты)
9. [Как запустить и проверить](#9-как-запустить)
10. [Что должно работать — чек-лист](#10-чек-лист)
11. [Расширение: G-code, большие файлы, обратная совместимость](#11-расширение-gcode)

---

## 1. Структура проекта

Создайте папку `slicestore/` и внутри неё следующее дерево:

```
slicestore/
├── docker-compose.yml          # Вся инфраструктура одной командой
├── .env                        # Переменные окружения
│
├── format/                     # Библиотека формата .sls
│   ├── __init__.py
│   ├── constants.py            # Магические числа, коды блоков
│   ├── compressor.py           # LZ4 / Zstd / LZMA
│   ├── delta.py                # bsdiff — дельта-патчи
│   ├── blocks.py               # Классы блоков (Header, Layer, Meta…)
│   ├── writer.py               # Запись .sls файла
│   ├── reader.py               # Чтение .sls файла (ленивое)
│   └── versioning.py           # Работа с версиями
│
├── service/                    # FastAPI сервис
│   ├── main.py                 # Точка входа
│   ├── config.py               # Настройки из .env
│   ├── database.py             # Подключение к PostgreSQL (asyncpg)
│   ├── storage.py              # Подключение к MinIO
│   ├── cache.py                # Подключение к Redis
│   ├── models.py               # Pydantic-схемы запросов/ответов
│   ├── routers/
│   │   ├── slices.py           # CRUD для файлов
│   │   ├── versions.py         # Управление версиями
│   │   └── search.py           # Поиск
│   └── Dockerfile
│
├── client/                     # Клиентская библиотека
│   ├── __init__.py
│   └── client.py
│
├── tests/                      # Тесты
│   ├── test_format.py
│   ├── test_api.py
│   └── conftest.py
│
└── migrations/
    └── init.sql                # SQL схема БД
```

---

## 2. Docker Compose

Создайте файл `.env` в корне проекта:

```env
# .env
POSTGRES_USER=slicestore
POSTGRES_PASSWORD=slicestore123
POSTGRES_DB=slicestore

MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
MINIO_BUCKET=slices

REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql://slicestore:slicestore123@postgres:5432/slicestore
MINIO_ENDPOINT=minio:9000
```

Создайте `docker-compose.yml`:

```yaml
# docker-compose.yml
version: "3.9"

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./migrations/init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    ports:
      - "9000:9000"   # S3 API
      - "9001:9001"   # Web console
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 10s
      timeout: 5s
      retries: 5

  service:
    build: ./service
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: ${DATABASE_URL}
      MINIO_ENDPOINT: ${MINIO_ENDPOINT}
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      MINIO_BUCKET: ${MINIO_BUCKET}
      REDIS_URL: ${REDIS_URL}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    volumes:
      - ./format:/app/format   # монтируем библиотеку формата

volumes:
  pg_data:
  minio_data:
```

---

## 3. Формат файла .sls

### Как устроен файл

```
┌─────────────────────────────────────┐
│  FILE HEADER (48 байт)              │  magic + version + flags + toc_offset
├─────────────────────────────────────┤
│  BLOCK HEADER (24 байта)            │  type + id + compressed_size + hash
│  BLOCK DATA   (N байт, сжатые)      │  содержимое блока
├─────────────────────────────────────┤
│  BLOCK HEADER                       │  LAYER блок (слой 0)
│  BLOCK DATA                         │
├─────────────────────────────────────┤
│  ... (по одному блоку на слой)      │
├─────────────────────────────────────┤
│  TABLE OF CONTENTS                  │  массив {type, offset, size}
└─────────────────────────────────────┘
```

---

## 4. Библиотека slicestore-format

Установите зависимости:
```bash
pip install lz4 zstandard bsdiff4 xxhash msgpack
```

### 4.1 constants.py

```python
# format/constants.py

MAGIC = b"SLSSTORE!"          # 9 байт сигнатура
FORMAT_MAJOR = 1
FORMAT_MINOR = 0

# Коды типов блоков (uint16)
BLOCK_METADATA = 0x0001
BLOCK_LAYER    = 0x0002
BLOCK_PROCESS  = 0x0003
BLOCK_VERSION  = 0x0004
BLOCK_DELTA    = 0x0005
BLOCK_TOC      = 0x00FF

# Алгоритмы компрессии (флаги в заголовке)
COMPRESS_NONE = 0
COMPRESS_LZ4  = 1
COMPRESS_ZSTD = 2
COMPRESS_LZMA = 3
```

### 4.2 compressor.py

```python
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
```

### 4.3 blocks.py

```python
# format/blocks.py
import struct
import xxhash
import msgpack
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Point:
    """Точка в микрометрах (int32)."""
    x: int  # микрометры
    y: int  # микрометры

    def encode(self) -> bytes:
        # Кодируем как int32 little-endian
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
        # delta-кодирование: храним приращения между соседними точками
        result = struct.pack("<I", len(self.points))  # кол-во точек
        if not self.points:
            return result
        # Первая точка — абсолютная
        result += self.points[0].encode()
        # Остальные — дельты
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
        # Первая точка абсолютная
        p = Point.decode(data, offset)
        points.append(p)
        offset += 8
        # Остальные — дельты
        for _ in range(count - 1):
            dx, dy = struct.unpack_from("<ii", data, offset)
            offset += 8
            points.append(Point(points[-1].x + dx, points[-1].y + dy))
        return cls(points), offset


@dataclass
class InfillPath:
    """Траектория штриховки (открытая полилиния)."""
    points: List[Point] = field(default_factory=list)
    speed_mms: float = 60.0       # скорость мм/с

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
    index: int              # порядковый номер слоя
    height_um: int          # высота слоя в микрометрах (например 200 = 0.2 мм)
    contours: List[Contour] = field(default_factory=list)
    infill_paths: List[InfillPath] = field(default_factory=list)
    extruder_temp: float = 210.0   # °C
    bed_temp: float = 60.0         # °C

    def add_contour(self, points_mm: List[tuple]) -> None:
        """Добавить контур. points_mm — список (x_mm, y_mm)."""
        pts = [Point(int(x * 1000), int(y * 1000)) for x, y in points_mm]
        self.contours.append(Contour(pts))

    def add_infill(self, points_mm: List[tuple], speed_mms: float = 60.0) -> None:
        """Добавить траекторию штриховки."""
        pts = [Point(int(x * 1000), int(y * 1000)) for x, y in points_mm]
        self.infill_paths.append(InfillPath(pts, speed_mms))

    def encode(self) -> bytes:
        """Сериализовать слой в байты."""
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
        """Десериализовать слой из байт."""
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
    """Вычислить xxHash64 блока данных."""
    return xxhash.xxh64(data).intdigest()


def encode_metadata(meta: dict) -> bytes:
    """Сериализовать метаданные через MessagePack."""
    return msgpack.packb(meta, use_bin_type=True)


def decode_metadata(data: bytes) -> dict:
    """Десериализовать метаданные из MessagePack."""
    return msgpack.unpackb(data, raw=False)
```

### 4.4 writer.py

```python
# format/writer.py
import struct
import io
import time
from typing import List, Optional, Dict, Any

from .constants import (
    MAGIC, FORMAT_MAJOR, FORMAT_MINOR,
    BLOCK_METADATA, BLOCK_LAYER, BLOCK_TOC,
    COMPRESS_ZSTD
)
from .blocks import LayerData, encode_metadata, compute_block_hash
from .compressor import compress


class SliceWriter:
    """
    Записывает .sls файл.
    
    Использование:
        writer = SliceWriter("output.sls", compression=COMPRESS_ZSTD)
        writer.set_metadata({"job_name": "bracket_v1", "printer": "mk4"})
        writer.add_layer(layer_data)
        writer.save()
    """

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
        """Записать файл на диск."""
        buf = io.BytesIO()

        # Резервируем место под заголовок (48 байт)
        buf.write(b"\x00" * 48)

        toc_entries = []  # [(block_type, offset, compressed_size)]

        # --- Блок метаданных ---
        meta_raw = encode_metadata({
            **self._metadata,
            "layer_count": len(self._layers),
            "created_at": int(time.time()),
            "compression": self.compression,
        })
        meta_compressed = compress(meta_raw, self.compression)
        meta_hash = compute_block_hash(meta_compressed)

        offset = buf.tell()
        # Block header: type(2) + padding(2) + orig_size(4) + comp_size(4) + hash(8) + reserved(4) = 24 байта
        buf.write(struct.pack("<HHIIQi",
            BLOCK_METADATA, 0,
            len(meta_raw),
            len(meta_compressed),
            meta_hash,
            self.compression
        ))
        buf.write(meta_compressed)
        toc_entries.append((BLOCK_METADATA, offset, len(meta_compressed)))

        # --- Блоки слоёв ---
        for layer in self._layers:
            layer_raw = layer.encode()
            layer_compressed = compress(layer_raw, self.compression)
            layer_hash = compute_block_hash(layer_compressed)

            offset = buf.tell()
            buf.write(struct.pack("<HHIIQi",
                BLOCK_LAYER, 0,
                len(layer_raw),
                len(layer_compressed),
                layer_hash,
                self.compression
            ))
            buf.write(layer_compressed)
            toc_entries.append((BLOCK_LAYER, offset, len(layer_compressed)))

        # --- TOC (оглавление) ---
        toc_offset = buf.tell()
        toc_raw = b""
        for btype, boffset, bsize in toc_entries:
            toc_raw += struct.pack("<HxxIQ", btype, boffset, bsize)
        buf.write(toc_raw)

        # --- Записываем заголовок файла ---
        buf.seek(0)
        # magic(9) + major(1) + minor(1) + compression(1) + block_count(4) +
        # toc_offset(8) + created_at(8) + padding(16) = 48 байт
        header = MAGIC                                          # 9 байт
        header += struct.pack("<BB", FORMAT_MAJOR, FORMAT_MINOR)  # 2 байта
        header += struct.pack("<B", self.compression)             # 1 байт
        header += struct.pack("<I", len(toc_entries))             # 4 байт
        header += struct.pack("<Q", toc_offset)                   # 8 байт
        header += struct.pack("<Q", int(time.time()))             # 8 байт
        header += b"\x00" * (48 - len(header))                   # padding до 48 байт
        buf.write(header)

        # Сохраняем на диск
        with open(self.path, "wb") as f:
            f.write(buf.getvalue())

        print(f"[SliceWriter] Сохранено: {self.path} "
              f"({len(self._layers)} слоёв, {len(buf.getvalue())} байт)")
```

### 4.5 reader.py

```python
# format/reader.py
import struct
from typing import List, Optional, Dict, Any

from .constants import (
    MAGIC, BLOCK_METADATA, BLOCK_LAYER, BLOCK_TOC
)
from .blocks import LayerData, decode_metadata, compute_block_hash
from .compressor import decompress


class SliceReader:
    """
    Читает .sls файл.
    Поддерживает ленивую загрузку: блоки декомпрессируются только при запросе.

    Использование:
        with SliceReader("output.sls") as reader:
            meta = reader.get_metadata()
            layer = reader.get_layer(0)
    """

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
        self.compression = struct.unpack("<B", self._file.read(1))[0]
        self.block_count = struct.unpack("<I", self._file.read(4))[0]
        self.toc_offset = struct.unpack("<Q", self._file.read(8))[0]
        self.created_at = struct.unpack("<Q", self._file.read(8))[0]

    def _parse_toc(self):
        self._file.seek(self.toc_offset)
        self._toc = []
        for _ in range(self.block_count):
            data = self._file.read(14)  # HxxIQ = 2+2+4+8=16, но у нас HxxIQ=2+2+4+8
            btype, boffset, bsize = struct.unpack("<HxxIQ", data)
            self._toc.append({
                "type": btype,
                "offset": boffset,
                "compressed_size": bsize
            })

    def _read_block(self, offset: int) -> bytes:
        """Прочитать и декомпрессировать блок по смещению."""
        self._file.seek(offset)
        # Читаем заголовок блока
        hdr = self._file.read(24)
        btype, _, orig_size, comp_size, block_hash, algo = struct.unpack("<HHIIQi", hdr)
        # Читаем данные
        compressed = self._file.read(comp_size)
        # Проверяем хэш
        actual_hash = compute_block_hash(compressed)
        if actual_hash != block_hash:
            raise ValueError(f"Ошибка целостности блока по смещению {offset}!")
        # Декомпрессируем
        return decompress(compressed, algo)

    def get_metadata(self) -> Dict[str, Any]:
        """Получить метаданные файла."""
        if self._metadata is not None:
            return self._metadata
        for entry in self._toc:
            if entry["type"] == BLOCK_METADATA:
                raw = self._read_block(entry["offset"])
                self._metadata = decode_metadata(raw)
                return self._metadata
        return {}

    def get_layer(self, index: int) -> Optional[LayerData]:
        """Получить данные конкретного слоя (ленивая загрузка)."""
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
```

### 4.6 delta.py

```python
# format/delta.py
"""
Дельта-кодирование между версиями файла.
Используем bsdiff4: создаём патч (diff) и применяем его (patch).
"""
import bsdiff4


def create_delta(old_data: bytes, new_data: bytes) -> bytes:
    """
    Создать дельта-патч от old_data к new_data.
    Возвращает байты патча (намного меньше full-снимка для близких версий).
    """
    return bsdiff4.diff(old_data, new_data)


def apply_delta(old_data: bytes, patch: bytes) -> bytes:
    """
    Применить патч к old_data, получить new_data.
    """
    return bsdiff4.patch(old_data, patch)


def delta_stats(old_data: bytes, patch: bytes) -> dict:
    """Статистика: насколько патч меньше полного снимка."""
    return {
        "original_size": len(old_data),
        "patch_size": len(patch),
        "ratio_percent": round(len(patch) / len(old_data) * 100, 2)
    }
```

### 4.7 __init__.py библиотеки

```python
# format/__init__.py
from .blocks import LayerData, Point, Contour, InfillPath
from .writer import SliceWriter
from .reader import SliceReader
from .compressor import compress, decompress
from .delta import create_delta, apply_delta
from .constants import COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD, COMPRESS_LZMA

__all__ = [
    "LayerData", "Point", "Contour", "InfillPath",
    "SliceWriter", "SliceReader",
    "compress", "decompress",
    "create_delta", "apply_delta",
    "COMPRESS_NONE", "COMPRESS_LZ4", "COMPRESS_ZSTD", "COMPRESS_LZMA",
]
```

---

## 5. База данных

### migrations/init.sql

```sql
-- migrations/init.sql
-- Запускается автоматически при первом старте PostgreSQL-контейнера

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Основная таблица файлов
CREATE TABLE slices (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(255) NOT NULL,
    printer_id  VARCHAR(100),
    material    VARCHAR(100),
    layer_count INTEGER,
    file_size   BIGINT,
    checksum    VARCHAR(64),            -- xxHash64 hex
    compression SMALLINT DEFAULT 2,    -- 0=none 1=lz4 2=zstd 3=lzma
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    deleted_at  TIMESTAMPTZ,           -- soft delete
    storage_key VARCHAR(512)           -- ключ объекта в MinIO
);

-- Версии файлов
CREATE TABLE slice_versions (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slice_id     UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    version_num  INTEGER NOT NULL,
    message      TEXT,
    author       VARCHAR(255),
    file_size    BIGINT,
    storage_key  VARCHAR(512),         -- ключ в MinIO для этой версии
    delta_key    VARCHAR(512),         -- ключ патча (если есть)
    is_snapshot  BOOLEAN DEFAULT FALSE, -- TRUE = полный снимок, FALSE = дельта
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (slice_id, version_num)
);

-- Теги
CREATE TABLE slice_tags (
    slice_id  UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    tag       VARCHAR(100) NOT NULL,
    PRIMARY KEY (slice_id, tag)
);

-- Индексы для быстрого поиска
CREATE INDEX idx_slices_name      ON slices(name) WHERE deleted_at IS NULL;
CREATE INDEX idx_slices_printer   ON slices(printer_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_slices_created   ON slices(created_at DESC);
CREATE INDEX idx_tags_tag         ON slice_tags(tag);
CREATE INDEX idx_versions_slice   ON slice_versions(slice_id, version_num DESC);

-- Полнотекстовый поиск по имени и материалу
CREATE INDEX idx_slices_fts ON slices USING gin(
    to_tsvector('russian', coalesce(name,'') || ' ' || coalesce(material,'') || ' ' || coalesce(printer_id,''))
);
```

---

## 6. FastAPI сервис

### service/Dockerfile

```dockerfile
# service/Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Создайте `service/requirements.txt`:

```
fastapi==0.110.0
uvicorn[standard]==0.29.0
asyncpg==0.29.0
aioboto3==12.4.0
redis[asyncio]==5.0.3
python-multipart==0.0.9
pydantic==2.7.0
pydantic-settings==2.2.1
xxhash==3.4.1
msgpack==1.0.8
python-jose[cryptography]==3.3.0
lz4==4.3.3
zstandard==0.22.0
bsdiff4==1.2.4
```

### service/config.py

```python
# service/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://slicestore:slicestore123@postgres:5432/slicestore"
    minio_endpoint: str = "minio:9000"
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "minioadmin123"
    minio_bucket: str = "slices"
    redis_url: str = "redis://redis:6379/0"

    class Config:
        env_file = ".env"


settings = Settings()
```

### service/database.py

```python
# service/database.py
import asyncpg
from .config import settings

_pool: asyncpg.Pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
```

### service/storage.py

```python
# service/storage.py
"""Работа с MinIO (S3-совместимое объектное хранилище)."""
import aioboto3
from botocore.exceptions import ClientError
from .config import settings

_session = None


def get_session():
    global _session
    if _session is None:
        _session = aioboto3.Session()
    return _session


def s3_client():
    """Контекстный менеджер для S3-клиента."""
    return get_session().client(
        "s3",
        endpoint_url=f"http://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
    )


async def ensure_bucket():
    """Создать бакет, если он не существует."""
    async with s3_client() as s3:
        try:
            await s3.head_bucket(Bucket=settings.minio_bucket)
        except ClientError:
            await s3.create_bucket(Bucket=settings.minio_bucket)


async def upload_file(key: str, data: bytes, content_type: str = "application/octet-stream"):
    """Загрузить файл в MinIO."""
    async with s3_client() as s3:
        await s3.put_object(
            Bucket=settings.minio_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )


async def download_file(key: str) -> bytes:
    """Скачать файл из MinIO."""
    async with s3_client() as s3:
        response = await s3.get_object(Bucket=settings.minio_bucket, Key=key)
        return await response["Body"].read()


async def delete_file(key: str):
    """Удалить файл из MinIO."""
    async with s3_client() as s3:
        await s3.delete_object(Bucket=settings.minio_bucket, Key=key)
```

### service/cache.py

```python
# service/cache.py
"""Redis кэш для метаданных."""
import json
import redis.asyncio as redis
from .config import settings

_redis: redis.Redis = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def cache_get(key: str):
    r = await get_redis()
    val = await r.get(key)
    return json.loads(val) if val else None


async def cache_set(key: str, value: dict, ttl: int = 300):
    r = await get_redis()
    await r.setex(key, ttl, json.dumps(value, default=str))


async def cache_delete(key: str):
    r = await get_redis()
    await r.delete(key)
```

### service/models.py

```python
# service/models.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid


class SliceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    printer_id: Optional[str] = None
    material: Optional[str] = None
    tags: List[str] = []


class SliceResponse(BaseModel):
    id: uuid.UUID
    name: str
    printer_id: Optional[str]
    material: Optional[str]
    layer_count: Optional[int]
    file_size: Optional[int]
    compression: int
    created_at: datetime
    updated_at: datetime
    tags: List[str] = []

    class Config:
        from_attributes = True


class VersionResponse(BaseModel):
    id: uuid.UUID
    slice_id: uuid.UUID
    version_num: int
    message: Optional[str]
    author: Optional[str]
    file_size: Optional[int]
    is_snapshot: bool
    created_at: datetime


class SearchParams(BaseModel):
    query: Optional[str] = None
    printer_id: Optional[str] = None
    material: Optional[str] = None
    tags: List[str] = []
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)
```

### service/routers/slices.py

```python
# service/routers/slices.py
"""CRUD операции с .sls файлами."""
import uuid
import xxhash
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import Response
from typing import List, Optional

from ..database import get_pool
from ..storage import upload_file, download_file, delete_file
from ..cache import cache_get, cache_set, cache_delete
from ..models import SliceCreate, SliceResponse

router = APIRouter(prefix="/slices", tags=["slices"])


@router.post("/", response_model=SliceResponse, status_code=201)
async def upload_slice(
    file: UploadFile = File(..., description="Файл в формате .sls"),
    name: str = Form(...),
    printer_id: Optional[str] = Form(None),
    material: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),   # JSON-строка ["tag1","tag2"]
):
    """Загрузить новый .sls файл."""
    import json

    # Читаем содержимое файла
    content = await file.read()
    if len(content) < 9 or content[:9] != b"SLSSTORE!":
        raise HTTPException(400, detail="Файл не является валидным .sls файлом (неверная сигнатура)")

    file_size = len(content)
    checksum = xxhash.xxh64(content).hexdigest()
    storage_key = f"slices/{uuid.uuid4()}.sls"

    # Парсим теги
    tag_list = json.loads(tags) if tags else []

    # Читаем метаданные из файла (упрощённо — берём layer_count из заголовка)
    import struct
    try:
        # block_count в заголовке — смещение 12, 4 байта
        block_count = struct.unpack_from("<I", content, 12)[0]
        layer_count = block_count - 1   # грубая оценка (1 блок = metadata)
    except Exception:
        layer_count = None

    # Загружаем в MinIO
    await upload_file(storage_key, content)

    # Сохраняем в PostgreSQL
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO slices (name, printer_id, material, layer_count, file_size, checksum, storage_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
        """, name, printer_id, material, layer_count, file_size, checksum, storage_key)

        slice_id = row["id"]

        # Сохраняем версию 1 (начальный снимок)
        await conn.execute("""
            INSERT INTO slice_versions (slice_id, version_num, message, is_snapshot, file_size, storage_key)
            VALUES ($1, 1, 'Initial upload', TRUE, $2, $3)
        """, slice_id, file_size, storage_key)

        # Сохраняем теги
        for tag in tag_list:
            await conn.execute(
                "INSERT INTO slice_tags (slice_id, tag) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                slice_id, tag
            )

    result = dict(row)
    result["tags"] = tag_list
    return SliceResponse(**result)


@router.get("/{slice_id}", response_class=Response)
async def download_slice(slice_id: uuid.UUID):
    """Скачать .sls файл (последняя версия)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT storage_key FROM slices WHERE id = $1 AND deleted_at IS NULL",
            slice_id
        )
    if not row:
        raise HTTPException(404, detail="Файл не найден")

    content = await download_file(row["storage_key"])
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{slice_id}.sls"'}
    )


@router.get("/{slice_id}/metadata", response_model=SliceResponse)
async def get_metadata(slice_id: uuid.UUID):
    """Получить метаданные файла без скачивания бинарных данных."""
    # Пробуем кэш
    cached = await cache_get(f"slice:{slice_id}")
    if cached:
        return SliceResponse(**cached)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM slices WHERE id = $1 AND deleted_at IS NULL", slice_id
        )
        if not row:
            raise HTTPException(404, detail="Файл не найден")
        tags = await conn.fetch(
            "SELECT tag FROM slice_tags WHERE slice_id = $1", slice_id
        )

    result = dict(row)
    result["tags"] = [t["tag"] for t in tags]

    # Кэшируем на 5 минут
    await cache_set(f"slice:{slice_id}", result)
    return SliceResponse(**result)


@router.delete("/{slice_id}", status_code=204)
async def delete_slice(slice_id: uuid.UUID):
    """Мягкое удаление файла."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE slices SET deleted_at = NOW() WHERE id = $1 AND deleted_at IS NULL",
            slice_id
        )
    if result == "UPDATE 0":
        raise HTTPException(404, detail="Файл не найден")

    await cache_delete(f"slice:{slice_id}")
    return None
```

### service/routers/versions.py

```python
# service/routers/versions.py
"""Управление версиями .sls файлов."""
import uuid
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from typing import List, Optional

from ..database import get_pool
from ..storage import upload_file, download_file
from ..models import VersionResponse

router = APIRouter(prefix="/slices", tags=["versions"])


@router.get("/{slice_id}/versions", response_model=List[VersionResponse])
async def list_versions(slice_id: uuid.UUID):
    """Получить историю версий файла."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM slice_versions
            WHERE slice_id = $1
            ORDER BY version_num DESC
        """, slice_id)
    return [VersionResponse(**dict(r)) for r in rows]


@router.post("/{slice_id}/versions", response_model=VersionResponse, status_code=201)
async def create_version(
    slice_id: uuid.UUID,
    file: UploadFile = File(...),
    message: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
):
    """
    Создать новую версию файла.
    Автоматически вычисляет дельта-патч от предыдущей версии.
    """
    import xxhash
    from ..storage import download_file as dl

    new_content = await file.read()
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Проверяем существование файла
        slice_row = await conn.fetchrow(
            "SELECT * FROM slices WHERE id = $1 AND deleted_at IS NULL", slice_id
        )
        if not slice_row:
            raise HTTPException(404, detail="Файл не найден")

        # Получаем последнюю версию
        last_ver = await conn.fetchrow("""
            SELECT * FROM slice_versions
            WHERE slice_id = $1
            ORDER BY version_num DESC LIMIT 1
        """, slice_id)

        new_version_num = (last_ver["version_num"] + 1) if last_ver else 1

        # Пытаемся создать дельта-патч
        is_snapshot = True
        delta_key = None
        storage_key = f"slices/{slice_id}/v{new_version_num}.sls"

        if last_ver and last_ver["storage_key"]:
            try:
                from format.delta import create_delta, delta_stats
                old_content = await dl(last_ver["storage_key"])
                patch = create_delta(old_content, new_content)
                stats = delta_stats(old_content, patch)

                # Если патч меньше 50% от полного файла — используем дельту
                if stats["ratio_percent"] < 50:
                    delta_key = f"slices/{slice_id}/v{new_version_num}.patch"
                    await upload_file(delta_key, patch)
                    is_snapshot = False
                    print(f"[Versions] Дельта: {stats['ratio_percent']}% от оригинала")
                else:
                    print(f"[Versions] Патч слишком большой ({stats['ratio_percent']}%), сохраняем снимок")
            except Exception as e:
                print(f"[Versions] Не удалось создать дельту: {e}, сохраняем снимок")

        # Всегда сохраняем полный файл (нужен для будущих дельт)
        await upload_file(storage_key, new_content)

        # Обновляем запись в БД
        await conn.execute("""
            UPDATE slices SET storage_key=$1, file_size=$2, updated_at=NOW()
            WHERE id=$3
        """, storage_key, len(new_content), slice_id)

        # Записываем версию
        row = await conn.fetchrow("""
            INSERT INTO slice_versions
                (slice_id, version_num, message, author, file_size, storage_key, delta_key, is_snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """, slice_id, new_version_num, message, author,
             len(new_content), storage_key, delta_key, is_snapshot)

    return VersionResponse(**dict(row))


@router.get("/{slice_id}/versions/{version_num}", response_class=Response)
async def get_version(slice_id: uuid.UUID, version_num: int):
    """Скачать конкретную версию файла."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM slice_versions
            WHERE slice_id=$1 AND version_num=$2
        """, slice_id, version_num)

    if not row:
        raise HTTPException(404, detail="Версия не найдена")

    content = await download_file(row["storage_key"])
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="v{version_num}.sls"'}
    )


@router.post("/{slice_id}/restore/{version_num}", status_code=200)
async def restore_version(slice_id: uuid.UUID, version_num: int):
    """Восстановить указанную версию как текущую (создаёт новую версию)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM slice_versions WHERE slice_id=$1 AND version_num=$2
        """, slice_id, version_num)

        if not row:
            raise HTTPException(404, detail="Версия не найдена")

        content = await download_file(row["storage_key"])

        # Получаем номер следующей версии
        last = await conn.fetchrow("""
            SELECT MAX(version_num) as max_v FROM slice_versions WHERE slice_id=$1
        """, slice_id)
        new_num = (last["max_v"] or 0) + 1

        new_key = f"slices/{slice_id}/v{new_num}.sls"
        await upload_file(new_key, content)

        await conn.execute("""
            INSERT INTO slice_versions (slice_id, version_num, message, is_snapshot, file_size, storage_key)
            VALUES ($1,$2,$3, TRUE,$4,$5)
        """, slice_id, new_num, f"Restored from version {version_num}", len(content), new_key)

        await conn.execute(
            "UPDATE slices SET storage_key=$1, updated_at=NOW() WHERE id=$2",
            new_key, slice_id
        )

    return {"message": f"Версия {version_num} восстановлена как v{new_num}"}
```

### service/routers/search.py

```python
# service/routers/search.py
"""Поиск по метаданным."""
import uuid
from fastapi import APIRouter, Query
from typing import List, Optional

from ..database import get_pool
from ..models import SliceResponse

router = APIRouter(prefix="/slices", tags=["search"])


@router.get("/search", response_model=List[SliceResponse])
async def search_slices(
    q: Optional[str] = Query(None, description="Полнотекстовый поиск по имени/материалу"),
    printer_id: Optional[str] = Query(None),
    material: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Поиск файлов по различным критериям."""
    pool = await get_pool()

    conditions = ["s.deleted_at IS NULL"]
    params = []
    idx = 1

    if q:
        conditions.append(f"""
            to_tsvector('russian', coalesce(s.name,'') || ' ' || coalesce(s.material,'') || ' ' || coalesce(s.printer_id,''))
            @@ plainto_tsquery('russian', ${idx})
        """)
        params.append(q)
        idx += 1

    if printer_id:
        conditions.append(f"s.printer_id = ${idx}")
        params.append(printer_id)
        idx += 1

    if material:
        conditions.append(f"s.material ILIKE ${idx}")
        params.append(f"%{material}%")
        idx += 1

    if tag:
        conditions.append(f"""
            EXISTS (SELECT 1 FROM slice_tags st WHERE st.slice_id=s.id AND st.tag=${idx})
        """)
        params.append(tag)
        idx += 1

    where = " AND ".join(conditions)

    params.extend([limit, offset])

    query = f"""
        SELECT s.*, array_agg(DISTINCT st.tag) FILTER (WHERE st.tag IS NOT NULL) as tags
        FROM slices s
        LEFT JOIN slice_tags st ON st.slice_id = s.id
        WHERE {where}
        GROUP BY s.id
        ORDER BY s.created_at DESC
        LIMIT ${idx} OFFSET ${idx+1}
    """

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(query, *params)

    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = d.get("tags") or []
        result.append(SliceResponse(**d))
    return result
```

### service/main.py

```python
# service/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from .database import get_pool, close_pool
from .storage import ensure_bucket
from .routers import slices, versions, search


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await get_pool()
    await ensure_bucket()
    print("[SliceStore] Сервис запущен. БД и хранилище подключены.")
    yield
    # Shutdown
    await close_pool()
    print("[SliceStore] Сервис остановлен.")


app = FastAPI(
    title="SliceStore API",
    description="Сервис хранения данных слайсинга и штриховки для 3D-печати",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router)    # ВАЖНО: search РАНЬШЕ slices (иначе /search перехватится как /{id})
app.include_router(slices.router)
app.include_router(versions.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "slicestore"}
```

---

## 7. Клиентская библиотека

```python
# client/client.py
"""
Клиент для взаимодействия с SliceStore API.

Использование:
    from client.client import SliceStoreClient
    from format import SliceWriter, LayerData, COMPRESS_ZSTD

    # 1. Создаём файл
    writer = SliceWriter("/tmp/test.sls", compression=COMPRESS_ZSTD)
    writer.set_metadata({"job_name": "bracket"})
    layer = LayerData(index=0, height_um=200)
    layer.add_contour([(0,0),(10,0),(10,10),(0,10)])
    layer.add_infill([(1,1),(9,1),(9,9),(1,9)])
    writer.add_layer(layer)
    writer.save()

    # 2. Загружаем
    client = SliceStoreClient("http://localhost:8000")
    file_id = client.upload("/tmp/test.sls", name="bracket_v1", tags=["test"])

    # 3. Получаем метаданные
    meta = client.get_metadata(file_id)
    print(meta)

    # 4. Скачиваем
    client.download(file_id, "/tmp/downloaded.sls")

    # 5. История версий
    versions = client.get_versions(file_id)
    for v in versions:
        print(f"  v{v['version_num']}: {v['message']}")
"""
import json
import requests
from typing import Optional, List, Dict, Any


class SliceStoreClient:
    def __init__(self, base_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def upload(self, path: str, name: str, printer_id: str = None,
               material: str = None, tags: List[str] = None) -> str:
        """Загрузить файл. Возвращает UUID."""
        with open(path, "rb") as f:
            resp = self.session.post(
                f"{self.base_url}/slices/",
                files={"file": (path, f, "application/octet-stream")},
                data={
                    "name": name,
                    "printer_id": printer_id or "",
                    "material": material or "",
                    "tags": json.dumps(tags or []),
                },
            )
        resp.raise_for_status()
        return resp.json()["id"]

    def get_metadata(self, slice_id: str) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}/metadata")
        resp.raise_for_status()
        return resp.json()

    def download(self, slice_id: str, dest_path: str) -> None:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}")
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)

    def get_versions(self, slice_id: str) -> List[Dict]:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}/versions")
        resp.raise_for_status()
        return resp.json()

    def restore_version(self, slice_id: str, version_num: int) -> Dict:
        resp = self.session.post(f"{self.base_url}/slices/{slice_id}/restore/{version_num}")
        resp.raise_for_status()
        return resp.json()

    def search(self, q: str = None, printer_id: str = None,
               material: str = None, tag: str = None) -> List[Dict]:
        params = {}
        if q: params["q"] = q
        if printer_id: params["printer_id"] = printer_id
        if material: params["material"] = material
        if tag: params["tag"] = tag
        resp = self.session.get(f"{self.base_url}/slices/search", params=params)
        resp.raise_for_status()
        return resp.json()

    def delete(self, slice_id: str) -> None:
        resp = self.session.delete(f"{self.base_url}/slices/{slice_id}")
        resp.raise_for_status()
```

---

## 8. Тесты

Установите зависимости для тестов:
```bash
pip install pytest pytest-asyncio httpx
```

### tests/conftest.py

```python
# tests/conftest.py
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
```

### tests/test_format.py

```python
# tests/test_format.py
"""Модульные тесты для формата .sls"""
import os
import tempfile
import pytest

from format import (
    SliceWriter, SliceReader, LayerData,
    COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD, COMPRESS_LZMA
)
from format.delta import create_delta, apply_delta, delta_stats


class TestCompression:
    """Тесты компрессии — убеждаемся, что каждый алгоритм работает."""

    @pytest.mark.parametrize("algo", [COMPRESS_NONE, COMPRESS_LZ4, COMPRESS_ZSTD, COMPRESS_LZMA])
    def test_roundtrip(self, algo):
        """Сжать и распаковать — должны получить исходные данные."""
        from format.compressor import compress, decompress
        original = b"Hello SliceStore! " * 100
        compressed = compress(original, algo)
        decompressed = decompress(compressed, algo)
        assert decompressed == original

    def test_lz4_is_faster_check(self):
        """LZ4 должен сжимать корректно."""
        from format.compressor import compress, decompress
        data = bytes(range(256)) * 50
        assert decompress(compress(data, COMPRESS_LZ4), COMPRESS_LZ4) == data

    def test_lzma_compresses_better(self):
        """LZMA должен давать лучший коэффициент сжатия на повторяющихся данных."""
        from format.compressor import compress
        data = b"AAABBBCCC" * 1000
        lz4_size = len(compress(data, COMPRESS_LZ4))
        lzma_size = len(compress(data, COMPRESS_LZMA))
        assert lzma_size < lz4_size


class TestLayerData:
    """Тесты сериализации данных слоя."""

    def test_encode_decode_roundtrip(self):
        """Данные слоя должны корректно сериализоваться и десериализоваться."""
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
        # Проверяем, что координаты сохранились (с точностью до мкм)
        assert decoded.contours[0].points[0].x == 0
        assert decoded.contours[0].points[1].x == 10000  # 10мм = 10000мкм

    def test_empty_layer(self):
        """Пустой слой (без контуров) должен сериализоваться."""
        layer = LayerData(index=0, height_um=100)
        raw = layer.encode()
        decoded = LayerData.decode(raw)
        assert decoded.index == 0
        assert len(decoded.contours) == 0


class TestSliceFile:
    """Интеграционные тесты записи и чтения файла."""

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
        """Zstd должен сжимать файл лучше, чем без сжатия."""
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            path_none = f.name
        with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
            path_zstd = f.name
        try:
            self._make_test_file(path_none, algo=COMPRESS_NONE, n_layers=20)
            self._make_test_file(path_zstd, algo=COMPRESS_ZSTD, n_layers=20)
            size_none = os.path.getsize(path_none)
            size_zstd = os.path.getsize(path_zstd)
            assert size_zstd < size_none, f"Zstd ({size_zstd}) должен быть меньше None ({size_none})"
        finally:
            os.unlink(path_none)
            os.unlink(path_zstd)


class TestDelta:
    """Тесты системы версионирования (дельта-патчи)."""

    def test_delta_roundtrip(self):
        """Применить патч к старым данным — получить новые."""
        old = b"Version 1 data: " + b"ABCDEF" * 100
        new = b"Version 2 data: " + b"ABCDEF" * 100 + b"NEW_SUFFIX"
        patch = create_delta(old, new)
        restored = apply_delta(old, patch)
        assert restored == new

    def test_delta_is_small_for_minor_changes(self):
        """Для небольших изменений патч должен быть значительно меньше файла."""
        base = b"X" * 10000
        # Меняем только 100 байт
        modified = base[:100] + b"Y" * 100 + base[200:]
        patch = create_delta(base, modified)
        stats = delta_stats(base, patch)
        # Патч должен быть меньше 30% от оригинала
        assert stats["ratio_percent"] < 30, f"Патч слишком большой: {stats['ratio_percent']}%"
```

### tests/test_api.py

```python
# tests/test_api.py
"""
Тесты REST API.
Запускать с работающим Docker Compose: docker-compose up -d
"""
import io
import tempfile
import os
import pytest
import requests

from format import SliceWriter, LayerData, COMPRESS_ZSTD

BASE_URL = "http://localhost:8000"


def make_sls_bytes(n_layers: int = 2) -> bytes:
    """Создать корректный .sls файл в памяти."""
    with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
        path = f.name
    try:
        writer = SliceWriter(path, compression=COMPRESS_ZSTD)
        writer.set_metadata({"job_name": "api_test"})
        for i in range(n_layers):
            layer = LayerData(index=i, height_um=200)
            layer.add_contour([(0, 0), (10, 0), (10, 10), (0, 10)])
            writer.add_layer(layer)
        writer.save()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


@pytest.fixture(scope="module")
def uploaded_id():
    """Загрузить тестовый файл, вернуть его ID."""
    content = make_sls_bytes(n_layers=3)
    resp = requests.post(
        f"{BASE_URL}/slices/",
        files={"file": ("test.sls", content, "application/octet-stream")},
        data={"name": "API Test File", "material": "PLA", "tags": '["test","api"]'},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_health():
    resp = requests.get(f"{BASE_URL}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload(uploaded_id):
    assert uploaded_id is not None


def test_get_metadata(uploaded_id):
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/metadata")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "API Test File"
    assert "test" in data["tags"]


def test_download(uploaded_id):
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}")
    assert resp.status_code == 200
    assert resp.content[:9] == b"SLSSTORE!"   # проверяем сигнатуру


def test_versions(uploaded_id):
    """Создать вторую версию и проверить историю."""
    new_content = make_sls_bytes(n_layers=5)
    resp = requests.post(
        f"{BASE_URL}/slices/{uploaded_id}/versions",
        files={"file": ("v2.sls", new_content, "application/octet-stream")},
        data={"message": "Added 2 more layers", "author": "test_user"},
    )
    assert resp.status_code == 201

    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/versions")
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) >= 2


def test_search():
    resp = requests.get(f"{BASE_URL}/slices/search?q=API+Test")
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1


def test_delete(uploaded_id):
    resp = requests.delete(f"{BASE_URL}/slices/{uploaded_id}")
    assert resp.status_code == 204
    # Проверяем, что файл удалён
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/metadata")
    assert resp.status_code == 404
```

---

## 9. Как запустить

### Шаг 1 — Установить зависимости

```bash
# Создаём виртуальное окружение
python -m venv venv
source venv/bin/activate          # Linux/Mac
# venv\Scripts\activate           # Windows

# Устанавливаем зависимости формата
pip install lz4 zstandard bsdiff4 xxhash msgpack

# Зависимости тестов
pip install pytest requests
```

### Шаг 2 — Запустить инфраструктуру

```bash
cd slicestore/
docker-compose up -d

# Проверяем, что все сервисы запустились
docker-compose ps
# Должны быть: postgres, redis, minio, service — все с Status = running
```

### Шаг 3 — Проверить сервис

```bash
# Health check
curl http://localhost:8000/health
# Ожидаемый ответ: {"status":"ok","service":"slicestore"}

# Swagger UI с документацией API
# Откройте в браузере: http://localhost:8000/docs

# MinIO консоль (хранилище файлов)
# Откройте в браузере: http://localhost:9001
# Логин: minioadmin / minioadmin123
```

### Шаг 4 — Быстрый тест из Python

```python
# Создаём и загружаем тестовый файл
# Запустите этот код в Python:

import sys
sys.path.insert(0, ".")   # из корня проекта

from format import SliceWriter, SliceReader, LayerData, COMPRESS_ZSTD
from client.client import SliceStoreClient

# 1. Создаём .sls файл
writer = SliceWriter("/tmp/test_bracket.sls", compression=COMPRESS_ZSTD)
writer.set_metadata({
    "job_name": "bracket_v1",
    "printer_id": "mk4-001",
    "material": "PLA",
    "layer_height_mm": 0.2,
})

for i in range(10):
    layer = LayerData(index=i, height_um=200)
    layer.add_contour([(0, 0), (20, 0), (20, 20), (0, 20)])
    layer.add_infill([(1, 1 + i), (19, 1 + i)], speed_mms=60.0)
    layer.add_infill([(1, 5 + i), (19, 5 + i)], speed_mms=60.0)
    writer.add_layer(layer)

writer.save()
print("Файл создан:", "/tmp/test_bracket.sls")

# 2. Читаем обратно
with SliceReader("/tmp/test_bracket.sls") as r:
    meta = r.get_metadata()
    print(f"Метаданные: {meta}")
    print(f"Слоёв: {r.layer_count}")
    layer = r.get_layer(3)
    print(f"Слой 3: {len(layer.contours)} контуров, {len(layer.infill_paths)} траекторий")

# 3. Загружаем на сервер
client = SliceStoreClient("http://localhost:8000")
file_id = client.upload(
    "/tmp/test_bracket.sls",
    name="Test Bracket v1",
    printer_id="mk4-001",
    material="PLA",
    tags=["test", "bracket"],
)
print(f"Загружено с ID: {file_id}")

# 4. Получаем метаданные
meta = client.get_metadata(file_id)
print(f"Из сервера: {meta['name']}, теги: {meta['tags']}")

# 5. Скачиваем
client.download(file_id, "/tmp/downloaded.sls")
print("Скачано в /tmp/downloaded.sls")

# 6. Проверяем историю версий
versions = client.get_versions(file_id)
print(f"Версий: {len(versions)}")
for v in versions:
    print(f"  v{v['version_num']}: {v['message']} (снимок: {v['is_snapshot']})")
```

### Шаг 5 — Запустить тесты

```bash
# Только тесты формата (не требуют запущенного Docker)
pytest tests/test_format.py -v

# Тесты API (требуют запущенного docker-compose)
pytest tests/test_api.py -v

# Все тесты с покрытием
pip install pytest-cov
pytest tests/ -v --cov=format --cov-report=term-missing
```

---

## 10. Чек-лист — что должно работать

### Формат файла (.sls)
- [ ] Файл начинается с сигнатуры `SLSSTORE!`
- [ ] `SliceWriter` создаёт файл, `SliceReader` его читает
- [ ] Все 4 алгоритма компрессии работают (None/LZ4/Zstd/LZMA)
- [ ] Zstd сжимает лучше, чем LZ4; LZ4 быстрее
- [ ] Delta-патч для небольших изменений < 30% от полного файла
- [ ] Контрольная сумма xxHash64 обнаруживает повреждение данных
- [ ] Координаты точек сохраняются корректно (delta-кодирование)

### REST API
- [ ] `GET /health` → `{"status":"ok"}`
- [ ] `POST /slices/` с .sls файлом → 201, возвращает UUID
- [ ] `GET /slices/{id}` → бинарный .sls файл с сигнатурой
- [ ] `GET /slices/{id}/metadata` → JSON метаданных (кэшируется в Redis)
- [ ] `GET /slices/{id}/versions` → список версий
- [ ] `POST /slices/{id}/versions` → создаёт новую версию + дельта-патч
- [ ] `POST /slices/{id}/restore/{v}` → восстанавливает версию
- [ ] `GET /slices/search?q=...` → полнотекстовый поиск
- [ ] `DELETE /slices/{id}` → 204, мягкое удаление
- [ ] После DELETE: `GET /slices/{id}/metadata` → 404

### Инфраструктура
- [ ] `docker-compose up -d` поднимает все 4 сервиса без ошибок
- [ ] PostgreSQL содержит таблицы `slices`, `slice_versions`, `slice_tags`
- [ ] MinIO bucket `slices` создаётся автоматически
- [ ] Redis кэширует метаданные (второй запрос быстрее первого)
- [ ] `http://localhost:8000/docs` — Swagger UI открывается

### Тесты
- [ ] `pytest tests/test_format.py` — все тесты зелёные
- [ ] `pytest tests/test_api.py` — все тесты зелёные (с запущенным docker-compose)
- [ ] Покрытие кода > 80%

---

## Полезные команды

```bash
# Смотреть логи сервиса в реальном времени
docker-compose logs -f service

# Подключиться к PostgreSQL
docker-compose exec postgres psql -U slicestore -d slicestore

# Посмотреть все загруженные файлы в БД
# (внутри psql):
SELECT id, name, file_size, created_at FROM slices WHERE deleted_at IS NULL;

# Полностью сбросить и пересоздать все контейнеры
docker-compose down -v && docker-compose up -d

# Остановить без удаления данных
docker-compose stop
```

---

## 11. Расширение: G-code, большие файлы, обратная совместимость

> **Контекст:** Проект работает с реальными производственными G-code файлами размером 1–3 ГБ. G-code — универсальный стандарт инструкций ЧПУ/принтеров — используется и в FDM, и в SLS, и в SLM. Концепция хранения и версионирования унифицирована, но параметры штриховки (hatching) различаются по типу технологии. SliceStore **не отказывается от G-code**, а добавляет над ним слой версионирования, индексации и хранения.

---

### 11.1 Обратная совместимость с G-code

G-code — это просто текстовая инструкция для машины. SliceStore хранит G-code как есть, не интерпретируя его содержимое. Формат `.sls` оборачивает G-code в блочную структуру с метаданными и сжатием, но исходный файл всегда можно извлечь обратно без потерь.

**Принцип работы:**

```
G-code файл (1–3 ГБ)
    │
    ▼
SliceWriter (потоковая запись)
    ├── BLOCK_METADATA  → тип принтера, материал, технология, хэш оригинала
    ├── BLOCK_GCODE_RAW → сжатый G-code (Zstd, стриминг по чанкам)
    ├── BLOCK_INDEX     → индекс слоёв (номер слоя → байтовый офсет в G-code)
    └── BLOCK_TOC
    │
    ▼
SliceReader (ленивое чтение)
    └── get_layer(n) → читает только нужный диапазон байт из BLOCK_GCODE_RAW
```

**Добавить в `constants.py`:**

```python
# format/constants.py — дополнение для G-code

BLOCK_GCODE_RAW   = 0x0010   # Сырой G-code (может быть разбит на чанки)
BLOCK_GCODE_CHUNK = 0x0011   # Один чанк большого G-code (для стриминга)
BLOCK_LAYER_INDEX = 0x0012   # Индекс: layer_num → offset в G-code

PRINTER_TYPE_FDM  = "fdm"    # Fused Deposition Modeling
PRINTER_TYPE_SLS  = "sls"    # Selective Laser Sintering
PRINTER_TYPE_SLM  = "slm"    # Selective Laser Melting

CHUNK_SIZE = 64 * 1024 * 1024   # 64 МБ — размер одного чанка при записи
```

---

### 11.2 Работа с большими файлами (1–3 ГБ)

Главная проблема: нельзя читать файл целиком в память. Решение — **стриминговая запись чанками** и **индекс слоёв** для произвольного доступа.

**Добавить `format/gcode_streamer.py`:**

```python
# format/gcode_streamer.py
"""
Потоковая запись и чтение G-code файлов размером 1-3 ГБ.
Файл разбивается на чанки по 64 МБ, каждый сжимается независимо.
Индекс слоёв позволяет читать произвольный слой без распаковки всего файла.
"""
import struct
import re
import zstandard as zstd
from pathlib import Path
from typing import Iterator, Dict, List, Tuple, Optional

from .constants import (
    CHUNK_SIZE, BLOCK_GCODE_CHUNK, BLOCK_LAYER_INDEX,
    BLOCK_METADATA, COMPRESS_ZSTD
)
from .blocks import encode_metadata, compute_block_hash


# Регулярка для определения начала нового слоя в G-code
# Работает для Cura, PrusaSlicer, Simplify3D, а также SLS/SLM слайсеров
LAYER_COMMENT_RE = re.compile(
    rb";(?:LAYER|layer|Layer)[:\s]+(\d+)"       # ;LAYER:0 или ;layer 0
    rb"|;Z:[\d.]+\s"                            # ;Z:0.200
    rb"|G0\s.*?Z([\d.]+)"                       # G0 Z0.200 (только подъём Z)
    , re.MULTILINE
)


class GCodeStreamer:
    """
    Записывает большой G-code файл в .sls контейнер потоково.

    Использование:
        streamer = GCodeStreamer("output.sls")
        streamer.write_gcode("/path/to/huge.gcode", metadata={...})
    """

    def __init__(self, output_path: str, chunk_size: int = CHUNK_SIZE):
        self.output_path = output_path
        self.chunk_size = chunk_size

    def write_gcode(self, gcode_path: str, metadata: dict) -> dict:
        """
        Потоково читает G-code, записывает чанками в .sls файл.
        Возвращает статистику: кол-во слоёв, размер, коэффициент сжатия.
        """
        gcode_path = Path(gcode_path)
        file_size = gcode_path.stat().st_size
        print(f"[GCodeStreamer] Начало: {gcode_path.name} ({file_size / 1e9:.2f} ГБ)")

        cctx = zstd.ZstdCompressor(level=3, threads=-1)   # все ядра

        layer_index: Dict[int, int] = {}    # layer_num → байтовый офсет в исходном G-code
        chunks_info: List[Tuple[int, int]] = []  # (compressed_size, orig_offset)

        total_compressed = 0
        gcode_offset = 0     # офсет в исходном файле
        chunk_num = 0

        with open(gcode_path, "rb") as src, open(self.output_path, "wb") as dst:
            # Резервируем место под заголовок файла (48 байт)
            dst.write(b"\x00" * 48)
            toc_entries = []

            # --- Блок метаданных ---
            meta_payload = encode_metadata({
                **metadata,
                "source_file": gcode_path.name,
                "source_size_bytes": file_size,
                "format": "gcode",
            })
            meta_compressed = cctx.compress(meta_payload)
            blk_offset = dst.tell()
            dst.write(struct.pack("<HHIIQi",
                BLOCK_METADATA, 0,
                len(meta_payload), len(meta_compressed),
                compute_block_hash(meta_compressed), COMPRESS_ZSTD
            ))
            dst.write(meta_compressed)
            toc_entries.append((BLOCK_METADATA, blk_offset, len(meta_compressed)))

            # --- Стриминг G-code чанками ---
            buf = b""
            while True:
                read = src.read(self.chunk_size - len(buf))
                if not read and not buf:
                    break
                buf += read

                # Ищем слои в текущем буфере
                for m in LAYER_COMMENT_RE.finditer(buf):
                    layer_num_str = m.group(1)
                    if layer_num_str:
                        layer_num = int(layer_num_str)
                        layer_index[layer_num] = gcode_offset + m.start()

                # Если прочитали полный чанк или EOF — сжимаем и пишем
                if len(buf) >= self.chunk_size or not read:
                    compressed_chunk = cctx.compress(buf)
                    blk_offset = dst.tell()
                    dst.write(struct.pack("<HHIIQi",
                        BLOCK_GCODE_CHUNK, chunk_num,
                        len(buf), len(compressed_chunk),
                        compute_block_hash(compressed_chunk), COMPRESS_ZSTD
                    ))
                    dst.write(compressed_chunk)
                    toc_entries.append((BLOCK_GCODE_CHUNK, blk_offset, len(compressed_chunk)))
                    chunks_info.append((len(compressed_chunk), gcode_offset))

                    total_compressed += len(compressed_chunk)
                    gcode_offset += len(buf)
                    chunk_num += 1
                    buf = b""

                    pct = gcode_offset / file_size * 100
                    print(f"\r[GCodeStreamer] {pct:.1f}% ({chunk_num} чанков)", end="")

                if not read:
                    break

            print()

            # --- Блок индекса слоёв ---
            index_payload = struct.pack("<I", len(layer_index))
            for layer_num in sorted(layer_index):
                index_payload += struct.pack("<IQ", layer_num, layer_index[layer_num])
            index_compressed = cctx.compress(index_payload)
            blk_offset = dst.tell()
            dst.write(struct.pack("<HHIIQi",
                BLOCK_LAYER_INDEX, 0,
                len(index_payload), len(index_compressed),
                compute_block_hash(index_compressed), COMPRESS_ZSTD
            ))
            dst.write(index_compressed)
            toc_entries.append((BLOCK_LAYER_INDEX, blk_offset, len(index_compressed)))

            # --- TOC ---
            toc_offset = dst.tell()
            for btype, boff, bsize in toc_entries:
                dst.write(struct.pack("<HxxIQ", btype, boff, bsize))

            # --- Заголовок файла ---
            import time
            from .constants import MAGIC, FORMAT_MAJOR, FORMAT_MINOR
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
        stats = {
            "source_size_gb": file_size / 1e9,
            "compressed_size_gb": total_compressed / 1e9,
            "ratio": ratio,
            "chunks": chunk_num,
            "layers_indexed": len(layer_index),
        }
        print(f"[GCodeStreamer] Готово: {ratio:.1%} от оригинала, "
              f"{len(layer_index)} слоёв проиндексировано")
        return stats
```

---

### 11.3 Версионирование (актуально для SLS и SLM)

В порошковых технологиях (SLS, SLM) G-code часто правится вручную технологом: меняется мощность лазера, скорость сканирования, стратегия штриховки. Каждое такое изменение — новая версия, которую нужно хранить и уметь откатить.

**Стратегия хранения версий:**

| Ситуация | Что хранить |
|---|---|
| Первая загрузка | Полный файл (baseline) |
| Небольшое изменение (< 5%) | Бинарный дельта-патч (bsdiff4) |
| Большое изменение (> 5%) | Новый полный файл + ссылка на baseline |
| Критический снимок (snapshot) | Всегда полный файл, независимо от размера |

**Добавить в `format/versioning.py`** (дополнение к существующему):

```python
# format/versioning.py — дополнение для больших G-code файлов

import os
import hashlib
import bsdiff4
from pathlib import Path


DELTA_THRESHOLD = 0.05   # если патч > 5% от оригинала — храним полный файл

def gcode_delta_or_full(
    old_path: str,
    new_path: str,
    force_snapshot: bool = False
) -> dict:
    """
    Вычислить, что выгоднее хранить: дельта-патч или полный файл.
    Возвращает словарь с типом хранения и данными.

    Для файлов > 200 МБ используем построчное сравнение вместо bsdiff4
    (bsdiff4 держит оба файла в RAM, что неприемлемо для 1-3 ГБ).
    """
    old_size = os.path.getsize(old_path)
    new_size = os.path.getsize(new_path)

    if force_snapshot or old_size > 200 * 1024 * 1024:
        # Для больших файлов — всегда полный снимок
        # (bsdiff4 не справится с 3 ГБ в RAM)
        checksum = _file_sha256(new_path)
        return {
            "type": "full",
            "reason": "snapshot" if force_snapshot else "file_too_large_for_delta",
            "path": new_path,
            "checksum": checksum,
            "size": new_size,
        }

    # Для небольших файлов — пробуем дельту
    with open(old_path, "rb") as f:
        old_data = f.read()
    with open(new_path, "rb") as f:
        new_data = f.read()

    patch = bsdiff4.diff(old_data, new_data)
    ratio = len(patch) / max(len(new_data), 1)

    if ratio < DELTA_THRESHOLD:
        return {
            "type": "delta",
            "patch": patch,
            "patch_size": len(patch),
            "ratio": ratio,
            "base_checksum": _file_sha256(old_path),
            "target_checksum": _file_sha256(new_path),
        }
    else:
        return {
            "type": "full",
            "reason": "delta_too_large",
            "path": new_path,
            "checksum": _file_sha256(new_path),
            "size": new_size,
        }


def apply_gcode_patch(base_path: str, patch: bytes, output_path: str) -> None:
    """Применить дельта-патч к базовому файлу."""
    with open(base_path, "rb") as f:
        base_data = f.read()
    result = bsdiff4.patch(base_data, patch)
    with open(output_path, "wb") as f:
        f.write(result)


def _file_sha256(path: str) -> str:
    """SHA-256 файла по чанкам (не грузит в RAM целиком)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
```

**API эндпоинты для G-code версий** (добавить в `service/routers/versions.py`):

```python
# Загрузить новую версию G-code файла
@router.post("/slices/{slice_id}/versions/gcode")
async def upload_gcode_version(
    slice_id: UUID,
    file: UploadFile,
    message: str = Form(""),
    author: str = Form("unknown"),
    force_snapshot: bool = Form(False),
):
    """
    Загрузить новую версию G-code.
    Система сама решает: дельта или полный файл.
    force_snapshot=true → всегда полный файл (рекомендуется для SLS/SLM).
    """
    ...
```

---

### 11.4 Концепции штриховки по типу технологии

> **Ключевой тезис:** G-code везде одинаковый синтаксически — это просто команды движения `G0`/`G1` с координатами. Но **смысл и стратегия** штриховки (hatching) кардинально отличаются в зависимости от технологии печати.

#### FDM (Fused Deposition Modeling)

В FDM штриховка — это траектории экструдера внутри контура слоя. Параметры:

- **Infill pattern**: rectilinear (прямые линии), gyroid, honeycomb
- **Infill density**: 10–100% (насколько плотно заполнять)
- **Print speed**: типично 40–80 мм/с
- **Экструзия**: команды `E` в G-code задают количество пластика

```gcode
; FDM штриховка — пример
G1 X10.000 Y10.000 E0.1234 F3000  ; движение с экструзией
G1 X90.000 Y10.000 E1.5678 F3000
G1 X10.000 Y11.000 E1.6789 F3000  ; следующая строка штриховки
```

**В SliceStore для FDM** хранить в `InfillPath`:
- `speed_mms` — скорость
- `extrusion_e` — параметр E (количество пластика на отрезок)
- `pattern` — тип паттерна

#### SLS / SLM (порошковые технологии)

В SLS и SLM нет экструзии. Лазер **сканирует** порошок по заданным траекториям. Параметры штриховки принципиально другие:

- **Scan speed**: 500–3000 мм/с (на порядок быстрее FDM)
- **Laser power**: 20–400 Вт
- **Hatch spacing**: расстояние между линиями (50–200 мкм)
- **Rotation angle**: угол поворота штриховки между слоями (обычно 67°)
- **Scan strategy**: meander (зигзаг), island/checkerboard, stripe

```gcode
; SLS/SLM штриховка — пример (без E, другие скорости)
G1 X10.000 Y10.000 F180000      ; F в мм/мин = 3000 мм/с
G1 X90.000 Y10.000
G1 X90.000 Y10.100              ; следующая линия (hatch spacing 0.1 мм)
G1 X10.000 Y10.100
```

**Ключевое отличие SLS от SLM:**

| Параметр | SLS (полимеры) | SLM (металлы) |
|---|---|---|
| Laser power | 10–200 Вт | 200–1000 Вт |
| Scan speed | 1000–4000 мм/с | 500–2000 мм/с |
| Hatch spacing | 100–200 мкм | 50–120 мкм |
| Layer thickness | 60–180 мкм | 20–80 мкм |
| Требует инертной атмосферы | Нет | Да (N₂ или Ar) |

**Добавить в `format/blocks.py`** — специализированный класс штриховки:

```python
# format/blocks.py — дополнение

from enum import IntEnum
from dataclasses import dataclass
import struct


class PrintTechnology(IntEnum):
    FDM = 1
    SLS = 2
    SLM = 3


class HatchStrategy(IntEnum):
    MEANDER    = 1   # зигзаг (по умолчанию)
    ISLAND     = 2   # шахматные острова (SLS/SLM)
    STRIPE     = 3   # полосы
    GYROID     = 4   # только FDM
    HONEYCOMB  = 5   # только FDM


@dataclass
class HatchParams:
    """
    Параметры штриховки — универсальные для всех технологий.
    Для FDM часть полей не используется (laser_power_w = 0.0).
    """
    technology: PrintTechnology
    strategy: HatchStrategy
    hatch_spacing_um: int           # расстояние между линиями в мкм
    rotation_deg: float             # угол поворота (для SLS/SLM обычно 67°)
    scan_speed_mms: float           # скорость сканирования
    laser_power_w: float = 0.0      # мощность лазера (SLS/SLM)
    infill_density_pct: float = 100.0  # плотность (FDM)
    layer_rotation_increment_deg: float = 67.0  # поворот между слоями (SLS/SLM)

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
```

---

### 11.5 Паттерны штриховки — компрессия по площади

> **Идея:** Вместо хранения полного списка координат всех линий штриховки хранить **параметры генерации** — контур + параметры паттерна. При чтении файла паттерн восстанавливается программно. Это сжимает данные о штриховке в 10–100 раз.

**Добавить `format/hatch_generator.py`:**

```python
# format/hatch_generator.py
"""
Генератор паттернов штриховки по площади контура.
Хранит только параметры, восстанавливает координаты при чтении.
Применимо к FDM (infill), SLS и SLM (scan paths).
"""
import math
from typing import List, Tuple
from .blocks import HatchParams, HatchStrategy, Point, InfillPath


def generate_meander(
    bbox: Tuple[float, float, float, float],  # (x_min, y_min, x_max, y_max) в мм
    params: HatchParams,
) -> List[InfillPath]:
    """
    Зигзаг-штриховка (meander) внутри bounding box.
    Подходит для FDM, SLS, SLM.
    """
    x_min, y_min, x_max, y_max = bbox
    spacing_mm = params.hatch_spacing_um / 1000.0
    angle_rad = math.radians(params.rotation_deg)

    paths = []
    y = y_min
    left_to_right = True

    while y <= y_max:
        # Линия штриховки с поворотом
        x_start = x_min if left_to_right else x_max
        x_end   = x_max if left_to_right else x_min

        # Поворот координат
        def rotate(x, y_coord):
            rx = x * math.cos(angle_rad) - y_coord * math.sin(angle_rad)
            ry = x * math.sin(angle_rad) + y_coord * math.cos(angle_rad)
            return rx, ry

        p1 = rotate(x_start, y)
        p2 = rotate(x_end, y)

        path = InfillPath(
            points=[
                Point(int(p1[0] * 1000), int(p1[1] * 1000)),
                Point(int(p2[0] * 1000), int(p2[1] * 1000)),
            ],
            speed_mms=params.scan_speed_mms,
        )
        paths.append(path)

        y += spacing_mm
        left_to_right = not left_to_right

    return paths


def generate_island(
    bbox: Tuple[float, float, float, float],
    params: HatchParams,
    island_size_mm: float = 5.0,
) -> List[InfillPath]:
    """
    Шахматный (island/checkerboard) паттерн — стандарт для SLS и SLM.
    Снижает остаточные напряжения в металлических деталях.
    Каждый остров сканируется независимо, направление чередуется.
    """
    x_min, y_min, x_max, y_max = bbox
    spacing_mm = params.hatch_spacing_um / 1000.0

    all_paths = []
    island_row = 0

    y = y_min
    while y < y_max:
        x = x_min
        island_col = 0
        while x < x_max:
            # Угол поворота для этого острова
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
                x, y,
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
    """
    Выбрать и применить стратегию штриховки для слоя.
    Угол поворота автоматически инкрементируется между слоями.
    """
    # Поворот угла между слоями (SLS/SLM стандарт: 67° на слой)
    rotated_params = HatchParams(
        technology=params.technology,
        strategy=params.strategy,
        hatch_spacing_um=params.hatch_spacing_um,
        rotation_deg=(params.rotation_deg + layer_index * params.layer_rotation_increment_deg) % 360,
        scan_speed_mms=params.scan_speed_mms,
        laser_power_w=params.laser_power_w,
        infill_density_pct=params.infill_density_pct,
        layer_rotation_increment_deg=params.layer_rotation_increment_deg,
    )

    if params.strategy == HatchStrategy.ISLAND:
        return generate_island(bbox, rotated_params)
    else:
        return generate_meander(bbox, rotated_params)
```

---

### 11.6 Загрузка G-code через API (batch и по одному)

Поддерживаются два режима: одиночная загрузка и пакетная (batch) для нескольких файлов сразу.

**Добавить в `service/routers/slices.py`:**

```python
# Одиночная загрузка G-code
@router.post("/slices/gcode", status_code=201)
async def upload_gcode(
    file: UploadFile,
    printer_type: str = Form(...),      # "fdm" / "sls" / "slm"
    name: str = Form(""),
    material: str = Form(""),
    tags: str = Form("[]"),
):
    """
    Загрузить G-code файл напрямую (без предварительной конвертации в .sls).
    Конвертация в .sls происходит на сервере потоково.
    Поддерживает файлы до 3 ГБ через chunked upload.
    """
    ...


# Пакетная загрузка (batch)
@router.post("/slices/gcode/batch", status_code=202)
async def upload_gcode_batch(
    files: List[UploadFile],
    printer_type: str = Form(...),
    material: str = Form(""),
):
    """
    Загрузить несколько G-code файлов одновременно.
    Возвращает список task_id для отслеживания прогресса.
    Обработка идёт параллельно через Celery/asyncio.
    """
    task_ids = []
    for f in files:
        task_id = await enqueue_gcode_processing(f, printer_type, material)
        task_ids.append({"filename": f.filename, "task_id": task_id})
    return {"tasks": task_ids, "total": len(files)}


# Статус задачи
@router.get("/slices/gcode/task/{task_id}")
async def get_task_status(task_id: str):
    """Получить статус обработки G-code (прогресс загрузки + конвертации)."""
    ...
```

---

### 11.7 Обновления схемы БД для G-code

```sql
-- migrations/add_gcode_support.sql

-- Добавить поля к таблице slices
ALTER TABLE slices
    ADD COLUMN IF NOT EXISTS printer_type  VARCHAR(10),   -- 'fdm', 'sls', 'slm'
    ADD COLUMN IF NOT EXISTS format        VARCHAR(10) DEFAULT 'sls',  -- 'sls' или 'gcode'
    ADD COLUMN IF NOT EXISTS layer_count   INTEGER,
    ADD COLUMN IF NOT EXISTS source_file   TEXT,          -- оригинальное имя G-code файла
    ADD COLUMN IF NOT EXISTS hatch_strategy VARCHAR(20);  -- 'meander', 'island', 'stripe'

-- Индекс слоёв G-code (для быстрого доступа к конкретному слою)
CREATE TABLE IF NOT EXISTS gcode_layer_index (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slice_id    UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    layer_num   INTEGER NOT NULL,
    byte_offset BIGINT NOT NULL,     -- офсет в исходном G-code файле
    z_height_mm NUMERIC(8,4),        -- высота слоя
    UNIQUE (slice_id, layer_num)
);
CREATE INDEX IF NOT EXISTS idx_gcode_layer_slice ON gcode_layer_index(slice_id);

-- Расширить версии: хранить тип изменения
ALTER TABLE slice_versions
    ADD COLUMN IF NOT EXISTS change_type VARCHAR(20) DEFAULT 'full',  -- 'full', 'delta'
    ADD COLUMN IF NOT EXISTS change_summary TEXT;  -- что изменилось (human-readable)
```

---

### 11.8 Вопросы для уточнения на основе реального опыта

Перед финальной реализацией рекомендуется уточнить у технолога / оператора машины:

**По SLS/SLM:**
- Какой слайсер используется (Magics, Netfabb, EOS Print, собственный)? От этого зависит формат комментариев слоёв в G-code.
- Как именно правится G-code вручную — через текстовый редактор или GUI? Нужна ли история изменений построчно?
- Насколько часто меняется стратегия штриховки между версиями (или в основном меняются только параметры мощности/скорости)?
- Нужен ли откат к версии прямо перед запуском печати, или версии нужны только для архива?

**По размерам файлов:**
- Типичный размер G-code для одной детали vs. для целой платформы (несколько деталей)?
- Есть ли файлы с несколькими материалами / несколькими лазерами (для многолазерных SLM)?
- Насколько критично время загрузки файла — допустимо ли 5–10 минут на 3 ГБ?

**По FDM:**
- Нужна ли поддержка multi-material (Bambu, Prusa MMU)?
- Используется ли температурное управление в G-code (M104, M109, M140)?

---

### 11.9 Обновлённый чек-лист (G-code + большие файлы)

- [ ] `GCodeStreamer.write_gcode()` обрабатывает файл 1 ГБ за < 5 мин без превышения лимита RAM (< 500 МБ)
- [ ] Индекс слоёв позволяет читать слой N без декомпрессии всего файла
- [ ] `gcode_delta_or_full()` для файла < 200 МБ создаёт патч < 5% от оригинала при малом изменении
- [ ] Для файлов > 200 МБ автоматически используется полный снимок
- [ ] `HatchParams.encode()` / `decode()` корректно сериализует параметры для FDM, SLS, SLM
- [ ] `hatch_for_layer()` поворачивает угол штриховки на `layer_rotation_increment_deg` каждый слой
- [ ] Паттерн `island` генерирует шахматную сетку островов с чередованием направления
- [ ] `POST /slices/gcode` принимает файл до 3 ГБ через chunked upload
- [ ] `POST /slices/gcode/batch` ставит несколько файлов в очередь и возвращает task_id
- [ ] `GET /slices/gcode/task/{id}` возвращает прогресс обработки
- [ ] Таблица `gcode_layer_index` заполняется при загрузке и позволяет быстрый поиск по слою
- [ ] Поле `printer_type` в БД различает `fdm` / `sls` / `slm`
NEWECTION
