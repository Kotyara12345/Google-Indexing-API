

## Что это

Настольное приложение для Windows, которое через Google Indexing API отправляет URL-адреса сайта на переиндексацию поисковым роботом Google. Работает с ключом сервисного аккаунта Google Cloud, ведёт строгую проверку домена и выдаёт отчёт о результатах.

## Возможности

- **Выбор JSON-ключа** сервисного аккаунта через диалог. Путь сохраняется в `config.ini` рядом с программой и подставляется при следующем запуске.
- **Поле домена** с автоматической нормализацией: можно вводить `example.com` или `https://example.com` — оба варианта приводятся к корректному виду.
- **Многострочное поле URL** — список по одному адресу в строке, плюс кнопка загрузки из `.txt`-файла.
- **Строгая проверка домена.** Учитываются и схема (`http` / `https`), и хост целиком. `http://example.com`, `https://example.com`, `https://www.example.com`, `https://blog.example.com` — четыре разных сайта.
- **Лимит 100 URL за раз** со счётчиком в реальном времени. При превышении отправка блокируется.
- **Пакетная отправка (batch).** URL группируются по 50 и отправляются одним HTTP-запросом через `BatchHttpRequest`. При недоступности batch-эндпоинта автоматический откат на последовательный режим.
- **Фоновая отправка** в отдельном потоке. Интерфейс остаётся отзывчивым, работает кнопка **«Отмена»**.
- **Итоговый отчёт** в логе и в диалоговом окне:
  - всего строк в списке;
  - успешно отправлено;
  - чужой домен (не отправлены);
  - невалидные строки;
  - отклонено API.
- **Валидация JSON-ключа** перед первым обращением к API: проверяются тип `service_account` и обязательные поля (`client_email`, `private_key`, `token_uri`, `project_id`).
- **Обработка сетевых сбоев** по каждому URL отдельно — одна битая ссылка не роняет весь пакет.

## Технические требования

- **ОС:** Windows 10/11 (64-бит).
- **Python:** 3.10 или новее. Обязательно компонент **tcl/tk and IDLE** (нужен для Tkinter).
- **Google Cloud:** проект с включённым Indexing API, сервисный аккаунт с JSON-ключом, добавленный владельцем в Search Console нужного домена.
- **Интернет:** постоянное подключение.

## Структура проекта

```
E:\indexator\
├── main.py
├── gui.py
├── indexing_client.py
├── url_utils.py
└── config_store.py
```

| Файл | Назначение |
|---|---|
| `main.py` | Точка входа, создаёт Tk-окно и запускает приложение. |
| `gui.py` | Интерфейс Tkinter, обработка событий, фоновая отправка, отчёт. |
| `indexing_client.py` | Клиент Google Indexing API, валидация ключа, batch и последовательная отправка. |
| `url_utils.py` | Нормализация домена, сборка полных URL, строгое сравнение сайтов. |
| `config_store.py` | Чтение и запись `config.ini` (путь к JSON-ключу). |

Рядом с программой после первого запуска появится:

- `config.ini` — сохранённый путь к ключу.
- `key.json` — файл сервисного аккаунта (кладёте сами).

## Установка из исходников

### 1. Установка Python

