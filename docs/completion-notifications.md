# Completion Notifications

Hermes WebUI can notify after a browser-originated assistant response reaches the
server's successful completion point and is persisted. Browser disconnects,
user cancellation, provider errors, and partial streams do not trigger a
completion notification.

## Settings

The Settings panel contains a global, default-off `Notify when a response is
complete` option. Channels are selected independently:

- Browser: uses the existing browser notification permission and background-tab
  behavior.
- Weixin: sends through the Hermes Weixin iLink adapter to its home channel.
- WeCom: sends through the Hermes WeCom adapter to its home channel.
- Feishu: sends through the Hermes Feishu adapter to its home channel.

The WebUI never accepts or returns platform credentials or target IDs in browser
requests. It reuses the platform credentials and home channel configured in the
active Hermes home. The settings API exposes only boolean `configured` status.

The notification body contains only a completion label and an instruction to
return to WebUI. Session titles, assistant output, full transcripts, credentials,
access tokens, and platform target IDs are not sent.

## Delivery Contract

The server dispatches after the session save and completion journal event. Each
`session_id + stream_id + channel` is recorded in the WebUI state directory so
reconnects and duplicate terminal callbacks do not send the same channel twice.
Failed platform delivery is retried three times with short backoff; failure is
logged only as a channel name and exception type and does not fail the chat
turn. A later duplicate completion can retry a channel that never succeeded.
When Hermes reports a provider cooldown, retries honor that delay (capped at 60
seconds) instead of immediately adding more requests.

Delivery is successful only when Hermes Agent's official `send_message` result
contains `success=true`. Transport success alone is not considered delivery.
