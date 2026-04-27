# 0001. Three-tier skill system (builtin / YAML / remote)

**Status:** accepted
**Date:** 2026-02-20

## Context
Self-healing нужен для разных типов алёртов: одни простые и universal (OOMKill → bump memory), другие специфичные для конкретного сервиса/команды (recover redis from corrupted RDB), третьи требуют внешнего знания (ML-based root cause analysis). Single hardcoded set Go skills плохо масштабируется.

## Decision
**Three-tier skill registry:**
1. **Builtin Go skills** — компилируются в бинарь, 9 шт universal patterns. Высокая стабильность, обновление через release.
2. **YAML skills** — определяются в `skills/custom/`, hot-reload без restart'а. Любая команда может добавить regex pattern + remediation template.
3. **Remote skills** — регистрируются через `POST /api/v1/skills/register`, делегируют диагноз внешнему HTTP сервису. Для сложных AI/ML или vendor-specific cases.

Skill matching ranked by confidence; circuit breaker auto-disables failing skills.

## Consequences
- **+** Builtin = высокий бар качества (review + tests + Go strict typing)
- **+** YAML = быстрая итерация для команд (PR в gitops, не в mctl-agent)
- **+** Remote = расширяемость без модификации mctl-agent
- **−** Три entry-point'а — увеличивает площадь "что может пойти не так"
- **−** Circuit breaker может скрыть реальную проблему — нужна alert на disabled skills

## Что НЕ предлагать
- Слияние всех skills в Go (потеря YAML hot-reload)
- Удаление remote tier — он будущий (mctl-agents может стать remote skill source!)
- Переключение на JS/Python plugin engine — теряем Go performance + type safety
