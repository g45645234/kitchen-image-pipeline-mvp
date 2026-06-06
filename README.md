# Kitchen Image Pipeline MVP

Lightweight MVP для поиска, ревью и экспорта референсных картинок (кандидатов) для исправления ошибок кухонного дизайна в видеороликах.

## Архитектура
* **Backend:** FastAPI (Python 3.11+)
* **Database:** PostgreSQL + SQLAlchemy 2.0 (async) + Alembic
* **Background Jobs:** Outbox/Job Queue pattern (In-Process worker)
* **Storage:** Локальная файловая система (`./storage`, `./exports`)

## Требования
* Docker и Docker Compose
* (Опционально) Python 3.11+, если вы запускаете backend локально без Docker.

## Быстрый старт (Запуск через Docker)

Для полноценной работы базы данных и приложения:

1. **Создайте файл `.env`** (если его еще нет):
   ```bash
   cp .env.example .env
   ```
2. **Поднимите контейнеры**:
   ```bash
   docker compose up -d
   ```
   *Запустятся: БД PostgreSQL (`db`) и само приложение (`web`).*

3. **Сгенерируйте и накатите миграции БД** (так как мы добавили новые модели):
   ```bash
   # Генерация первой миграции на основе моделей:
   docker compose exec web alembic revision --autogenerate -m "init_models"
   
   # Применение миграции (создание таблиц):
   docker compose exec web alembic upgrade head
   ```

4. **Откройте Swagger UI**:
   Перейдите в браузере по адресу: **http://localhost:8000/docs**

## Сценарий использования (User Flow)

MVP поддерживает следующий жизненный цикл обработки данных:

1. **Создание видео и ошибки**:
   - `POST /api/videos` -> создаем сущность видео.
   - Заходим в БД (или через админку, если бы она была реализована) и привязываем `Mistake` к созданному `video_id`.

2. **Запуск поиска кандидатов**:
   - `POST /api/mistakes/{mistake_id}/candidates/search`
   - *Это создаст Job (`search_all_queries`), который подхватит фоновый воркер.*
   - *Воркер сгенерирует поисковые запросы и моковых кандидатов с картинками.*

3. **Просмотр и ревью**:
   - `GET /api/mistakes/{mistake_id}/candidates` -> получаем список найденных кандидатов.
   - `POST /api/candidates/{candidate_id}/review` (body: `{"action": "approve"}`)
   - *Создаст Job (`review_candidate`), воркер скопирует кандидата в таблицу `FinalAsset`.*

4. **Экспорт финального манифеста**:
   - `POST /api/videos/{video_id}/export`
   - *Создаст Job (`export_final_assets`), который соберет все утвержденные картинки для видео и создаст JSON-файл в папке `./exports/`.*

## Структура проекта

```text
├── alembic/                # Файлы конфигурации миграций БД
├── app/
│   ├── main.py             # Точка входа FastAPI, запуск background worker
│   ├── config.py           # Конфигурация (Pydantic BaseSettings)
│   ├── db.py               # Подключение к БД, базовый класс SQLAlchemy
│   ├── models/             # SQLAlchemy модели таблиц
│   ├── schemas/            # Pydantic схемы (Валидация API и Job Payloads)
│   ├── routers/            # Маршруты API
│   └── services/           # Бизнес-логика (Поиск, Хранилище, Ревью, Экспорт)
├── docker-compose.yml      # Конфигурация Docker
└── pyproject.toml          # Зависимости проекта
```
