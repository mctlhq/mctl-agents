# Parity byte differences (informational)

Raw compact output of each subject for the T11 sequence. Semantic equality is
what T11 enforces; this file records how the bytes differ.

```diff
--- /home/runner/work/_temp/parity/parity-rust.raw	2026-10-23 21:43:50.059791106 +0000
+++ /home/runner/work/_temp/parity/parity-python.raw	2026-10-23 21:43:50.376796720 +0000
@@ -1,7 +1,7 @@
-{"request":"initialize","result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"mctl-runtime","version":"0.0.0"}}}
-{"request":"tools/list","result":{"tools":[{"name":"mctl_whoami","description":"Who am I","inputSchema":{"type":"object","properties":{"verbose":{"type":"boolean","default":false},"limit":{"type":"integer","maximum":1.50}},"required":[],"additionalProperties":false}},{"name":"deny_tool","description":"Always denied","inputSchema":{"required":["target"],"type":"object","properties":{"target":{"type":"string"},"dry_run":{"default":true,"type":"boolean"}}}},{"name":"approval_tool","description":"Needs approval","inputSchema":{"type":"object","properties":{"z":{"type":"array","items":{"type":"number","minimum":0.10}},"a":{"type":"string"}},"required":["a"]}},{"name":"slow_tool","description":"Slow","inputSchema":{"type":"object","properties":{},"additionalProperties":true}}]}}
+{"request":"initialize","result":{"protocolVersion":"2025-06-18","capabilities":{"experimental":{},"tools":{"listChanged":false}},"serverInfo":{"name":"mctl-runtime-baseline","version":"1.29.0"}}}
+{"request":"tools/list","result":{"tools":[{"name":"mctl_whoami","description":"Who am I","inputSchema":{"type":"object","properties":{"verbose":{"type":"boolean","default":false},"limit":{"type":"integer","maximum":1.5}},"required":[],"additionalProperties":false}},{"name":"deny_tool","description":"Always denied","inputSchema":{"required":["target"],"type":"object","properties":{"target":{"type":"string"},"dry_run":{"default":true,"type":"boolean"}}}},{"name":"approval_tool","description":"Needs approval","inputSchema":{"type":"object","properties":{"z":{"type":"array","items":{"type":"number","minimum":0.1}},"a":{"type":"string"}},"required":["a"]}},{"name":"slow_tool","description":"Slow","inputSchema":{"type":"object","properties":{},"additionalProperties":true}}]}}
 {"request":"tools/call mctl_whoami","result":{"content":[{"type":"text","text":"user: tester"}],"isError":false}}
 {"request":"tools/call deny_tool","result":{"content":[{"type":"text","text":"DENY: policy forbids deny_tool"}],"isError":true}}
 {"request":"tools/call approval_tool","result":{"content":[{"type":"text","text":"REQUIRE_APPROVAL: approval_id=ap-1"}],"isError":true}}
-{"request":"tools/call secret_tool","result":{"content":[{"type":"text","text":"not-eligible: secret_tool is not allowlisted"}],"isError":true,"structuredContent":{"reason_code":"not-eligible"}}}
-{"request":"tools/call no_such_tool","result":{"content":[{"type":"text","text":"not-eligible: no_such_tool is not allowlisted"}],"isError":true,"structuredContent":{"reason_code":"not-eligible"}}}
+{"request":"tools/call secret_tool","result":{"content":[{"type":"text","text":"not-eligible: secret_tool is not allowlisted"}],"structuredContent":{"reason_code":"not-eligible"},"isError":true}}
+{"request":"tools/call no_such_tool","result":{"content":[{"type":"text","text":"not-eligible: no_such_tool is not allowlisted"}],"structuredContent":{"reason_code":"not-eligible"},"isError":true}}
```
