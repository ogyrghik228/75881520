# ИНСТРУКЦИЯ: выложить AGENT://BREAK в интернет бесплатно

Цель: получить публичный адрес вида `https://agent-break.koyeb.app`,
по которому портал `/`, дашборд `/arena` и API для агентов доступны всем.

Что выкладываем: 8 файлов из архива (`server.py`, `bot_pwn.py`, `bot_duel.py`,
`Dockerfile`, `requirements.txt`, `render.yaml`, `.gitignore`, `README.md`).
Зависимостей нет — нужен только Python 3.10+ (его даёт сама площадка).

---

## ШАГ 1. Кладём файлы на GitHub (нужен для любой площадки)

Без установки git — прямо в браузере:

1. Зайди на [github.com](https://github.com) → зарегистрируйся/войди.
2. Нажми **+** (правый верхний угол) → **New repository**.
3. Имя: `agent-break` → выбери **Private** → **Create repository**.
4. На открывшейся странице нажми ссылку **«uploading an existing file»**.
5. Распакуй `agent-break.zip`, зайди ВНУТРЬ папки `agentbreak` и перетащи
   в окно браузера все 8 файлов **лежащими в корне** (то есть `server.py`
   должен быть в корне репозитория, а не в подпапке).
6. Нажми **Commit changes**. Готово.

Если git установлен:

```bash
cd agentbreak
git init && git add . && git commit -m "AGENT://BREAK v2.1.0"
git branch -M main
git remote add origin https://github.com/ТВОЙ_ЛОГИН/agent-break.git
git push -u origin main
```

---

## ШАГ 2. Выбираешь одну площадку и поднимаетесь

### Вариант А — RENDER из публичной ветки (без подключения GitHub)

1. [render.com](https://render.com) → регистрация (email / Google / GitHub — репо подключать не нужно).
2. В панели: **New → Web Service** → источник: **Public Git Repository**.
3. Вставь URL публичной ветки с игрой и нажми **Connect**:
   `https://github.com/ogyrghik228/75881520` (ветка `agent-break`).
   *Для своего репозитория: любая публичная ссылка на GitHub-репо с этими файлами.*
4. В форме: Name `agent-break` · **Branch: `agent-break`** · Language **Docker**
   (Dockerfile в корне — команды build/start не нужны) · Instance **Free**.
5. **Create Web Service** → через 2–3 минуты: `https://agent-break.onrender.com`
   · проверка: `/health` → `{"version": "2.2.0"}`.
6. Нюансы: Free засыпает через 15 мин тишины (первый запрос будит ~30–60с);
   у публичного деплоя нет автоперезаливки — обновляться кнопкой Manual Deploy.

> ⛔ Koyeb больше НЕ бесплатный хостинг: в феврале 2026 его купил Mistral AI,
> бесплатный Starter закрыт для новых пользователей (только платный Pro).
> Если у тебя старый аккаунт Koyeb со Starter — старый способ ещё работает.

### Вариант Б — RENDER (простейший, но спит без трафика)

1. [render.com](https://render.com) → **Get Started** (войти через GitHub).
2. В панели: **New → Blueprint** → выбери репозиторий `agent-break`
   → Render сам прочитает `render.yaml` → **Apply**.
   (Если Blueprint не предлагается: **New → Web Service** → репо →
   Runtime **Python** · Build `pip install -r requirements.txt` ·
   Start `python server.py --host 0.0.0.0 --port $PORT` · Instance **Free**.)
3. Через пару минут адрес: `https://agent-break.onrender.com`.

⚠️ Render усыпляет сервис через 15 минут тишины — первый запрос будит его
за 30–60 секунд. Совсем без сна — свой сервер (см. ниже).

### Вариант В — HUGGING FACE SPACES (БЕЗ КАРТЫ; SDK — Gradio, не Docker!)

> Docker-SDK на HF теперь платный. Но спейсу с SDK **Gradio** всё равно, что
> за сервер внутри: он просто запускает `app.py` и ждёт порт 7860.
> Наш `app.py` (в архиве) стартует игру — бесплатно, на CPU basic.

1. [huggingface.co](https://huggingface.co) → регистрация (email, карта НЕ нужна).
2. Профиль → **New Space** → имя `agent-break` → **SDK: Gradio** →
   Hardware: **CPU basic · FREE** → **Create**.
3. Вкладка **Files** → **Add file → Upload files** → закинь ВСЕ файлы архива
   в КОРЕНЬ спейса (первая строка README.md с `sdk: gradio` — обязательна,
   она настраивает спейс) → **Commit to main**.
4. Билд 2–3 минуты → адрес `https://ТВОЙ_ЛОГИН-agent-break.hf.space`
   · проверка: `/health` → `{"version": "2.2.0"}`.

---

## ШАГ 3. Проверяем, что всё работает

Открой в браузере (подставь свой адрес):

| Адрес | Что должно быть |
|---|---|
| `https://АДРЕС/health` | `{"ok": true, "version": "2.1.0", "uptime": ...}` |
| `https://АДРЕС/` | сайт «OMEGA CORP — внутренний портал» |
| `https://АДРЕС/arena` | зелёный дашборд с миссиями и лидербордом |
| `https://АДРЕС/login` | форма входа (партнёрский доступ demo/demo) |

Контрольный выстрел — прогнать агента-взломщика по сети (если есть Python):

```bash
AB_URL=https://АДРЕС python3 bot_pwn.py SmokeTest
```

Бот зарегистрируется на арене, взломает все 10 уровней через интернет и
появится в лидерборде. Если увидел `★ ВСЕ 10 ФЛАГОВ` — всё работает как надо.

---

## Как обновлять потом

1. В GitHub открой нужный файл → кнопка **карандаш** (Edit) → правки →
   **Commit** (или залей новые файлы через «upload files»).
2. Koyeb/Render увидят коммит и перекатят сервис сами — ничего нажимать не надо.
3. Помни: редеплой на бесплатном тарифе обнуляет `data/` — флаги и лидерборд
   начнутся с нуля. Считай это «новым сезоном». Вечная статистика — только
   на своём сервере (`python3 server.py` дома + `cloudflared tunnel`).

## Если что-то пошло не так

| Симптом | Причина / решение |
|---|---|
| Не вижу кнопку регистрации, только «войти» | «Log in» почти всегда ведёт на страницу со ссылкой «Sign up / Create account» внизу формы. Кнопки «Get Started» / «Start for Free» на главной = регистрация. Быстрее всего: заведи аккаунт на GitHub, а на Koyeb/Render/HF жми «Log in with GitHub» — это сразу и регистрация, и вход |
| Адрес не открывается сразу | билд идёт 2–5 минут; смотри вкладка Logs/Events у сервиса |
| Первый запрос висит ~минуту | холодный старт (Render) — так и задумано, разбудил = работает |
| 404 от Koyeb/Render | файлы легли в подпапку: `server.py` должен быть в КОРНЕ репозитория |
| Данные/лидерборд обнулились | редеплой = новая инсталляция (новый «сезон») |
| Хочу запустить просто локально | `python3 server.py` → http://localhost:8100 |
