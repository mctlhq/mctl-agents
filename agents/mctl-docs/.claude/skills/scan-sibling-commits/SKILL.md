---
name: scan-sibling-commits
description: Использовать когда нужно собрать список user-visible изменений из соседних mctl-репо за указанный период.
---

# Scan sibling commits

Когда researcher mctl-docs ищет doc gaps, у тебя два режима — local-clone (предпочтительный, быстрый) и GitHub API fallback (для cluster runs, где склонирован только `mctl-gitops`).

## 1. Базовый путь и список репо
`BASE="${SIBLING_REPOS_PATH:-/Users/dmitriimashkov/PycharmProjects/mctlhq}"`. Список репо — в `CLAUDE.md`. Себя (mctl-docs) не сканируй.

## 2. За какой период
По умолчанию `--since="7 days ago"` (ISO8601 для API: вычисли как `now - 7d`). Если хочешь другой диапазон — учитывай дату последнего mentor-digest'а (можешь поискать `cd mctl-gitops && ls platform-gitops/agents-state/_mentor/digest/`, взять самый свежий).

## 3. Per-repo: выбери режим

Для каждого `<repo>` из списка:

### Mode A — local clone (если `$BASE/<repo>/.git` существует)
Проверь `Bash`: `test -d "$BASE/<repo>/.git" && echo present || echo missing`.

Если `present`:
```bash
# Перечисление user-visible коммитов
git -C "$BASE/<repo>" log --since="7 days ago" --pretty='%h|%ad|%s' --date=short --no-merges

# Если непонятно — получить touched files
git -C "$BASE/<repo>" show --stat <sha>

# Если всё ещё непонятно — посмотреть конкретный диф (короткий!)
git -C "$BASE/<repo>" show <sha> -- path/to/relevant.go | head -200
```

### Mode B — GitHub API fallback (если local clone отсутствует)
Если `missing` — НЕ записывай "no signal", а используй `WebFetch`:

URL: `https://api.github.com/repos/mctlhq/<repo>/commits?since=<ISO8601>&per_page=50`

Где `<ISO8601>` — например `2026-04-21T00:00:00Z` (для `--since="7 days ago"` от текущей даты).

`WebFetch` параметры:
- `url`: указанный выше
- `prompt`: что-то вроде:
  > Return a JSON-like list of objects, one per commit. For each commit extract:
  > - `sha` (the short 7-char hex from the `sha` field)
  > - `date` (the `commit.author.date` field, ISO8601 truncated to YYYY-MM-DD)
  > - `message` (the FIRST LINE of `commit.message` only — drop everything after the first `\n`)
  > - `url` (the `html_url` field)
  > Skip merge commits (commits with more than one parent in the `parents` array).
  > Output as plain text, one commit per line, format: `sha|date|message|url`.

WebFetch вернёт текст по этому формату — парси построчно.

**Headers**: WebFetch не позволяет задать произвольные header'ы напрямую. Если `prompt` не вытаскивает данные потому что ответ возвращается публичным API без auth и rate limit'ится — добавь в URL `?` query-string ничего не помогает; вместо этого используй `Bash curl` как fallback-of-fallback:
```bash
curl -sH "Authorization: Bearer $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github+json" \
     "https://api.github.com/repos/mctlhq/<repo>/commits?since=<ISO8601>&per_page=50" \
   | jq -r '.[] | select((.parents | length) <= 1) | "\(.sha[0:7])|\(.commit.author.date[0:10])|\(.commit.message | split("\n")[0])|\(.html_url)"'
```

Этот curl-вариант надёжнее — задаёт `Authorization`, поднимает rate-limit с 60/h до 5000/h и возвращает строки в нужном формате.

### Если нужны touched files / diff (Mode B)
- File list: `GET /repos/mctlhq/<repo>/commits/<sha>` (поле `files[].filename` + `additions`/`deletions`).
- Diff: `Accept: application/vnd.github.v3.diff` тот же endpoint вернёт raw diff. Усеки `head -200`.

## 4. Conventional commits фильтр
Оставлять только префиксы:
- `feat:` / `feat(scope):` — новая user-facing функциональность
- `fix:` / `fix(scope):` — исправление user-visible бага
- `docs:` / `docs(scope):` — но только если описывает новый concept (не typo fix)
- `BREAKING CHANGE:` упоминание — всегда брать (нужно migration note)

Отбрасывать: `chore:`, `refactor:`, `test:`, `ci:`, `style:`, `build:`, `perf:` (если perf не меняет user-observable behaviour).

## 5. Cross-reference с docs
Прочитай `context/docs-tree.md` — там snapshot структуры docs.mctl.ai с короткими описаниями. Для каждого user-visible коммита ответь "уже задокументировано / gap / stale" сравнив с этим деревом.

## 6. mctl MCP проверка (опционально)
Если `mcp__mctl__*` доступен — проверь текущую prod-версию каждого репо. Если коммит выше prod-версии — пометь "in-flight" (документировать рано, может откатиться).

## Rate limits
- Authenticated GitHub API (с `GITHUB_TOKEN`): **5000 req/h**.
- 7 sibling-репо × 1 list-call/run = 7/h baseline. Даже с дополнительными touched-files вызовами на каждый интересный commit — порядок 50–100 req/run. Очень комфортно.
- Без токена (`GITHUB_TOKEN` пустой): 60 req/h public — почти наверняка упрёшься. В этом случае пометь "no signal: <repo> rate-limited" в inbox и пропусти.

## Не делай
- Не клонируй ничего. В local mode — если репо нет, идём в Mode B. В Mode B — если `$GITHUB_TOKEN` пустой и публичный API rate-limit'ится, пиши "no signal" и пропускай.
- Не интерпретируй приоритет — это работа analyst.
- Не читай больше 200 строк дифа за раз — для понимания смысла обычно хватает stat + commit message + первого hunk'а.
