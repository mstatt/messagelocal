#!/usr/bin/env python3
"""MessageLocal - a read-only, offline email archive.

Pulls mail over IMAP (read-only, never deletes anything on either side) or
imports backups (cPanel archives, Maildir, mbox, .eml), stores every message
as a raw .eml file and indexes it in SQLite for browsing and search.
There is no SMTP and no way to send mail.
"""
import base64
import bz2
import calendar
import gzip
import hashlib
import html
import imaplib
import json
import lzma
import mailbox
import os
import re
import shutil
import sqlite3
import ssl
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from cryptography.fernet import Fernet, InvalidToken

try:
    import zstandard
except ImportError:  # optional: only needed for zstd-compressed backups
    zstandard = None

DATA = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "9950"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAIL_DIR = os.path.join(DATA, "mail")
TMP = os.path.join(tempfile.gettempdir(), "messagelocal")
imaplib._MAXLINE = 20_000_000

os.makedirs(MAIL_DIR, exist_ok=True)
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

# ---------------------------------------------------------------- database
LOCK = threading.RLock()
DB = sqlite3.connect(os.path.join(DATA, "messagelocal.db"), check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY, name TEXT, email TEXT, host TEXT, port INTEGER,
  security TEXT, verify_tls INTEGER DEFAULT 1, username TEXT, password_enc TEXT,
  last_sync INTEGER);
CREATE TABLE IF NOT EXISTS folders(
  id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, imap_name TEXT,
  uidvalidity INTEGER, last_uid INTEGER DEFAULT 0, UNIQUE(account_id, name));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, account_id INTEGER, folder_id INTEGER, sha TEXT,
  subject TEXT, from_name TEXT, from_addr TEXT, to_addrs TEXT, date_ts INTEGER,
  size INTEGER, attachments INTEGER, snippet TEXT,
  UNIQUE(account_id, folder_id, sha));
