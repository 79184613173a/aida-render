# AiДа Render

HTTP-сервис рендера коротких роликов (Reels/Shorts 9:16) для агента-продюсера AiДа.
n8n отдаёт таймлайн JSON, сервис собирает mp4 через ffmpeg и стучится на `callback_url`.

Умеет:
- сцены из видео и картинок: обрезка под 9:16 (`cover`) или вписывание на размытый фон (`blur`), Ken Burns для картинок;
- переходы между сценами (`fade`, `fadeblack`, `slideleft`, `slideup`, `wipeleft`, `circleopen`, `zoomin`, `smoothleft` — любые из ffmpeg xfade);
- субтитры с подсветкой слова под голос (если переданы тайминги слов);
- моушн-плашки: `title`, `plate`, `stat` (цифра считается от нуля), `list` (пункты по очереди), `cta` (пульсирующая кнопка);
- PNG-оверлеи (логотип, стикеры, графики);
- звук: голос, музыка с автоприглушением под голос, SFX по таймкодам, родной звук клипов; громкость выравнивается до −14 LUFS.

Время начала сцены = сумма длительностей предыдущих: переход «съедает» хвост уходящей сцены,
поэтому тайминги субтитров и плашек считаются от простой суммы `duration`.

## API

Все запросы, кроме `/health` и `/files/*`, требуют заголовок `X-Api-Key: <RENDER_API_KEY>`.

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | версия ffmpeg, длина очереди |
| POST | `/render` | ставит рендер в очередь → `{job_id, status_url, total_duration}` |
| GET | `/jobs/{id}` | статус: `queued` / `rendering` / `done` / `error`, `url`, `thumb_url`, `duration`, `scene_starts` |
| GET | `/files/{id}.mp4` | готовый ролик (и `{id}.jpg` — обложка) |
| POST | `/scenes` | `{url, threshold?, min_len?, margin?}` → `cuts` (секунды склеек) и `windows` (чистые окна `a`–`b` внутри одного плана) |
| POST | `/audio/concat` | `{urls[], gap?, lufs?}` → склеенный mp3 (loudnorm на каждый блок, паузы `gap`), `url`, `duration`, `parts[{n,start,end,dur}]` |
| POST | `/probe` | `{url}` → длительность и параметры файла (например, сколько длится озвучка) |

Когда рендер готов (или упал), сервис шлёт на `callback_url` POST с тем же JSON, что отдаёт `/jobs/{id}`,
включая `meta` из запроса (кладите туда `project_id`, `chat_id`).

Пример тела `/render` — `examples/render.json`. Полная схема — `app/models.py`.

## Деплой в Coolify

1. **+ New → Application → Private Repository (GitHub App)** → репо `aida-render`, ветка `main`.
2. Build Pack: **Dockerfile**. Port: **8000**.
3. Domain: `https://render.aidamedia.ru` (A-запись `render` → IP сервера).
4. **Storages → + Add → Volume**: mount path `/data`.
5. **Environment Variables**:
   - `RENDER_API_KEY` — длинный случайный ключ (тот же — в n8n-креде Header Auth `X-Api-Key`);
   - `BASE_URL` — `https://render.aidamedia.ru`;
   - `WORKERS` — `1` (сколько роликов рендерить одновременно; 1 на каждые 2–4 ядра);
   - `FILE_TTL_HOURS` — `72` (через сколько часов чистить готовые файлы).
6. Deploy → открыть `https://render.aidamedia.ru/health` — должна быть версия ffmpeg.

## Локальный запуск

```bash
docker build -t aida-render .
docker run -p 8000:8000 -e RENDER_API_KEY=test -e BASE_URL=http://localhost:8000 -v $PWD/data:/data aida-render
```

Шрифты: Montserrat (SIL Open Font License), лежат в `fonts/`. Свой шрифт бренда — положить `.ttf` в `fonts/`
и указать имя семейства в `style.font`.
