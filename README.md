# OpenCode Bridge 🌉

**Use any OpenCode Zen model (Muse Spark, Big Pickle, Ling, MiMo, Nemotron…) from Claude Code or any OpenAI client — free, local, no API keys.**

[Русская версия](https://github.com/tavrida1337-sketch/OpenCode-Most#%D1%80%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-)

---

## What is this?

OpenCode Desktop runs a hidden local server with all your chats and models. **OpenCode Bridge** finds that server automatically and exposes it as:

- ✅ **OpenAI-compatible API** → `POST /v1/chat/completions` (streaming supported)
- ✅ **Anthropic-compatible API** → `POST /v1/messages` (this is what **Claude Code** speaks)
- ✅ **Web dashboard** → pick the session, pick the Zen model, test prompts, live status

The model that answers is **the one selected in OpenCode** — switch it in the app (or on the dashboard) and the next request follows instantly.

No dependencies. One folder. Pure Python stdlib.

## ✨ Features

| | |
|---|---|
| 🤖 **Claude Code integration** | Point it at the bridge, pick the `OpenCode` model, code with free Zen models |
| 🧠 **Follows your picker** | Change the model at the bottom of OpenCode → next request uses it; or force one from the site dropdown |
| 🌐 **Zen model list** | Big Pickle, Ling 3.0, MiMo V2.5, Muse Spark 1.2/1.3, Nemotron 3 Ultra/3.5… — same catalogue as the desktop picker |
| 📂 **Session picker** | Requests go into the session whose folder you have open — edits land in the right project |
| 🔄 **Auto-discovery** | Finds the server port (TCP table) + password (process env) by itself, re-discovers on rotation |
| 📊 **Health page** | Plain-text `GET /status` → `status api:work` / `status api:no work (error: …)` |
| 🛡️ **Resilient** | Retries fast network drops, polls for late answers, never crashes on upstream errors |

## 🚀 Quick start

1. OpenCode Desktop must be running (any chat open).
2. Run **`start.bat`** — the bridge starts in the background, the dashboard opens at `http://127.0.0.1:8000/`.
3. For Claude Code:

```bat
set ANTHROPIC_BASE_URL=http://127.0.0.1:8000
set ANTHROPIC_API_KEY=any
set ANTHROPIC_MODEL=OpenCode
```

Or persist in `~/.claude/settings.json`:
```json
{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
         "ANTHROPIC_API_KEY": "any",
         "ANTHROPIC_MODEL": "OpenCode"}}
```

For OpenAI clients:
```bat
set OPENAI_BASE_URL=http://127.0.0.1:8000/v1
set OPENAI_API_KEY=any
```
Use model `auto` (or `OpenCode`) — the answer always comes from the model selected in OpenCode.

Stop everything with **`stop.bat`**.

## 🧩 How it works

- OpenCode Desktop spawns an embedded server on a **random** `127.0.0.1` port with a **random per-launch password** (HTTP Basic, user `opencode`).
- `bridge.py` finds the port via the Windows TCP table and reads the password from the server process environment (read-only, same user).
- Chat requests go to `POST /session/:id/message` with the live model — the same call the app itself makes, so real answers come back, not silence.
- The dashboard (`index.html`) lets you pick the target session, force a Zen model, test prompts and watch health.

## 📁 Files

| File | What |
|---|---|
| `start.bat` / `stop.bat` | start in background + open site / stop |
| `bridge.py` | the bridge (stdlib only, no pip install) |
| `index.html` | web dashboard served by the bridge |

## ❓ FAQ

**Claude Code hangs on "thinking"?** The request runs in the selected desktop session — open it and watch. Empty answers are polled for ~2 min, then a clear 502 is returned (see `bridge.log`).

**Port/password changed?** The bridge re-discovers automatically on 401, plus a background health check.

**Duplicate messages?** Every attempt carries a stable `messageID`; retry fires only on fast network failures.

## ⭐ Like it?

Star the repo — it helps a lot. Issues and PRs welcome.

---

## Русский 🇷🇺

**Используй любую модель OpenCode Zen (Muse Spark, Big Pickle, Ling, MiMo, Nemotron…) из Claude Code или любого OpenAI-клиента — бесплатно, локально, без API-ключей.**

### Что это?

OpenCode Desktop поднимает скрытый локальный сервер со всеми чатами и моделями. **Мост** находит его сам и отдаёт наружу:

- ✅ **OpenAI-совместимый API** → `POST /v1/chat/completions` (стрим есть)
- ✅ **Anthropic-совместимый API** → `POST /v1/messages` (на нём говорит **Claude Code**)
- ✅ **Веб-панель** → выбор сессии, модели Zen, тест, живой статус

Отвечает **та модель, что выбрана в опенкоде** — переключил внизу (Build · …) или на сайте, следующий запрос уже с ней.

Без зависимостей. Одна папка. Только стандартная библиотека Python.

### Быстрый старт

1. OpenCode Desktop запущен (любой чат открыт).
2. Запусти **`start.bat`** — мост поднимется фоном, откроется панель `http://127.0.0.1:8000/`.
3. Для Claude Code — переменные выше (`ANTHROPIC_MODEL=OpenCode`) или `~/.claude/settings.json`.
4. Остановка — **`stop.bat`**.

Для OpenAI-клиентов: `OPENAI_BASE_URL=http://127.0.0.1:8000/v1`, `OPENAI_API_KEY=any`, модель `auto`.

### Как устроено

- Порт ищется в TCP-таблице (слушатель `OpenCode.exe`), пароль — в env серверного процесса (только чтение).
- Запросы идут в `POST /session/:id/message` с живой моделью — тот же вызов, что делает само приложение, поэтому возвращается именно ответ.
- Пустые ответы допрашиваются по `parentID`, быстрые обрывы ретраятся один раз, всё пишется в `bridge.log`.

### Файлы

`start.bat` / `stop.bat` — запуск/остановка; `bridge.py` — сам мост; `index.html` — панель.

### Частые вопросы

**Висит «размышление»?** Запрос выполняется в выбранной сессии — открой её и смотри. Пустые ответы мост допрашивает ~2 минуты, потом честная 502.

**Порт/пароль сменились?** Мост переоткрывает сам.

### ⭐ Нравится?

Поставь звезду — это реально помогает. Баги и PR — велком.

---

MIT — делай что хочешь, звезда приветствуется 🙂

<img width="509" height="266" alt="image" src="https://github.com/user-attachments/assets/994b9556-86e8-44d9-8a73-5a637845a39b" />
<img width="585" height="790" alt="image" src="https://github.com/user-attachments/assets/94109444-b4e5-40a2-a1a1-806699e7043a" />


