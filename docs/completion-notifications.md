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

The notification body contains a bounded, single-line session title and a bounded
preview of the settled final assistant output so the recipient can identify the
completed work without opening WebUI. Both fields are force-redacted before
sending, control characters are folded, and the complete message is capped at
500 characters. Session IDs, full transcripts, credentials, access tokens, and
platform target IDs are not sent.

Feishu completion notices use a native Feishu card with a quiet blue header,
bounded redacted conclusion, and a compact snapshot of running subagents and
background processes. Only an explicit Feishu `230099` (card-content creation
failure) falls back to a compact text notice; permissions, rate limits, a
timeout or any other uncertain outcome never trigger a second send. Other
channels keep their existing text format. The card conclusion preserves bounded,
redacted `**bold**` spans while encoding other Markdown syntax (including links
and @ mentions) as literals.
The compact text fallback uses Feishu's supported `<b>` style tags for those
spans; Feishu plain-text messages do not render `**bold**` Markdown. Neither
path conveys the full final response.
The card requests Feishu's documented `large` Markdown text size (16px on
desktop, versus the 14px default). Actual client rendering still requires
visual acceptance; the field does not change the text fallback format.

If an operator sets
`HERMES_WEBUI_PUBLIC_URL` to the actual WebUI origin (HTTPS, or HTTP on a private
address), the card adds an **Open session** link. Without that explicit origin,
the link is omitted; it is never inferred from request headers. The configured
origin must be reachable from the recipient's device; invalid origins omit
the button without suppressing the notification. The production compose file
can be prepared with the origin while the running container remains unchanged
until the operator explicitly recreates it.

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

Text fallback is successful only when Hermes Agent's official `send_message`
result contains `success=true`; card delivery additionally requires a Feishu
message ID. Transport success alone is not considered delivery. An uncertain
card subprocess result records only a bounded failure category or exit code,
not the SDK exception text or stderr (which may contain credentials). Such a
result is never silently retried as text.
The sender runs in isolated Python mode with the validated Hermes Agent source
inserted first on `sys.path`; the WebUI working directory cannot shadow the
official `tools.send_message_tool` module.
