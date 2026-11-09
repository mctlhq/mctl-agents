# DevLoop decide notification

mctlhq/mctl-api#964. When a canonical DevLoop parks on a pending `aar_`, the
worker sends the operator one Telegram message. Its **Decide** button opens:

    https://api.mctl.ai/devloop/decide/<workflow_id>?grant=<aar_>&cd=<context digest>&xd=<content digest>

The decision is made on that page, under a fresh ZITADEL sign-in of a human
admin (mctl-api#976). The bot holds no mctl credential and decides nothing.
The page refuses a link whose grant was rewritten or decided, so an old
message is harmless and is never edited.

## When it sends

- Once per pending grant per run (`DECIDE_NOTIFY_PATCH`, `decide-notify`).
  A superseded proposal gets a new grant and a new message.
- Not for a grant found already approved.
- At least once, not exactly once: a worker crash between the send and its
  recording can repeat it.
- A grant whose park already ran before the worker carried the patch is not
  notified. A later park of the same loop (a re-ask after a transition or a
  supersede) is: `workflow.patched()` records the marker the first time the
  call site runs outside replay.

## Configuration

| Name | Kind | Meaning |
|---|---|---|
| `DEVLOOP_NOTIFY_TELEGRAM_BOT_TOKEN` | credential (`_FILE`) | Bot token used for `sendMessage` only |
| `DEVLOOP_NOTIFY_TELEGRAM_CHAT_ID` | env | Numeric chat id of the operator |
| `DEVLOOP_DECIDE_BASE_URL` | env, optional | Plain https origin of mctl-api, default `https://api.mctl.ai` |

If the token or chat id is missing, the activity reports `disabled`. If a value
does not match its pattern, it reports `refused` and names that field. In both
cases nothing is sent, and the loop is unaffected. A Telegram failure is retried
up to three times, then logged as `DECIDE_NOTIFY ... failed`. It is a missed
message, never a decision. In that case, approve as before.

The message carries the target, the issue link, the workflow id and the grant
id. All of these are already on the issue. It never carries proposal text.
