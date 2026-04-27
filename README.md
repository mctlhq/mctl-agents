# mctl-agents

Multi-agent система для платформы mctl. Каждый сервис имеет своего агента-владельца, который:
- читает источники (changelog'и, GitHub releases, CVE, метрики из mctl MCP)
- складывает находки в `inbox/`
- оформляет топ-предложения в `proposals/<slug>/{requirements,design,tasks}.md`

Mentor-агент агрегирует proposals и выдаёт еженедельный дайджест.

## Структура

```
agents/
├── mctl-web/                  # один агент = один сервис
│   ├── CLAUDE.md              # роль и границы агента
│   ├── .claude/
│   │   ├── skills/            # переиспользуемые навыки
│   │   └── agents/            # sub-agents (researcher, analyst, spec-writer)
│   ├── context/               # архитектура, ADR, текущая версия
│   ├── inbox/                 # сырые находки researcher'а
│   └── proposals/             # оформленные spec-driven предложения
├── _mentor/                   # ментор платформы
│   ├── CLAUDE.md
│   └── digest/                # еженедельные дайджесты
config/
└── settings.py                # SERVICES, mctl MCP URL
orchestrator/
├── auth.py                    # OAuth ИЛИ API-ключ
├── run_service_agent.py       # запуск агента сервиса
├── run_mentor.py              # запуск ментора
└── run_all.py                 # параллельный прогон всех + ментор
```

## Auth: два режима

Код одинаково работает и с OAuth-токеном (твоя Claude Pro/Max подписка),
и с API-ключом (Console-биллинг). Выбор автоматический:

- если задан `CLAUDE_CODE_OAUTH_TOKEN` — используется он (личное использование, прототип)
- иначе используется `ANTHROPIC_API_KEY` (production)

Получить OAuth-токен:
```bash
npm install -g @anthropic-ai/claude-code
claude setup-token   # откроет браузер, выдаст sk-ant-oat01-...
```

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env: положи либо CLAUDE_CODE_OAUTH_TOKEN, либо ANTHROPIC_API_KEY,
# плюс MCTL_TOKEN для доступа к https://api.mctl.ai/mcp

# один агент
python -m orchestrator.run_service_agent mctl-web

# ментор
python -m orchestrator.run_mentor

# всё целиком
python -m orchestrator.run_all
```
