---
name: track-dependencies
description: Использовать когда нужно проверить обновления npm/Python зависимостей mctl-portal.
---

# Track dependencies

Когда нужно посмотреть, что вышло нового у зависимостей сервиса:

1. Список ключевых зависимостей лежит в `context/architecture.md` (раздел "Dependencies").
2. Для каждой зависимости проверить `https://github.com/<owner>/<repo>/releases`
   через WebFetch.
3. Сравнить с текущей версией из `context/current-version.md`.
4. Релевантно только то, что вышло после нашей текущей версии **и** содержит
   security fix, performance улучшение или breaking change.

Не дублируй то, что уже зафиксировано в `context/decisions/` как сознательно отложенное.
