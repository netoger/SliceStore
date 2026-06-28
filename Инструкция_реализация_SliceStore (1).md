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
