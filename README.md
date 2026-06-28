# SliceStore

SliceStore - сервис хранения, версионирования и поиска данных слайсинга для 3D-печати. Проект умеет принимать `.sls` файлы, конвертировать G-code в внутренний `.sls` формат, хранить версии, создавать delta-обновления и отдавать метаданные через REST API.

## Возможности

- Загрузка и скачивание `.sls` файлов.
- Загрузка одиночного и пакетного G-code.
- Версионирование файлов с поддержкой snapshot/delta хранения.
- Поиск по имени, материалу, принтеру и тегам.
- Хранение метаданных в PostgreSQL.
- Хранение бинарных файлов в MinIO.
- Кеширование метаданных и статусов задач в Redis.
- Docker Compose окружение для локального запуска.

## Структура проекта

```text
.
├── client/              # Простой Python-клиент
├── format/              # Чтение, запись, сжатие и delta-логика .sls формата
├── service/             # FastAPI сервис
│   ├── routers/         # REST endpoints
│   └── Dockerfile
├── migrations/          # SQL-инициализация базы
├── benchmarks/          # Скрипты бенчмарков
├── tests/               # Pytest тесты формата и API
├── docker-compose.yml
└── requirements.txt
```

## Быстрый старт

1. Создайте `.env` в корне проекта:

```env
POSTGRES_USER=slicestore
POSTGRES_PASSWORD=slicestore123
POSTGRES_DB=slicestore
DATABASE_URL=postgresql://slicestore:slicestore123@postgres:5432/slicestore

MINIO_ENDPOINT=minio:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
MINIO_BUCKET=slices

REDIS_URL=redis://redis:6379/0
```

2. Запустите окружение:

```bash
docker compose up -d --build
```

3. Проверьте API:

```bash
curl http://localhost:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok","service":"slicestore"}
```

Документация FastAPI будет доступна по адресу:

- `http://localhost:8000/docs`
- `http://localhost:8000/redoc`

## Основные API endpoints

- `GET /health` - проверка состояния сервиса.
- `POST /slices/` - загрузить `.sls` файл.
- `GET /slices/{slice_id}` - скачать актуальную версию файла.
- `GET /slices/{slice_id}/metadata` - получить метаданные файла.
- `DELETE /slices/{slice_id}` - мягко удалить файл.
- `GET /slices/search` - поиск по метаданным.
- `GET /slices/{slice_id}/versions` - список версий.
- `POST /slices/{slice_id}/versions` - добавить новую `.sls` версию.
- `GET /slices/{slice_id}/versions/{version_num}` - скачать конкретную версию.
- `POST /slices/{slice_id}/restore/{version_num}` - восстановить версию.
- `POST /slices/gcode` - загрузить и конвертировать G-code.
- `POST /slices/gcode/batch` - поставить пакет G-code файлов в обработку.
- `GET /slices/gcode/task/{task_id}` - получить статус пакетной обработки.

## Примеры запросов

Загрузка `.sls` файла:

```bash
curl -X POST http://localhost:8000/slices/ \
  -F "file=@example.sls" \
  -F "name=Test print" \
  -F "material=PLA" \
  -F 'tags=["test","pla"]'
```

Поиск:

```bash
curl "http://localhost:8000/slices/search?q=Test&material=PLA"
```

Загрузка G-code:

```bash
curl -X POST http://localhost:8000/slices/gcode \
  -F "file=@example.gcode" \
  -F "printer_type=fdm" \
  -F "name=G-code print" \
  -F "material=PLA" \
  -F "tags=[]"
```

## Локальная разработка

Для запуска тестов формата:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pytest tests/test_format.py tests/test_gcode.py
```

API-тесты требуют запущенный Docker Compose:

```bash
docker compose up -d --build
pytest tests/test_api.py
```

## Переменные окружения

Сервис читает настройки из `.env` через `pydantic-settings`.

| Переменная | Назначение |
| --- | --- |
| `DATABASE_URL` | DSN подключения к PostgreSQL |
| `MINIO_ENDPOINT` | Endpoint MinIO |
| `MINIO_ROOT_USER` | Пользователь MinIO |
| `MINIO_ROOT_PASSWORD` | Пароль MinIO |
| `MINIO_BUCKET` | Bucket для хранения `.sls` и patch-файлов |
| `REDIS_URL` | URL Redis |

Файл `.env` намеренно исключен из Git, чтобы не публиковать локальные секреты.

