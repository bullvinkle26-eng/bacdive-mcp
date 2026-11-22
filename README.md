# BacDive Remote MCP Server 🦠🔬

Удаленный сервер протокола [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) для работы с официальным открытым REST API **DSMZ BacDive v2** ([The Bacterial Diversity Metadatabase](https://api.bacdive.dsmz.de/v2/)).

Сервер построен на базе **FastMCP**, работает полностью асинхронно через `httpx`, поддерживает транспорт **Streamable HTTP** (а также SSE), оснащен HTTP-эндпоинтами для проверки здоровья (`/health`) и полностью готов к мгновенному деплою на **Render.com** (или любой другой PaaS / Docker-контейнер).

---

## 📋 Особенности и возможности

- **Открытый REST API v2**: Начиная со второй версии API BacDive авторизация Keycloak и токены **не требуются** — запросы выполняются напрямую.
- **Поддержка Streamable HTTP и SSE**: Подходит для любых современных MCP-клиентов (Claude Desktop, Cursor, Windsurf, Cline, Continue и др.).
- **Оптимизация токенов контекста LLM**: Инструмент `get_strain_details` поддерживает фильтрацию по разделам (`section`), позволяя запрашивать только морфологию, условия роста, метаболизм или геномные данные, экономя десятки тысяч токенов.
- **Интеллектуальный поиск по сиквенсам**: Автоматическое определение типа последовательности (16S рРНК vs сборка генома NCBI/GenBank) при передаче параметра `sequence_type="auto"`.
- **Защита доступа (API Key / Bearer)**: Возможность закрыть сервер секретным ключом через переменную окружения `MCP_API_KEY`. При этом эндпоинт `/health` остается открытым для Keep-Alive пингов (UptimeRobot).
- **Готов к деплою на Render.com**: Автоматически слушает порт из переменной окружения `PORT` (`0.0.0.0:$PORT`) и содержит эндпоинт `/health` для проверки доступности.

---

## 🛠 Доступные MCP Инструменты

| Инструмент | Описание | Основные аргументы |
|---|---|---|
| `search_by_taxon` | Поиск штаммов по таксономической иерархии с пагинацией | `genus` (обязательный), `species`, `subspecies`, `page` |
| `search_by_culture_collection` | Поиск по номеру штамма в коллекции культур | `culture_collection_no` (напр. `"DSM 30083"`, `"ATCC 6051"`), `page` |
| `search_by_sequence_accession` | Поиск по номеру последовательности (16S рРНК или геном) | `accession` (напр. `"AF000162"`, `"GCA_000243275"`), `sequence_type` (`"auto"`, `"16s"`, `"genome"`) |
| `get_strain_details` | Получение подробных данных штамма по BacDive ID с фильтром разделов | `strain_id` (напр. `15259`), `section` (напр. `"Morphology"`, `"growth"`, `"metabolism"`, `"General"`) |

### Доступные разделы карточки штамма для `section`:
- `General` — общая информация, обозначение, видовой эпитет;
- `Name and taxonomic classification` (алиас: `taxonomy`) — полная таксономия и ссылки на LPSN;
- `Morphology` — форма клеток, подвижность, окраска по Граму;
- `Culture and growth conditions` (алиасы: `growth`, `culture`) — температуры, среды DSMZ, pH, отношение к кислороду;
- `Physiology and metabolism` (алиасы: `metabolism`, `physiology`) — ферменты, субстраты, антибиотикорезистентность;
- `Isolation, sampling and environmental information` (алиас: `isolation`) — географическое происхождение, среда выделения;
- `Interaction and safety` (алиас: `safety`) — класс патогенности (Biosafety level / Risk group);
- `Sequence information` (алиасы: `sequence`, `16s`) — номера 16S рРНК в GenBank, ссылки на сборки;
- `Genome-based predictions` — фенотипические предсказания на основе генома;
- `Literature` — публикации и цитирования PubMed;
- `Reference` — ссылки на источники данных внутри DSMZ.

---

## 🚀 Деплой на Render.com

### Вариант 1: Через Blueprint (`render.yaml`) — Рекомендуемый
1. Создайте репозиторий на GitHub/GitLab и загрузите файлы проекта:
   - `bacdive_server.py`
   - `requirements.txt`
   - `render.yaml`
2. Перейдите в [Render Dashboard](https://dashboard.render.com/) -> **Blueprints** -> **New Blueprint Instance**.
3. Подключите ваш репозиторий. Render автоматически считает параметры из `render.yaml` и запустит деплой.

### Вариант 2: Ручная настройка Web Service в панели Render
1. Нажмите **New +** -> **Web Service**.
2. Подключите репозиторий с кодом.
3. Укажите параметры конфигурации:
   - **Name**: `bacdive-mcp-server`
   - **Language / Runtime**: `Python`
   - **Region**: `Frankfurt` (ближе к серверам DSMZ в Германии) или любой другой
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bacdive_server.py`
   - **Instance Type**: `Free`
4. В разделе **Advanced** настройте:
   - **Health Check Path**: `/health`
   - **Environment Variables**:
     - `PORT`: `10000` (Render также автоматически прокидывает системный `$PORT`)
     - `MCP_TRANSPORT`: `http` (или `sse`)
     - `BACDIVE_TIMEOUT`: `30.0` (опционально)
5. Нажмите **Deploy Web Service**.

После сборки Render предоставит публичный URL, например:
`https://bacdive-mcp-server.onrender.com`

Проверить работоспособность:
```bash
curl https://bacdive-mcp-server.onrender.com/health
# Ответ: {"status":"healthy","service":"bacdive-mcp-server",...}
```

---

## 🔌 Подключение к MCP-клиентам

Эндпоинт MCP будет доступен по адресу:
- Для Streamable HTTP: `https://bacdive-mcp-server.onrender.com/mcp`
- Для SSE (если `MCP_TRANSPORT=sse`): `https://bacdive-mcp-server.onrender.com/sse`

### Настройка в Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "bacdive": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://bacdive-mcp-server.onrender.com/mcp",
        "--header",
        "Authorization: Bearer YOUR_MCP_API_KEY"
      ]
    }
  }
}
```

### Настройка в Cursor / Windsurf / Cline
Укажите:
- **URL**: `https://bacdive-mcp-server.onrender.com/mcp`
- **Headers**:
  ```json
  {
    "Authorization": "Bearer YOUR_MCP_API_KEY"
  }
  ```

*(Если клиент поддерживает нативное указание HTTP/SSE URL без локального моста, укажите URL напрямую в настройках клиента)*.

---

## 💻 Локальный запуск и разработка

```bash
# 1. Клонирование / переход в директорию
cd bacdive-mcp

# 2. Создание и активация виртуального окружения
python -m venv .venv
source .venv/bin/activate  # На Windows: .venv\Scripts\activate

# 3. Установка зависимостей
pip install -r requirements.txt

# 4. Запуск сервера
python bacdive_server.py
```

Сервер запустится на `http://0.0.0.0:8000`. Вы можете проверить:
- Health check: `http://localhost:8000/health`
- Корневой статус: `http://localhost:8000/`
- MCP эндпоинт: `http://localhost:8000/mcp`
