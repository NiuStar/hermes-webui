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
`profile + session_id + stream_id + channel` is claimed atomically in the
profile's WebUI state directory before any network send, so concurrent WebUI
processes, reconnects, and duplicate terminal callbacks cannot send the same
channel twice. Claim keys are hashed and stored in a profile-local transactional
SQLite database with mode `0600`.
Each claimed channel is attempted exactly once. A failure is logged only as a
channel name and exception type and does not fail the chat
turn. Claims are retained after a failed or interrupted delivery: this is an
intentional at-most-once boundary, preferring a missed notification over a
duplicate notification after an uncertain external send.
There is no WebUI-layer retry, including after a timeout or provider cooldown,
because the external service may have accepted the message before its response
was lost. Retrying would violate the at-most-once guarantee.

Delivery is successful only when Hermes Agent's official `send_message` result
contains `success=true`. Transport success alone is not considered delivery.
The sender runs in isolated Python mode with the validated Hermes Agent source
inserted first on `sys.path`; the WebUI working directory cannot shadow the
official `tools.send_message_tool` module.