CREATE INDEX IF NOT EXISTS idx_msg_list ON messages(account_id, folder_id, date_ts);
CREATE INDEX IF NOT EXISTS idx_msg_date ON messages(date_ts);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  subject, sender, recipients, body, tokenize='unicode61 remove_diacritics 2');
""")


for col in ("thread", "kinds"):  # added for the constellation view
    if col not in [r["name"] for r in DB.execute("PRAGMA table_info(messages)")]:
        DB.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
for table, col in (("accounts", "hidden"), ("accounts", "is_default"), ("folders", "hidden")):
    if col not in [r["name"] for r in DB.execute(f"PRAGMA table_info({table})")]:
        DB.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER DEFAULT 0")
DB.executescript("""
CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(account_id, from_addr COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(account_id, thread);
""")
DB.commit()


def q(sql, args=()):
    with LOCK:
        return DB.execute(sql, args).fetchall()


def run(sql, args=()):
    with LOCK:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- secrets
def _fernet():
    env = os.environ.get("MESSAGELOCAL_KEY")
    if env:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest()))
    path = os.path.join(DATA, "secret.key")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(Fernet.generate_key())
        os.chmod(path, 0o600)
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


FERNET = _fernet()


# ---------------------------------------------------------------- parsing
def hdr(msg, name):
    v = msg.get(name)
    if v is None:
        return ""
    try:
        v = str(make_header(decode_header(str(v))))
    except Exception:
        v = str(v)
    return re.sub(r"\s+", " ", v).strip()


def part_text(p):
    raw = p.get_payload(decode=True) or b""
    cs = p.get_content_charset() or "utf-8"
    try:
        return raw.decode(cs, "replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", "replace")


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(h):
    s = _Strip()
    try:
        s.feed(h)
    except Exception:
        return re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(s.out)).strip()


MEDIA_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
             "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp",
             "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "ogv": "video/ogg",
             "mov": "video/quicktime"}
MEDIA_TYPES = set(MEDIA_EXT.values()) | {"image/jpg", "image/pjpeg"}


def media_mime(ctype, filename):
    """Browser-playable mime type for an image/video attachment, else None.
    Falls back to the file extension, since many senders label media as generic binary."""
    if ctype in MEDIA_TYPES:
        return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(ctype, ctype)
    if ctype in ("application/octet-stream", "binary/octet-stream") or ctype.startswith(("image/", "video/")):
        return MEDIA_EXT.get((filename or "").rsplit(".", 1)[-1].lower())
    return None


KIND_EXT = {
    "image": "png jpg jpeg gif webp avif bmp heic heif tif tiff svg",
    "video": "mp4 m4v mov webm ogv avi mkv wmv 3gp",
    "audio": "mp3 m4a wav ogg flac aac",
    "pdf": "pdf",
    "document": "doc docx odt rtf txt pages md",
    "spreadsheet": "xls xlsx xlsm csv ods numbers",
    "presentation": "ppt pptx key odp",
    "archive": "zip rar 7z tar gz tgz bz2",
}
EXT_KIND = {e: k for k, exts in KIND_EXT.items() for e in exts.split()}
KIND_LABEL = {"image": "Images", "video": "Videos", "audio": "Audio", "pdf": "PDFs",
              "document": "Documents", "spreadsheet": "Spreadsheets",
              "presentation": "Presentations", "archive": "Archives", "other": "Other files"}


def kind_of(ctype, filename):
    k = EXT_KIND.get((filename or "").rsplit(".", 1)[-1].lower())
    if k:
        return k
    if ctype.split("/")[0] in ("image", "video", "audio"):
        return ctype.split("/")[0]
    return "pdf" if ctype == "application/pdf" else "other"


def kinds_of(parts):
    ks = sorted({kind_of(a["ctype"], a["filename"]) for a in parts if not a["inline"]})
    return "," + ",".join(ks) + "," if ks else ""


SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|aw|wg|tr|rv|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)


def clean_subject(s):
    return re.sub(r"\s+", " ", SUBJECT_PREFIX.sub("", s or "")).strip()


def analyze(msg):
    """Return (plain_text, html, parts). parts are attachments / inline files."""
    text = htm = None
    parts = []
    for i, p in enumerate(msg.walk()):
        if p.is_multipart():
            continue
        ctype = p.get_content_type()
        try:
            disp = p.get_content_disposition()
            fn = p.get_filename()
        except Exception:
            disp, fn = None, None
        if fn:
            try:
                fn = str(make_header(decode_header(fn)))
            except Exception:
                pass
        if disp != "attachment" and not fn:
            if ctype == "text/plain" and text is None:
                text = part_text(p)
                continue
            if ctype == "text/html" and htm is None:
                htm = part_text(p)
                continue
        try:
            size = len(p.get_payload(decode=True) or b"")
        except Exception:
            size = 0
        cid = (p.get("Content-ID") or "").strip().strip("<>")
        ext = {"text/plain": "txt", "text/html": "html"}.get(ctype, ctype.split("/")[-1][:8])
        name = fn or f"part-{i}.{ext}"
        mime = media_mime(ctype, name)
        parts.append({"idx": i, "filename": name, "ctype": ctype, "size": size, "cid": cid,
                      "mime": mime, "media": mime.split("/")[0] if mime else None})
    for a in parts:
        a["inline"] = bool(a["cid"] and htm and ("cid:" + a["cid"]) in htm)
    return text, htm, parts


def msg_date(msg, fallback):
    cands = [msg.get("Date")]
    rec = msg.get("Received")
    if rec and ";" in str(rec):
        cands.append(str(rec).rsplit(";", 1)[1])
    for c in cands:
        try:
            d = parsedate_to_datetime(str(c).strip())
            ts = int(d.timestamp()) if d.tzinfo else int(time.mktime(d.timetuple()))
            if 0 < ts < 4102444800:
                return ts
        except Exception:
            continue
    return int(fallback or time.time())


def unpack(raw):
    """Mail servers and backup tools often store each message compressed on disk
    (gzip, bzip2, xz or zstd). Return the plain message."""
    for _ in range(2):
        try:
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif raw[:3] == b"BZh":
                raw = bz2.decompress(raw)
            elif raw[:6] == b"\xfd7zXZ\x00":
                raw = lzma.decompress(raw)
            elif raw[:4] == b"\x28\xb5\x2f\xfd" and zstandard:
                raw = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
            else:
                break
        except Exception:
            break
    return raw


HEADER_LINE = re.compile(rb"(From |>From |[!-9;-~]+[ \t]*:)")


def looks_like_mail(raw):
    head = raw[:2048]
    if b"\x00" in head:
        return False
    return any(HEADER_LINE.match(line) for line in head.splitlines()[:5])


def eml_path(account_id, sha):
    return os.path.join(MAIL_DIR, str(account_id), sha[:2], sha + ".eml")


def store_message(account_id, folder_id, raw, fallback_ts=None, strict=False):
    """Save one raw message. Returns True if new, False if already archived,
    None if (strict) the data is not a readable email."""
    if not raw or not raw.strip():
        return False
    raw = unpack(raw)
    if strict and not looks_like_mail(raw):
        return None
    sha = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    if q("SELECT 1 FROM messages WHERE account_id=? AND folder_id=? AND sha=?",
         (account_id, folder_id, sha)):
        return False
    path = eml_path(account_id, sha)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(raw)
        os.replace(path + ".part", path)
    msg = message_from_bytes(raw)
    try:
        text, htm, parts = analyze(msg)
    except Exception:
        text, htm, parts = "", None, []
    body = text if text and text.strip() else (html_to_text(htm) if htm else "")
    body = body[:300_000]
    frm = getaddresses([hdr(msg, "From")]) or [("", "")]
    to = ", ".join(x for x in (hdr(msg, "To"), hdr(msg, "Cc")) if x)
    natt = sum(1 for a in parts if not a["inline"])
    names = " ".join(a["filename"] for a in parts)
    with LOCK:
        cur = DB.execute(
            "INSERT OR IGNORE INTO messages(account_id,folder_id,sha,subject,from_name,"
            "from_addr,to_addrs,date_ts,size,attachments,snippet,thread,kinds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, folder_id, sha, hdr(msg, "Subject"), frm[0][0], frm[0][1], to,
             msg_date(msg, fallback_ts), len(raw), natt,
             re.sub(r"\s+", " ", body)[:200].strip(),
             clean_subject(hdr(msg, "Subject")).lower(), kinds_of(parts)))
        if cur.rowcount:
            DB.execute("INSERT INTO fts(rowid,subject,sender,recipients,body) VALUES(?,?,?,?,?)",
                       (cur.lastrowid, hdr(msg, "Subject"), hdr(msg, "From"), to,
                        body + " " + names))
        DB.commit()
        return bool(cur.rowcount)


def get_folder(account_id, name, imap_name=None):
    name = name.strip("/") or "INBOX"
    if name.upper() == "INBOX":
        name = "INBOX"
    with LOCK:
        r = q("SELECT * FROM folders WHERE account_id=? AND name=?", (account_id, name))
        if r:
            if imap_name and r[0]["imap_name"] != imap_name:
                run("UPDATE folders SET imap_name=? WHERE id=?", (imap_name, r[0]["id"]))
                return q("SELECT * FROM folders WHERE id=?", (r[0]["id"],))[0]
            return r[0]
        fid = run("INSERT INTO folders(account_id,name,imap_name) VALUES(?,?,?)",
                  (account_id, name, imap_name))
        return q("SELECT * FROM folders WHERE id=?", (fid,))[0]


# ---------------------------------------------------------------- jobs
JOBS = {}


def new_job(kind, label, account_id=None):
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "label": label, "account_id": account_id,
           "status": "running", "detail": "Starting", "done": 0, "total": 0, "added": 0, "skipped": 0,
           "error": None, "started": time.time(), "finished": None}
    JOBS[job["id"]] = job
    for old in sorted(JOBS.values(), key=lambda j: j["started"])[:-25]:
        if old["status"] != "running":
            JOBS.pop(old["id"], None)
    return job


def do_job(job, fn, *args):
    if True:
        try:
            fn(job, *args)
            job["status"] = "done"
            job["detail"] = job.get("summary") or f"{job['added']} new message{'s' if job['added'] != 1 else ''}"
            if job["skipped"]:
                job["detail"] += f", {job['skipped']} unreadable file{'s' if job['skipped'] != 1 else ''} skipped"
        except Exception as e:
            if not isinstance(e, RuntimeError):
                traceback.print_exc()
            job["status"] = "error"
            job["error"] = friendly_error(e)
        job["finished"] = time.time()


def run_job(job, fn, *args):
    threading.Thread(target=do_job, args=(job, fn) + args, daemon=True).start()


def friendly_error(e):
    s = str(e)
    if isinstance(e, ssl.SSLCertVerificationError):
        return ("The server's security certificate doesn't match this server name. Try the "
                "hostname your provider lists for IMAP, or turn off certificate verification.")
    if isinstance(e, imaplib.IMAP4.error):
        if isinstance(e.args[0] if e.args else None, bytes):
            s = e.args[0].decode("utf-8", "replace")
        return "Mail server said: " + s
    if isinstance(e, (TimeoutError, ConnectionError, OSError)) and not isinstance(e, ssl.SSLError):
        return f"Couldn't reach the mail server ({s}). Check the server name and port."
    return s or e.__class__.__name__


# ---------------------------------------------------------------- IMAP
def mutf7(s):
    def dec(m):
        if not m.group(1):
            return "&"
        b = m.group(1).replace(",", "/")
        try:
            return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"&([^-]*)-", dec, s)


def imap_connect(host, port, security, verify, user, pw):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if security == "ssl":
        M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=90)
    else:
        M = imaplib.IMAP4(host, port, timeout=90)
        if security == "starttls":
            M.starttls(ctx)
    M.login(user, pw)
    return M


LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.S)


def imap_folders(M):
    typ, data = M.list()
    out = []
    for item in data or []:
        literal = None
        if isinstance(item, tuple):
            item, literal = item[0], item[1]
        if not item:
            continue
        m = LIST_RE.match(item)
        if not m or b"\\noselect" in m.group("flags").lower():
            continue
        delim = m.group("delim")
        delim = None if delim == b"NIL" else delim[1:-1].decode().replace("\\\\", "\\").replace('\\"', '"')
        if literal is not None:
            raw = literal.decode("utf-8", "replace")
        else:
            raw = m.group("name").strip().decode("utf-8", "replace")
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        bits = mutf7(raw).split(delim) if delim else [mutf7(raw)]
        if len(bits) > 1 and bits[0].upper() == "INBOX":
            bits = bits[1:]
        out.append((raw, "/".join(b for b in bits if b)))
    return out


def sync_account(job, acct):
    try:
        pw = FERNET.decrypt(acct["password_enc"].encode()).decode()
    except InvalidToken:
        raise RuntimeError("The saved password can't be decrypted (the encryption key changed). "
                           "Add the account again to re-enter it.")
    job["detail"] = "Connecting"
    M = imap_connect(acct["host"], acct["port"], acct["security"], acct["verify_tls"],
                     acct["username"], pw)
    try:
        folders = imap_folders(M)
        for raw_name, display in folders:
            job["detail"] = display
            quoted = '"' + raw_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
            try:
                typ, _ = M.select(quoted, readonly=True)
            except imaplib.IMAP4.error:
                continue
            if typ != "OK":
                continue
            uv = M.response("UIDVALIDITY")[1]
            uv = int(uv[0]) if uv and uv[0] else 0
            f = get_folder(acct["id"], display, raw_name)
            last = f["last_uid"] if f["uidvalidity"] == uv else 0
            typ, d = M.uid("SEARCH", "UID", f"{last + 1}:*")
            uids = sorted(int(x) for x in (d[0] or b"").split() if int(x) > last)
            job["total"] += len(uids)
            for i in range(0, len(uids), 20):
                chunk = uids[i:i + 20]
                typ, d = M.uid("FETCH", ",".join(map(str, chunk)), "(UID INTERNALDATE BODY.PEEK[])")
                for item in d or []:
                    if not isinstance(item, tuple):
                        continue
                    ts = None
                    try:
                        t = imaplib.Internaldate2tuple(item[0])
                        ts = time.mktime(t) if t else None
                    except Exception:
                        pass
                    try:
                        if store_message(acct["id"], f["id"], item[1], ts):
                            job["added"] += 1
                    except Exception:
                        traceback.print_exc()
                    job["done"] += 1
                run("UPDATE folders SET last_uid=?, uidvalidity=? WHERE id=?", (chunk[-1], uv, f["id"]))
            if not uids:
                run("UPDATE folders SET uidvalidity=? WHERE id=?", (uv, f["id"]))
        run("UPDATE accounts SET last_sync=? WHERE id=?", (int(time.time()), acct["id"]))
    finally:
        try:
            M.logout()
        except Exception:
            pass


def start_sync(account_id):
    for j in JOBS.values():
        if j["status"] == "running" and j["kind"] == "sync" and j["account_id"] == account_id:
            return j
    acct = q("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acct or not acct[0]["host"]:
        return None
    job = new_job("sync", f"Syncing {acct[0]['name']}", account_id)
    run_job(job, sync_account, acct[0])
    return job


# ---------------------------------------------------------------- import
JUNK = re.compile(r"^(dovecot|maildirsize|maildirfolder|subscriptions|courier)|\.(msf|dat|log|index|cache)$", re.I)


def _safe_dest(root, name):
    bits = [b for b in name.replace("\\", "/").split("/") if b not in ("", ".", "..")]
    if not bits:
        return None
    bits[-1] = re.sub(r'[:*?"<>|]', "_", bits[-1])
    return os.path.join(root, *bits)


def extract(path, out):
    """Extract zip/tar (any compression) safely. Returns False if not an archive."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                dest = None if info.is_dir() else _safe_dest(out, info.filename)
                if dest:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return True
    try:
        is_tar = tarfile.is_tarfile(path)
    except Exception:
        is_tar = False
    if is_tar:
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                dest = _safe_dest(out, m.name) if m.isfile() else None
                if dest:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with tf.extractfile(m) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return True
    return False


def maildir_info(root):
    """For a Maildir (the parent of cur/new) return (folder_name, mailbox_email or None)."""
    name = os.path.basename(root)
    if name.startswith(".") and len(name) > 1:
        folder, box = name[1:].replace(".", "/"), os.path.dirname(root)
        if folder.upper().startswith("INBOX/"):
            folder = folder[6:]
    else:
        folder, box = "INBOX", root
    bits = box.replace("\\", "/").split("/")
    email = None  # cPanel layout: .../mail/<domain>/<user>/
    if len(bits) >= 3 and bits[-3] == "mail" and "." in bits[-2]:
        email = f"{bits[-1]}@{bits[-2]}".lower()
    return folder, email


def scan(root):
    """Yield (kind, path, folder, mailbox_email) for every mail source under root."""
    for cur, dirs, files in os.walk(root):
        base = os.path.basename(cur)
        if base in ("cur", "new"):
            folder, email = maildir_info(os.path.dirname(cur))
            for fn in files:
                yield "eml", os.path.join(cur, fn), folder, email
            continue
        if base == "tmp" and os.path.isdir(os.path.join(os.path.dirname(cur), "cur")):
            continue
        for fn in files:
            p = os.path.join(cur, fn)
            if JUNK.search(fn):
                continue
            if fn.lower().endswith(".eml"):
                yield "eml", p, (base if cur != root else "Imported"), None
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(5)
            except OSError:
                continue
            if head == b"From ":
                stem = re.sub(r"\.mbox$", "", fn, flags=re.I)
                yield "mbox", p, stem or "Imported", None


def import_backup(job, path, filename, target):
    work = tempfile.mkdtemp(dir=TMP)
    try:
        job["detail"] = "Unpacking"
        if extract(path, work):
            os.remove(path)
            for cur, dirs, files in os.walk(work):  # older cPanel full backups nest the mail
                for fn in files:
                    if fn == "homedir.tar":
                        inner = os.path.join(cur, fn)
                        extract(inner, os.path.join(cur, "homedir"))
                        os.remove(inner)
        else:
            shutil.move(path, os.path.join(work, re.sub(r'[/\\:*?"<>|]', "_", filename) or "upload"))
        job["detail"] = "Scanning"
        items = list(scan(work))
        if not items:
            raise RuntimeError("No email found in that file. Supported: cPanel backups, Maildir "
                               "folders, mbox files and .eml files (loose or in a .zip / .tar.gz).")
        emails = {e for _, _, _, e in items if e}
        route = {}

        def account_for(email):
            key = email if len(emails) > 1 else None
            if key in route:
                return route[key]
            aid = None
            if key:
                r = q("SELECT id FROM accounts WHERE lower(email)=?", (key,))
                aid = r[0]["id"] if r else run("INSERT INTO accounts(name,email) VALUES(?,?)", (key, key))
            elif target:
                aid = target
            else:
                only = next(iter(emails)) if len(emails) == 1 else None
                r = q("SELECT id FROM accounts WHERE lower(email)=?", (only,)) if only else None
                aid = r[0]["id"] if r else run(
                    "INSERT INTO accounts(name,email) VALUES(?,?)",
                    (only or re.sub(r"(\.(zip|tar|tgz|gz|bz2|xz|mbox|eml))+$", "", filename, flags=re.I) or "Imported archive", only))
            route[key] = aid
            return aid

        job["total"] = len(items)
        for kind, p, folder, email in items:
            aid = account_for(email)
            fid = get_folder(aid, folder)["id"]
            job["detail"] = folder
            if kind == "eml":
                try:
                    with open(p, "rb") as f:
                        r = store_message(aid, fid, f.read(), os.path.getmtime(p), strict=True)
                    if r:
                        job["added"] += 1
                    elif r is None:
                        job["skipped"] += 1
                except Exception:
                    traceback.print_exc()
            else:
                box = mailbox.mbox(p, create=False)
                try:
                    keys = box.keys()
                    job["total"] += len(keys)
                    for k in keys:
                        try:
                            r = store_message(aid, fid, box.get_bytes(k), strict=True)
                            if r:
                                job["added"] += 1
                            elif r is None:
                                job["skipped"] += 1
                        except Exception:
                            traceback.print_exc()
                        job["done"] += 1
                finally:
                    box.close()
            job["done"] += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------- repair
def drop_message(row):
    with LOCK:
        DB.execute("DELETE FROM fts WHERE rowid=?", (row["id"],))
        DB.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        DB.commit()
        shared = q("SELECT 1 FROM messages WHERE account_id=? AND sha=?", (row["account_id"], row["sha"]))
    if not shared:
        try:
            os.remove(eml_path(row["account_id"], row["sha"]))
        except OSError:
            pass


def repair_archive(job):
    """Fix messages that an earlier version imported while still compressed:
    decompress and re-index them, and drop entries that are not email at all."""
    rows = q("SELECT id, account_id, folder_id, sha, date_ts FROM messages")
    job["total"] = len(rows)
    fixed = removed = 0
    for row in rows:
        job["done"] += 1
        path = eml_path(row["account_id"], row["sha"])
        try:
            with open(path, "rb") as f:
                head = f.read(2048)
                if looks_like_mail(head):
                    continue
                raw = head + f.read()
        except OSError:
            continue
        plain = unpack(raw)
        ok = plain is not raw and looks_like_mail(plain)
        if ok or b"\x00" in head:
            drop_message(row)
            if ok:
                store_message(row["account_id"], row["folder_id"], plain, row["date_ts"])
                fixed += 1
            else:
                removed += 1
        job["detail"] = f"{fixed} repaired"
    run("INSERT OR REPLACE INTO meta(key,value) VALUES('repair_compressed','1')")
    job["summary"] = f"{fixed} repaired, {removed} non-mail entr{'y' if removed == 1 else 'ies'} removed"


def reindex(job):
    """Fill in thread and attachment-type data for mail archived by an earlier version."""
    rows = q("SELECT id, account_id, sha, subject, attachments FROM messages WHERE kinds IS NULL OR thread IS NULL")
    job["total"] = len(rows)
    for n, row in enumerate(rows, 1):
        kinds = ""
        if row["attachments"]:
            try:
                with open(eml_path(row["account_id"], row["sha"]), "rb") as f:
                    kinds = kinds_of(analyze(message_from_bytes(f.read()))[2])
            except Exception:
                kinds = ",other,"
        with LOCK:
            DB.execute("UPDATE messages SET thread=?, kinds=? WHERE id=?",
                       (clean_subject(row["subject"]).lower(), kinds, row["id"]))
            if n % 200 == 0:
                DB.commit()
        job["done"] = n
    with LOCK:
        DB.commit()
    job["summary"] = f"{len(rows)} messages updated"


def startup_tasks():
    if not q("SELECT 1 FROM meta WHERE key='repair_compressed'"):
        if q("SELECT 1 FROM messages LIMIT 1"):
            do_job(new_job("repair", "Repairing archive"), repair_archive)
        else:
            run("INSERT OR REPLACE INTO meta(key,value) VALUES('repair_compressed','1')")
    if q("SELECT 1 FROM messages WHERE kinds IS NULL OR thread IS NULL LIMIT 1"):
        do_job(new_job("reindex", "Updating index"), reindex)


# ---------------------------------------------------------------- API helpers
FOLDER_ORDER = ["inbox", "sent", "sent items", "sent messages", "drafts", "archive", "junk", "spam", "trash"]


def folder_key(name):
    n = name.lower()
    return (FOLDER_ORDER.index(n) if n in FOLDER_ORDER else 99, n)


def accounts_json():
    out = []
    counts = {r["folder_id"]: r["c"] for r in q("SELECT folder_id, COUNT(*) c FROM messages GROUP BY folder_id")}
    for a in q("SELECT * FROM accounts ORDER BY id"):
        fs = [{"id": f["id"], "name": f["name"], "count": counts.get(f["id"], 0), "hidden": bool(f["hidden"])}
              for f in q("SELECT * FROM folders WHERE account_id=?", (a["id"],))]
        fs = sorted((f for f in fs if f["count"]), key=lambda f: folder_key(f["name"]))
        out.append({"id": a["id"], "name": a["name"], "email": a["email"], "host": a["host"],
                    "kind": "imap" if a["host"] else "local", "last_sync": a["last_sync"],
                    "hidden": bool(a["hidden"]), "default": bool(a["is_default"]),
                    "total": sum(f["count"] for f in fs if not f["hidden"]), "folders": fs})
    return out


def fts_query(s):
    toks = re.findall(r"[\w@.\-']+", s, re.U)
    return " ".join('"' + t.replace('"', "") + '"*' for t in toks[:12])


def build_filter(qs):
    """Shared WHERE clause for message lists and stats. Returns (join, where, args)."""
    where, args, join = [], [], ""
    if qs.get("hidden") != "1":  # hidden accounts/folders drop out unless asked for by id
        if not qs.get("account", "").isdigit():
            where.append("m.account_id NOT IN (SELECT id FROM accounts WHERE hidden=1)")
        if not qs.get("folder", "").isdigit():
            where.append("m.folder_id NOT IN (SELECT id FROM folders WHERE hidden=1)")
    for key, col in (("account", "m.account_id"), ("folder", "m.folder_id")):
        if qs.get(key, "").isdigit():
            where.append(col + "=?"); args.append(int(qs[key]))
    if qs.get("person"):
        where.append("m.from_addr=? COLLATE NOCASE"); args.append(qs["person"])
    if qs.get("year", "").isdigit() and 1970 <= int(qs["year"]) < 2100:
        y = int(qs["year"])
        where.append("m.date_ts>=? AND m.date_ts<?")
        args += [calendar.timegm((y, 1, 1, 0, 0, 0)), calendar.timegm((y + 1, 1, 1, 0, 0, 0))]
    if qs.get("kind") in KIND_LABEL:
        where.append("m.kinds LIKE ?"); args.append(f"%,{qs['kind']},%")
    if "thread" in qs:
        where.append("COALESCE(m.thread,'')=?"); args.append(qs["thread"])
    if qs.get("attach") == "1":
        where.append("m.attachments>0")
    term = fts_query(qs.get("q", ""))
    if term:
        join = "JOIN fts ON fts.rowid=m.id"
        where.append("fts MATCH ?"); args.append(term)
    return join, ("WHERE " + " AND ".join(where)) if where else "", args


def stats_json(qs):
    join, w, args = build_filter(qs)
    r = q(f"SELECT COUNT(*) c, COALESCE(SUM(m.attachments>0),0) a, COUNT(DISTINCT m.thread) t, "
          f"MIN(m.date_ts) f, MAX(m.date_ts) l FROM messages m {join} {w}", args)[0]
    months = []
    if r["c"]:
        end = time.gmtime(r["l"])
        y, mo = end.tm_year, end.tm_mon
        labels = []
        for _ in range(24):
            labels.append(f"{y:04d}-{mo:02d}")
            y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
        labels.reverse()
        start = calendar.timegm((int(labels[0][:4]), int(labels[0][5:]), 1, 0, 0, 0))
        got = {x["ym"]: x["c"] for x in q(
            f"SELECT strftime('%Y-%m', m.date_ts, 'unixepoch') ym, COUNT(*) c FROM messages m {join} "
            f"{w + ' AND' if w else 'WHERE'} m.date_ts>=? GROUP BY ym", args + [start])}
        months = [got.get(k, 0) for k in labels]
    return {"count": r["c"], "with_attachments": r["a"], "threads": r["t"],
            "first_ts": r["f"], "last_ts": r["l"], "months": months}


def threads_json(qs, limit=300):
    """Conversations (messages grouped by subject) within any filter."""
    join, w, args = build_filter(qs)
    rows = q(f"SELECT m.thread, MAX(m.subject) subject, COUNT(*) c, MAX(m.date_ts) last, "
             f"COALESCE(SUM(m.attachments>0),0) a FROM messages m {join} {w} "
             f"GROUP BY m.thread ORDER BY last DESC LIMIT ?", args + [limit])
    return [{"thread": r["thread"] or "", "label": clean_subject(r["subject"]) or "(no subject)",
             "count": r["c"], "last_ts": r["last"], "with_attachments": r["a"]} for r in rows]


VISIBLE = ("m.account_id NOT IN (SELECT id FROM accounts WHERE hidden=1) "
           "AND m.folder_id NOT IN (SELECT id FROM folders WHERE hidden=1)")


def timeline_json():
    """Monthly message counts across every visible mailbox and folder."""
    rows = [dict(r) for r in q(f"SELECT strftime('%Y-%m', m.date_ts, 'unixepoch') ym, COUNT(*) c "
                               f"FROM messages m WHERE {VISIBLE} GROUP BY ym ORDER BY ym")]
    total = sum(r["c"] for r in rows)
    months = lambda ym: int(ym[:4]) * 12 + int(ym[5:])
    # A stray message with a bogus date (1970, 2099) would stretch the scale: trim such outliers.
    for end in (0, -1):
        while len(rows) > 2 and rows[end]["c"] <= max(1, total * 0.002) and \
                abs(months(rows[end]["ym"]) - months(rows[1 if end == 0 else -2]["ym"])) > 24:
            rows.pop(end)
    return {"total": total, "buckets": rows}


def timeline_row(where, args, order):
    r = q(f"SELECT m.id, m.subject, m.from_name, m.from_addr, m.to_addrs, m.date_ts, m.attachments, "
          f"m.snippet, COALESCE(m.kinds,'') kinds, f.name folder, a.name account FROM messages m "
          f"JOIN folders f ON f.id=m.folder_id JOIN accounts a ON a.id=m.account_id "
          f"WHERE {VISIBLE} AND {where} ORDER BY m.date_ts {order}, m.id {order} LIMIT 1", args)
    if not r:
        return None
    row = dict(r[0])
    row["pos"] = q(f"SELECT COUNT(*) c FROM messages m WHERE {VISIBLE} AND (m.date_ts<? OR (m.date_ts=? AND m.id<?))",
                   (row["date_ts"], row["date_ts"], row["id"]))[0]["c"] + 1
    row["total"] = q(f"SELECT COUNT(*) c FROM messages m WHERE {VISIBLE}")[0]["c"]
    return row


def timeline_at(ts):
    return timeline_row("m.date_ts>=?", (ts,), "ASC") or timeline_row("1=1", (), "DESC")


def timeline_step(mid, direction):
    cur = q("SELECT id, date_ts FROM messages WHERE id=?", (mid,))
    if not cur:
        return None
    d, i = cur[0]["date_ts"], cur[0]["id"]
    if direction > 0:
        return timeline_row("(m.date_ts>? OR (m.date_ts=? AND m.id>?))", (d, d, i), "ASC")
    if direction < 0:
        return timeline_row("(m.date_ts<? OR (m.date_ts=? AND m.id<?))", (d, d, i), "DESC")
    return timeline_row("m.id=?", (i,), "ASC")


def constellation_json(aid, lens, top=400):
    own = (q("SELECT email FROM accounts WHERE id=?", (aid,)) or [{"email": ""}])[0]["email"] or ""
    VIS = "folder_id NOT IN (SELECT id FROM folders WHERE hidden=1)"
    people_where = f"account_id=? AND {VIS} AND from_addr!='' AND from_addr!=? COLLATE NOCASE"
    kinds_sql = ", ".join(f"COALESCE(SUM(kinds LIKE '%,{k},%'),0) {k}" for k in KIND_LABEL)
    kinds = q(f"SELECT {kinds_sql}, COALESCE(SUM(kinds!=''),0) any_ FROM messages WHERE account_id=? AND {VIS}", (aid,))[0]
    totals = {
        "people": q(f"SELECT COUNT(DISTINCT lower(from_addr)) c FROM messages WHERE {people_where}", (aid, own))[0]["c"],
        "folders": q(f"SELECT COUNT(DISTINCT folder_id) c FROM messages WHERE account_id=? AND {VIS}", (aid,))[0]["c"],
        "threads": q(f"SELECT COUNT(*) c FROM (SELECT 1 FROM messages WHERE account_id=? AND {VIS} AND thread!='' "
                     "GROUP BY thread HAVING COUNT(*)>1)", (aid,))[0]["c"],
        "files": kinds["any_"],
        "years": q(f"SELECT COUNT(DISTINCT strftime('%Y', date_ts, 'unixepoch')) c FROM messages WHERE account_id=? AND {VIS}", (aid,))[0]["c"],
    }
    items = []
    if lens == "people":
        for r in q(f"SELECT from_addr addr, MAX(from_name) name, COUNT(*) c FROM messages WHERE {people_where} "
                   "GROUP BY from_addr COLLATE NOCASE ORDER BY c DESC, addr LIMIT ?", (aid, own, top)):
            items.append({"label": r["name"] or r["addr"], "sub": r["addr"], "count": r["c"], "filter": {"person": r["addr"]}})
    elif lens == "folders":
        rows = q("SELECT f.id, f.name, COUNT(*) c FROM messages m JOIN folders f ON f.id=m.folder_id "
                 "WHERE m.account_id=? AND f.hidden=0 GROUP BY f.id", (aid,))
        for r in sorted(rows, key=lambda r: folder_key(r["name"]))[:top]:
            items.append({"label": "Inbox" if r["name"] == "INBOX" else r["name"], "count": r["c"], "filter": {"folder": str(r["id"])}})
    elif lens == "threads":
        for r in q(f"SELECT thread, MAX(subject) subject, COUNT(*) c FROM messages WHERE account_id=? AND {VIS} AND thread!='' "
                   "GROUP BY thread HAVING c>1 ORDER BY c DESC, thread LIMIT ?", (aid, top)):
            items.append({"label": clean_subject(r["subject"]) or r["thread"], "count": r["c"], "filter": {"thread": r["thread"]}})
    elif lens == "files":
        for k in sorted(KIND_LABEL, key=lambda k: -kinds[k]):
            if kinds[k]:
                items.append({"label": KIND_LABEL[k], "count": kinds[k], "filter": {"kind": k}})
    elif lens == "years":
        for r in q(f"SELECT strftime('%Y', date_ts, 'unixepoch') y, COUNT(*) c FROM messages WHERE account_id=? AND {VIS} "
                   "GROUP BY y ORDER BY y DESC LIMIT ?", (aid, top)):
            items.append({"label": r["y"], "count": r["c"], "filter": {"year": r["y"]}})
    return {"totals": totals, "items": items}


def load_message(mid):
    r = q("SELECT m.*, f.name folder, a.name account FROM messages m JOIN folders f ON f.id=m.folder_id "
          "JOIN accounts a ON a.id=m.account_id WHERE m.id=?", (mid,))
    if not r:
        return None, None, None
    with open(eml_path(r[0]["account_id"], r[0]["sha"]), "rb") as f:
        raw = f.read()
    return r[0], raw, message_from_bytes(raw)


def part_by_idx(msg, idx):
    for i, p in enumerate(msg.walk()):
        if i == idx and not p.is_multipart():
            return p
    return None


REMOTE_IMG = re.compile(r"""(<img\b[^>]*\bsrc\s*=\s*["']?\s*https?:|url\(\s*["']?https?:)""", re.I)


def media_gallery(mid, parts):
    """HTML block showing image and video attachments beneath an HTML message."""
    out = []
    for a in parts:
        if a["inline"] or not a["media"]:
            continue
        url = f"/api/messages/{mid}/part/{a['idx']}?inline=1"
        name = html.escape(a["filename"])
        if a["media"] == "image":
            tag = f'<a href="{url}"><img src="{url}" alt="{name}" loading="lazy"></a>'
        else:
            tag = f'<video controls preload="metadata" src="{url}"></video>'
        out.append(f"<figure>{tag}<figcaption>{name}</figcaption></figure>")
    if not out:
        return ""
    return ('<style>.ml-media{margin:28px 0 0;padding:20px 0 0;border-top:1px solid #e3e6ea;display:grid;gap:20px}'
            '.ml-media figure{margin:0}.ml-media img,.ml-media video{display:block;max-width:100%;max-height:80vh;'
            'border-radius:8px;background:#f1f3f5}.ml-media figcaption{font:12px/1.4 system-ui,sans-serif;'
            'color:#6b7280;margin-top:6px}</style><div class="ml-media">' + "".join(out) + "</div>")


def render_html(msg, mid):
    text, htm, parts = analyze(msg)
    if htm is None:
        return None
    by_cid = {a["cid"]: a for a in parts if a["cid"]}

    def cid(m):
        a = by_cid.get(m.group(1))
        if not a or a["size"] > 8_000_000:
            return m.group(0)
        data = part_by_idx(msg, a["idx"]).get_payload(decode=True) or b""
        return f"data:{a['ctype']};base64,{base64.b64encode(data).decode()}"

    htm = re.sub(r"cid:([^\"'\s>)]+)", cid, htm)
    head = ('<!doctype html><meta charset="utf-8"><base target="_blank"><style>'
            "html{background:#fff}body{margin:20px 24px;font:15px/1.55 system-ui,-apple-system,"
            "Segoe UI,Roboto,sans-serif;color:#15181d;word-wrap:break-word}img{max-width:100%;"
            "height:auto}table{max-width:100%}</style>")
    return head + htm + media_gallery(mid, parts)


PART_CACHE = {}  # one decoded attachment, so video seeking doesn't re-decode the message


def part_bytes(mid, idx, msg):
    with LOCK:
        if PART_CACHE.get("key") == (mid, idx):
            return PART_CACHE["data"]
    part = part_by_idx(msg, idx)
    if part is None:
        return None
    data = part.get_payload(decode=True) or b""
    with LOCK:
        PART_CACHE.update(key=(mid, idx), data=data)
    return data


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MessageLocal"

    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, {"error": msg})

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def guard(self):
        # Blocks other websites from driving this app through the browser.
        if self.headers.get("X-MessageLocal") != "1":
            self.err(403, "Forbidden")
            return False
        return True

    # ---- GET
    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self.err(500, str(e))

    def route_get(self):
        u = urlparse(self.path)
        p, qs = u.path, {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        if p in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'"})
        if p == "/api/accounts":
            return self.send(200, accounts_json())
        if p == "/api/jobs":
            return self.send(200, sorted(JOBS.values(), key=lambda j: -j["started"]))
        if p == "/api/stats":
            return self.send(200, stats_json(qs))
        if p == "/api/timeline":
            return self.send(200, timeline_json())
        if p == "/api/timeline/at":
            return self.send(200, {"message": timeline_at(int(float(qs.get("ts") or 0)))})
        if p == "/api/timeline/step":
            return self.send(200, {"message": timeline_step(int(qs.get("id") or 0), int(qs.get("dir") or 0))})
        if p == "/api/threads":
            return self.send(200, threads_json(qs))
        if p == "/api/constellation":
            if not qs.get("account", "").isdigit():
                return self.err(400, "account is required")
            return self.send(200, constellation_json(int(qs["account"]), qs.get("lens", "people")))
        if p == "/api/messages":
            join, w, args = build_filter(qs)
            limit, offset = min(int(qs.get("limit", 60)), 200), int(qs.get("offset", 0))
            rows = q(f"SELECT m.id,m.subject,m.from_name,m.from_addr,m.to_addrs,m.date_ts,m.attachments,"
                     f"m.snippet,m.account_id,f.name folder FROM messages m JOIN folders f ON f.id=m.folder_id "
                     f"{join} {w} ORDER BY m.date_ts DESC, m.id DESC LIMIT ? OFFSET ?", args + [limit + 1, offset])
            total = q(f"SELECT COUNT(*) c FROM messages m {join} {w}", args)[0]["c"]
            return self.send(200, {"messages": [dict(r) for r in rows[:limit]],
                                   "more": len(rows) > limit, "total": total})
        m = re.fullmatch(r"/api/messages/(\d+)(?:/(html|raw|part/(\d+)))?", p)
        if m:
            row, raw, msg = load_message(int(m.group(1)))
            if not row:
                return self.err(404, "Message not found")
            sub = m.group(2)
            if not sub:
                text, htm, parts = analyze(msg)
                return self.send(200, {
                    "id": row["id"], "subject": hdr(msg, "Subject"), "from": hdr(msg, "From"),
                    "to": hdr(msg, "To"), "cc": hdr(msg, "Cc"), "date_ts": row["date_ts"],
                    "folder": row["folder"], "account": row["account"], "size": row["size"],
                    "text": text if text is not None else (html_to_text(htm) if htm else ""),
                    "has_html": htm is not None,
                    "has_remote": bool(htm and REMOTE_IMG.search(htm)),
                    "attachments": [{k: a[k] for k in ("idx", "filename", "ctype", "size", "media")}
                                    for a in parts if not a["inline"]]})
            if sub == "raw":
                return self.send(200, raw, "message/rfc822",
                                 {"Content-Disposition": f"attachment; filename=\"message-{row['id']}.eml\""})
            if sub == "html":
                remote = qs.get("remote") == "1"
                doc = render_html(msg, row["id"])
                if doc is None:
                    return self.err(404, "No HTML body")
                host = self.headers.get("Host", "")
                own = host if re.fullmatch(r"[\w.\-\[\]:]+", host) else ""
                img = f"data: {own}" + (" http: https:" if remote else "")
                media = own or "'none'"
                return self.send(200, doc, "text/html; charset=utf-8", {
                    "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; img-src {img}; "
                    f"media-src {media}; font-src data:; "
                    "sandbox allow-popups allow-popups-to-escape-sandbox"})
            idx = int(m.group(3))
            data = part_bytes(row["id"], idx, msg)
            if data is None:
                return self.err(404, "Attachment not found")
            _, _, parts = analyze(msg)
            meta = next((a for a in parts if a["idx"] == idx), None)
            name = meta["filename"] if meta else "attachment"
            inline = qs.get("inline") == "1" and meta and meta["mime"]
            heads = {"Content-Disposition": ("inline" if inline else "attachment") +
                     f"; filename*=UTF-8''{quote(name)}",
                     "Content-Security-Policy": "default-src 'none'; sandbox",
                     "Accept-Ranges": "bytes"}
            code, total = 200, len(data)
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if rng and (rng.group(1) or rng.group(2)):  # lets the browser seek within videos
                if rng.group(1):
                    start, end = int(rng.group(1)), min(int(rng.group(2) or total - 1), total - 1)
                else:
                    start, end = max(0, total - int(rng.group(2))), total - 1
                if start > end or start >= total:
                    return self.send(416, b"", "text/plain", {"Content-Range": f"bytes */{total}"})
                heads["Content-Range"] = f"bytes {start}-{end}/{total}"
                code, data = 206, data[start:end + 1]
            return self.send(code, data, meta["mime"] if inline else "application/octet-stream", heads)
        self.err(404, "Not found")

    # ---- POST / DELETE
    def do_POST(self):
        try:
            if self.guard():
                self.route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            if not isinstance(e, (imaplib.IMAP4.error, OSError)):
                traceback.print_exc()
            self.err(400, friendly_error(e))

    def route_post(self):
        u = urlparse(self.path)
        p, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        if p == "/api/accounts":
            b = self.body_json()
            email = (b.get("email") or "").strip()
            host = (b.get("host") or "").strip()
            user = (b.get("username") or email).strip()
            pw = b.get("password") or ""
            sec = b.get("security") if b.get("security") in ("ssl", "starttls", "none") else "ssl"
            port = int(b.get("port") or (993 if sec == "ssl" else 143))
            verify = 0 if b.get("verify_tls") is False else 1
            if not (host and user and pw):
                return self.err(400, "Server, username and password are required.")
            M = imap_connect(host, port, sec, verify, user, pw)  # raises with a readable error
            try:
                M.logout()
            except Exception:
                pass
            enc = FERNET.encrypt(pw.encode()).decode()
            name = (b.get("name") or email or user).strip()
            ex = q("SELECT id FROM accounts WHERE lower(email)=? AND email!=''", (email.lower(),)) if email else []
            if ex:
                aid = ex[0]["id"]
                run("UPDATE accounts SET host=?,port=?,security=?,verify_tls=?,username=?,password_enc=? WHERE id=?",
                    (host, port, sec, verify, user, enc, aid))
            else:
                aid = run("INSERT INTO accounts(name,email,host,port,security,verify_tls,username,password_enc) "
                          "VALUES(?,?,?,?,?,?,?,?)", (name, email, host, port, sec, verify, user, enc))
            job = start_sync(aid)
            return self.send(200, {"id": aid, "job": job["id"] if job else None})
        m = re.fullmatch(r"/api/accounts/(\d+)/sync", p)
        if m:
            job = start_sync(int(m.group(1)))
            return self.send(200, {"job": job["id"]}) if job else self.err(400, "This archive isn't connected to a mail server.")
        m = re.fullmatch(r"/api/(accounts|folders)/(\d+)/prefs", p)
        if m:
            b, table, rid = self.body_json(), m.group(1), int(m.group(2))
            with LOCK:
                if "hidden" in b:
                    DB.execute(f"UPDATE {table} SET hidden=? WHERE id=?", (1 if b["hidden"] else 0, rid))
                    if table == "accounts" and b["hidden"]:
                        DB.execute("UPDATE accounts SET is_default=0 WHERE id=?", (rid,))
                if "default" in b and table == "accounts":
                    DB.execute("UPDATE accounts SET is_default=0")
                    if b["default"]:
                        DB.execute("UPDATE accounts SET is_default=1, hidden=0 WHERE id=?", (rid,))
                DB.commit()
            return self.send(200, accounts_json())
        if p == "/api/import":
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self.err(400, "Empty upload.")
            filename = os.path.basename(qs.get("filename") or "upload")
            tmp = os.path.join(TMP, uuid.uuid4().hex)
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                os.remove(tmp)
                return self.err(400, "Upload was interrupted.")
            target = int(qs["account"]) if qs.get("account", "").isdigit() else None
            if target and not q("SELECT 1 FROM accounts WHERE id=?", (target,)):
                target = None
            job = new_job("import", f"Importing {filename}", target)
            run_job(job, import_backup, tmp, filename, target)
            return self.send(200, {"job": job["id"]})
        self.err(404, "Not found")

    def do_DELETE(self):
        try:
            if not self.guard():
                return
            m = re.fullmatch(r"/api/accounts/(\d+)", urlparse(self.path).path)
            if not m:
                return self.err(404, "Not found")
            aid = int(m.group(1))
            if any(j["status"] == "running" and j["account_id"] == aid for j in JOBS.values()):
                return self.err(400, "Wait for the running sync or import to finish first.")
            with LOCK:
                DB.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM messages WHERE account_id=?)", (aid,))
                DB.execute("DELETE FROM messages WHERE account_id=?", (aid,))
                DB.execute("DELETE FROM folders WHERE account_id=?", (aid,))
                DB.execute("DELETE FROM accounts WHERE id=?", (aid,))
                DB.commit()
            shutil.rmtree(os.path.join(MAIL_DIR, str(aid)), ignore_errors=True)
            self.send(200, {"ok": True})
        except Exception as e:
            traceback.print_exc()
            self.err(400, str(e))


if __name__ == "__main__":
    threading.Thread(target=startup_tasks, daemon=True).start()
    print(f"MessageLocal listening on :{PORT}, data in {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
