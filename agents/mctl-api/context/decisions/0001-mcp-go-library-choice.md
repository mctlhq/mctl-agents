# 0001. MCP server on mark3labs/mcp-go instead of a custom implementation

**Status:** accepted
**Date:** 2026-02-15

## Context
We need an MCP server for integration with Claude.ai (connector directory) and local Claude Code sessions. Streamable HTTP transport with OAuth 2.0 PKCE. We need 24 tools with schemas and validation. A custom implementation = a lot of low-level work around JSON-RPC 2.0, server-sent events, batch requests.

## Decision
We use **github.com/mark3labs/mcp-go** (v0.31.0). Actively maintained, supports Streamable HTTP transport, schema generation, MCP spec version 2025-06-18.

## Consequences
- **+** Ready transport layer (HTTP POST/GET, SSE), schema validation
- **+** Compatibility with the Claude.ai connector
- **+** Auto-generation of tool listings
- **−** Dependency on an external maintainer (alternatives: anthropics/sdk-go, custom)
- **−** Each bump requires re-validating all 24 tools through the MCP Inspector

## What NOT to propose (for analyst/researcher)
- Replacement with a custom JSON-RPC implementation — loss of compat with the Claude.ai connector
- Move to gRPC — Claude.ai does not support it
- Downgrading mark3labs/mcp-go for simplicity — we lose the 2025-06-18 spec