Скачайте Python 3.10+ с [python.org/downloads](https://www.python.org/downloads/windows/) и запустите установщик. Обязательно:

- галочка **Add python.exe to PATH**;
- компонент **tcl/tk and IDLE** (в разделе Optional Features).

Проверка в новом окне PowerShell:

```powershell
python --version
python -c "import tkinter; print('tk', tkinter.TkVersion)"
```

Обе команды должны отработать без ошибок.

### 2. Установка зависимостей

```powershell
python -m pip install --upgrade pip
python -m pip install google-auth google-auth-httplib2 google-api-python-client pyinstaller
```

Проверка:

```powershell
python -c "import google.oauth2.service_account, googleapiclient.discovery; print('ok')"
```

Должно вывестись `ok`.

### 3. Создание файлов проекта

Создайте папку, например `E:\indexator\`, и положите туда пять `.py`-файлов из раздела «Структура проекта». Проще всего через **VS Code**:

1. Скачайте и установите [VS Code](https://code.visualstudio.com/).
2. **File → Open Folder** → выберите `E:\indexator`.
3. Создайте каждый файл (**File → New File**), вставьте код, сохраните (`Ctrl+S`).

Проверка, что файлы не пустые:

```powershell
dir E:\indexator\*.py
```

Все пять должны иметь ненулевой размер (от ~200 байт до ~10 КБ).

### 4. Первый запуск из исходников

```powershell
cd E:\indexator
python main.py
```

Должно открыться окно с заголовком «Google Indexing API — переиндексация». Если окно появилось — можно переходить к сборке `.exe`.

## Сборка `.exe`

В папке с исходниками:

```powershell
cd E:\indexator
python -m PyInstaller --onefile --windowed --name="Indexer" main.py
```

Флаги:

- `--onefile` — один `.exe`-файл без папки с библиотеками.
- `--windowed` — без чёрного окна консоли.
- `--name="Indexer"` — имя итогового файла.

Готовый `Indexer.exe` появится в `E:\indexator\dist\`. Размер — 30–60 МБ (внутри Python, Tkinter и Google-клиент). Папки `build\` и файл `Indexer.spec` можно удалить, они нужны только для пересборки.

### Если PyInstaller ругается на отсутствие модулей Google

Пересоберите с явными скрытыми импортами (одной строкой в PowerShell):

```powershell
python -m PyInstaller --onefile --windowed --name="Indexer" --hidden-import=google.auth --hidden-import=google.auth.transport.requests --hidden-import=google.oauth2.service_account --hidden-import=googleapiclient.discovery --hidden-import=googleapiclient.discovery_cache --hidden-import=googleapiclient.http --hidden-import=googleapiclient.errors main.py
```

### Если Windows SmartScreen блокирует `.exe`

«Подробнее» → «Выполнить в любом случае». Это нормально для неподписанных самосборных бинарников. Если антивирус удаляет файл — добавьте `E:\indexator\dist` в исключения Защитника Windows.

## Получение ключа `key.json`

1. Откройте [Google Cloud Console](https://console.cloud.google.com/) и создайте новый проект (или используйте существующий).
2. **APIs & Services → Library** → найдите **Indexing API** → **Enable**.
3. **APIs & Services → Credentials → Create Credentials → Service account**.
   - Имя: например `indexer`.
   - Роли можно не назначать.
   - **Done**.
4. Откройте созданный сервисный аккаунт → вкладка **Keys** → **Add Key → Create new key → JSON**. Скачается файл — переименуйте его в `key.json`.
5. Скопируйте из JSON поле `client_email` (например, `indexer@my-project.iam.gserviceaccount.com`).
6. Откройте [Google Search Console](https://search.google.com/search-console/) → выберите сайт → **Settings → Users and permissions → Add user** → вставьте `client_email` с правами **Owner**.

Без шага 6 API будет возвращать `403 PERMISSION_DENIED`.

## Использование

1. Запустите `Indexer.exe` двойным щелчком.
2. Нажмите **«Выбрать…»** и укажите путь к `key.json`. Путь сохранится в `config.ini`.
3. Введите домен **со схемой**: `https://example.com` (без слэша на конце).
4. Вставьте URL в поле — по одному в строке. Можно полные (`https://example.com/page`) или относительные (`page`). При относительных домен подставится автоматически.
5. Счётчик под полем покажет количество. Если больше 100 — подсветится красным, отправка заблокируется.
6. Нажмите **«Отправить на переиндексацию»**. Интерфейс остаётся живым, при необходимости — **«Отмена»**.
7. Дождитесь итогового отчёта:

```
Всего строк:                 100
Успешно отправлено:          28
Чужой домен (не отправлены): 0
Невалидные строки:           0
Отклонено API:               72
```

## Важные ограничения Google

- **Дневная квота — 200 URL на проект.** Считается по URL, а не по HTTP-запросам. Batch-режим ускоряет работу, но квоту не увеличивает. Сбрасывается в 00:00 по PST (≈10:00–11:00 Минск).
- **Типы страниц.** Indexing API официально предназначен для страниц с разметкой `JobPosting` (вакансии) и `BroadcastEvent` (видео). Для обычных товарных карточек и статей Google может принять запрос с `200 OK`, но фактическую переиндексацию не гарантирует.
- **Проверка результата.** Факт переиндексации проверяется в Search Console → **Индексирование → Страницы** или через **Проверку URL** (введите адрес, посмотрите «Последнее сканирование»). Не раньше чем через 1–3 дня после отправки.
- **Ошибка 429.** Означает исчерпание дневной квоты. Сохраните отклонённые URL в отдельный файл и отправьте их после сброса квоты — повторная отправка ранее непринятых URL безопасна.

## Частые проблемы

| Симптом | Причина | Решение |
|---|---|---|
| `'pyinstaller' is not recognized` | Папка `Scripts` Python не в `PATH` | Используйте `python -m PyInstaller …` |
| `python` не распознан | Python без галочки Add to PATH | Переустановите Python с галочкой |
| `ModuleNotFoundError: No module named 'google'` | Не установлены Google-клиенты | `python -m pip install google-auth google-auth-httplib2 google-api-python-client`, затем пересборка |
| `.exe` мгновенно закрывается | Ошибка при старте | Соберите debug-версию без `--windowed`, запустите из консоли — увидите traceback |
| `403 PERMISSION_DENIED` | `client_email` не добавлен в Search Console | Добавьте как владельца сайта |
| `429 RATE_LIMIT_EXCEEDED` | Дневная квота 200 URL исчерпана | Дождитесь сброса в 00:00 PST или создайте второй Google Cloud проект |

## Обновление версии

Если правите исходники — просто пересоберите `.exe`:

```powershell
cd E:\indexator
python -m PyInstaller --onefile --windowed --name="Indexer" main.py
```

PyInstaller перезапишет `dist\Indexer.exe` и `build\`. Файл `config.ini` рядом с `.exe` не трогается, путь к ключу сохранится.
