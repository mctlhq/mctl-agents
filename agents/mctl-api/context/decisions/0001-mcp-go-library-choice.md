# 0001. MCP сервер на mark3labs/mcp-go вместо custom реализации

**Status:** accepted
**Date:** 2026-02-15

## Context
Нужен MCP-сервер для интеграции с Claude.ai (connector directory) и локальными Claude Code сессиями. Streamable HTTP transport с OAuth 2.0 PKCE. Нужны 24 тулзы со схемами и валидацией. Custom implementation = много низкоуровневой работы вокруг JSON-RPC 2.0, server-sent events, batch requests.

## Decision
Используем **github.com/mark3labs/mcp-go** (v0.31.0). Активно поддерживается, поддерживает Streamable HTTP transport, schema generation, MCP spec версии 2025-06-18.

## Consequences
- **+** Готовый transport layer (HTTP POST/GET, SSE), schema validation
- **+** Совместимость с Claude.ai connector
- **+** Авто-генерация tool-listings
- **−** Зависимость от внешнего мейнтейнера (alternatives: anthropics/sdk-go, custom)
- **−** Каждый bump требует ре-валидации всех 24 тулзов через MCP Inspector

## Что НЕ предлагать (для analyst/researcher)
- Замену на custom JSON-RPC implementation — потеря compat с Claude.ai connector
- Перехода на gRPC — Claude.ai не поддерживает
- Понижение версии mark3labs/mcp-go ради simplicity — теряем спецификацию 2025-06-18
