"""Reading the user's email directly from their mail provider (IMAP).

This needs no browser, so it works in the background. Access is read-only:
the mailbox is opened in read-only mode and messages are fetched without
being marked as read. The password is an "app password" created by the user
for Jarvis alone, kept in the private secrets file.

Email is outside content like any web page: senders can write anything, so
what is read here is never treated as instructions.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.message
import email.utils
import imaplib
import re

from pydantic import Field

from jarvis.config import get_secret
from jarvis.safety import Assessment
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from jarvis.tools.web import html_to_text

SECRET = "JARVIS_EMAIL_PASSWORD"  # noqa: S105 - the name of a stored secret, not a secret
_NOT_SET_UP = "Email is not connected yet. The user can connect it in Settings > Email (it takes an 'app password' from their mail provider)."


def default_host(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower()
    return {
        "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "yahoo.com": "imap.mail.yahoo.com",
        "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "aol.com": "imap.aol.com",
        "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com", "live.com": "outlook.office365.com",
    }.get(domain, f"imap.{domain}" if domain else "")


def _connect(ctx_or_core) -> imaplib.IMAP4_SSL:
    cfg = ctx_or_core.config.email
    paths = ctx_or_core.services.paths if hasattr(ctx_or_core, "services") else ctx_or_core.paths
    password = get_secret(paths, SECRET)
    if not cfg.address or not password:
        raise ToolError(_NOT_SET_UP)
    host = cfg.imap_host or default_host(cfg.address)
    try:
        box = imaplib.IMAP4_SSL(host, cfg.imap_port, timeout=25)
        box.login(cfg.address, password)
        box.select("INBOX", readonly=True)
    except imaplib.IMAP4.error as exc:
        raise ToolError(
            f"The mail server refused the sign-in ({_text(exc)[:160]}). It needs an app password, not the normal account "
            "password, and IMAP access must be allowed. The user can fix this in Settings > Email."
        ) from exc
    except OSError as exc:
        raise ToolError(f"Could not reach the mail server {host}: {exc}") from exc
    return box


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(raw):
        parts.append(chunk.decode(charset or "utf-8", "replace") if isinstance(chunk, bytes) else chunk)
    return " ".join("".join(parts).split())


def _body(message: email.message.Message) -> str:
    plain, rich = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not rich:
            rich = html_to_text(text)[1]
    return re.sub(r"\n{3,}", "\n\n", (plain or rich).strip())


def _list(ctx: ToolContext, unread_only: bool, important_only: bool, search: str, limit: int) -> dict:
    box = _connect(ctx)
    try:
        gmail = "gmail" in (ctx.config.email.imap_host or default_host(ctx.config.email.address))
        if important_only and gmail:
            criteria = ["X-GM-RAW", '"is:important' + (" is:unread" if unread_only else "") + '"']
        else:
            criteria = ["UNSEEN" if unread_only else "ALL"]
        if search:
            clean = re.sub(r'[^\x20-\x7e]|["\\]', " ", search).strip()
            criteria += ["TEXT", f'"{clean}"']
        status, data = box.uid("search", None, *criteria)
        if status != "OK":
            raise ToolError("The mail server could not run that search.")
        uids = data[0].split()
        chosen = uids[-limit:][::-1]                       # newest first
        messages = []
        if chosen:
            status, rows = box.uid("fetch", b",".join(chosen), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            for row in rows or []:
                if not isinstance(row, tuple):
                    continue
                meta = _text(row[0])
                uid = re.search(r"UID (\d+)", meta)
                head = email.message_from_bytes(row[1])
                messages.append({
                    "id": uid.group(1) if uid else "", "from": _header(head.get("From")), "subject": _header(head.get("Subject")) or "(no subject)",
                    "date": _header(head.get("Date")), "unread": "\\Seen" not in meta,
                })
            messages.sort(key=lambda m: int(m["id"] or 0), reverse=True)
        return {"mailbox": ctx.config.email.address, "matching": len(uids), "shown": len(messages), "messages": messages}
    finally:
        _close(box)


def _read(ctx: ToolContext, message_id: str, max_chars: int) -> str:
    box = _connect(ctx)
    try:
        status, rows = box.uid("fetch", message_id.encode(), "(BODY.PEEK[])")
        raw = next((row[1] for row in rows or [] if isinstance(row, tuple)), None)
        if status != "OK" or raw is None:
            raise ToolError(f"There is no message with id {message_id}. List the messages again to get current ids.")
        message = email.message_from_bytes(raw)
        body = _body(message)
        attachments = [_header(p.get_filename()) for p in message.walk() if p.get_filename()]
        text = f"From: {_header(message.get('From'))}\nTo: {_header(message.get('To'))}\nDate: {_header(message.get('Date'))}\nSubject: {_header(message.get('Subject'))}\n"
        if attachments:
            text += f"Attachments (not opened): {', '.join(attachments)}\n"
        text += "\n" + body[:max_chars] + ("\n[message truncated]" if len(body) > max_chars else "")
        return text
    finally:
        _close(box)


def _close(box: imaplib.IMAP4_SSL) -> None:
    try:
        box.logout()
    except Exception:  # noqa: BLE001, S110 - the connection is being dropped anyway
        pass


class CheckEmailInput(ToolInput):
    unread_only: bool = True
    important_only: bool = Field(default=False, description="Only messages the mail provider itself marks as important (Gmail).")
    search: str = Field(default="", description="Optional words to look for in the messages.")
    limit: int = Field(default=15, ge=1, le=50)


def _assess_check(a: CheckEmailInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Check the inbox", reads_private=True, ingests_untrusted=True)


async def _run_check(a: CheckEmailInput, ctx: ToolContext) -> ToolResult:
    data = await asyncio.to_thread(_list, ctx, a.unread_only, a.important_only, a.search, a.limit)
    return ToolResult(data, f"{data['matching']} message(s) found", untrusted_source="email inbox")


class ReadEmailInput(ToolInput):
    message_id: str = Field(pattern=r"^\d+$", description="The id of a message from check_email.")
    max_chars: int = Field(default=6000, ge=500, le=30_000)


def _assess_read(a: ReadEmailInput, ctx: ToolContext) -> Assessment:
    return Assessment(title=f"Read email {a.message_id}", reads_private=True, ingests_untrusted=True)


async def _run_read(a: ReadEmailInput, ctx: ToolContext) -> ToolResult:
    text = await asyncio.to_thread(_read, ctx, a.message_id, a.max_chars)
    return ToolResult(text, "read one email", untrusted_source="an email message")


EMAIL_TOOLS: list[Tool] = [
    Tool(
        "check_email",
        "List messages in the user's inbox (sender, subject, date) without opening or changing them. Use this for any "
        "question about their email; judge importance from senders and subjects, and read a message with read_email "
        "when its content matters. Do not use the browser or screenshots for email.",
        CheckEmailInput, _run_check, _assess_check, timeout=60, group="email",
    ),
    Tool("read_email", "Read one email's text. It is not marked as read. Attachments are listed but not opened.", ReadEmailInput, _run_read, _assess_read, timeout=60, group="email"),
]


def test_connection(core) -> int:
    """Sign in and count unread messages; used by the Settings panel."""
    box = _connect(core)
    try:
        return len(box.uid("search", None, "UNSEEN")[1][0].split())
    finally:
        _close(box)
