import os
import re
import time
import email as pyemail
from email.header import decode_header
from email.utils import getaddresses

from imapclient import IMAPClient


STYLE_RE = re.compile(r"<(style|script|head)[^>]*>.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
SENDERS = ("krea", "clerk")


def _decode_header_str(value):
    if not value:
        return ""
    out = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            for e in ([enc] if enc else []) + ["utf-8", "gb18030", "latin1"]:
                try:
                    out += text.decode(e)
                    break
                except Exception:
                    continue
        else:
            out += text
    return out


def extract_code(raw_bytes, recipient):
    msg = pyemail.message_from_bytes(raw_bytes)

    from_h = _decode_header_str(msg.get("From", ""))
    subject = _decode_header_str(msg.get("Subject", ""))
    if not any(s in from_h.lower() or s in subject.lower() for s in SENDERS):
        return ""

    to_headers = (
        msg.get_all("To", []) + msg.get_all("Delivered-To", [])
        + msg.get_all("X-Original-To", []) + msg.get_all("X-Forwarded-To", [])
    )
    all_rcpt = " ".join(a.lower() for _, a in getaddresses(to_headers))
    if recipient and recipient.lower() not in all_rcpt:
        return ""

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body += payload.decode(charset, errors="ignore")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
        except Exception:
            pass
    if not body:
        return ""

    text = STYLE_RE.sub(" ", body)
    m = re.search(r'class="code-box"[^>]*>\s*([0-9A-Za-z]{4,10})\s*<', text)
    if m:
        return m.group(1)
    text = TAG_RE.sub(" ", text)
    m = re.search(r"(?<!\d)(\d{8})(?!\d)", text)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return m.group(1) if m else ""


def wait_for_code(host, user, password, recipient, timeout=180):
    port = int(os.getenv("IMAP_PORT", "993"))
    start = time.time()
    print(f"[邮件] 连接 {host}:{port}，IDLE 实时等待 -> {recipient}", flush=True)
    client = IMAPClient(host, port=port, ssl=True)
    client.login(user, password)
    client.select_folder("INBOX")

    existing = client.search("ALL")
    baseline = max(existing) if existing else 0
    client.idle()
    try:
        while time.time() - start < timeout:
            remaining = timeout - (time.time() - start)
            responses = client.idle_check(timeout=min(remaining, 29))
            if not responses:
                client.idle_done()
                client.idle()
                continue
            client.idle_done()
            new_uids = [u for u in client.search("ALL") if u > baseline]
            for uid in sorted(new_uids, reverse=True):
                data = client.fetch([uid], ["RFC822"])
                if uid not in data or b"RFC822" not in data[uid]:
                    continue
                code = extract_code(data[uid][b"RFC822"], recipient)
                if code:
                    print(f"[邮件] 验证码: {code}（耗时 {int(time.time()-start)}s）", flush=True)
                    return code
            client.idle()
    finally:
        try:
            client.idle_done()
            client.logout()
        except Exception:
            pass
    print("[邮件] 超时未收到验证码", flush=True)
    return ""


def fetch_verification_code(timeout=180, since=None, recipient=None):
    host = os.getenv("IMAP_HOST")
    user = os.getenv("IMAP_USER")
    pwd = os.getenv("IMAP_PASS")
    if not (host and user and pwd):
        print("[邮件] 未配置 IMAP", flush=True)
        return ""
    return wait_for_code(host, user, pwd, recipient or os.getenv("KREA_EMAIL", ""), timeout=timeout)
