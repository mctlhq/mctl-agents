---
name: scan-sibling-commits
description: Использовать когда нужно собрать список user-visible изменений из соседних mctl-репо за указанный период.
---

# Scan sibling commits

Когда researcher mctl-docs ищет doc gaps:

1. **Базовый путь.** `BASE="${SIBLING_REPOS_PATH:-/Users/dmitriimashkov/PycharmProjects/mctlhq}"`. Список репо — в `CLAUDE.md`. Себя (mctl-docs) не сканируй.

2. **За какой период.** По умолчанию `--since="7 days ago"`. Если хочешь другой диапазон — учитывай дату последнего mentor-digest'а (можешь поискать `cd mctl-gitops && ls platform-gitops/agents-state/_mentor/digest/`, взять самый свежий).

3. **Команды:**
```bash
# Перечисление user-visible коммитов
git -C "$BASE/<repo>" log --since="7 days ago" --pretty='%h|%ad|%s' --date=short --no-merges

# Если непонятно — получить touched files
git -C "$BASE/<repo>" show --stat <sha>

# Если всё ещё непонятно — посмотреть конкретный диф (короткий!)
git -C "$BASE/<repo>" show <sha> -- path/to/relevant.go | head -200
```

4. **Conventional commits фильтр.** Оставлять только префиксы:
- `feat:` / `feat(scope):` — новая user-facing функциональность
- `fix:` / `fix(scope):` — исправление user-visible бага
- `docs:` / `docs(scope):` — но только если описывает новый concept (не typo fix)
- `BREAKING CHANGE:` упоминание — всегда брать (нужно migration note)

Отбрасывать: `chore:`, `refactor:`, `test:`, `ci:`, `style:`, `build:`, `perf:` (если perf не меняет user-observable behaviour).

5. **Cross-reference с docs.** Прочитай `context/docs-tree.md` — там snapshot структуры docs.mctl.ai с короткими описаниями. Для каждого user-visible коммита ответь "уже задокументировано / gap / stale" сравнив с этим деревом.

6. **mctl MCP проверка (опционально).** Если `mcp__mctl__*` доступен — проверь текущую prod-версию каждого репо. Если коммит выше prod-версии — пометь "in-flight" (документировать рано, может откатиться).

## Не делай
- Не клонируй ничего. Если репо нет по пути — это инфра-проблема, пиши в inbox и пропускай.
- Не интерпретируй приоритет — это работа analyst.
- Не читай больше 200 строк дифа за раз — для понимания смысла обычно хватает stat + commit message + первого hunk'а.
