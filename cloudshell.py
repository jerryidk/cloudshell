#!/usr/bin/env python3
"""cloudshell - interactive CloudLab machine finder / reservation helper.

Works on Linux and macOS. Everything goes through your normal
CloudLab web login (the same calls the cloudlab.us pages make):

  * hardware specs   - the public sheet behind cloudlab.us/portal-hardware.php
  * availability     - the reservation forecast behind resinfo.php
  * reservations     - the reservation form / dashboard (resgroup.php)

Your username and password are asked once; the password and the web session
are then kept in the operating system's keychain (macOS Keychain, or the
Secret Service keyring on Linux) via `keyring`.

Optional: with the portal-api package and an API token (cloudlab.jwt), `find`
can fall back to the much slower Portal API search.

Run `cloudshell` and type `help`.
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import html
import io
import json
import math
import os
import re
import shlex
import shutil
import sys
import time
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
from dateutil import parser as dtparser
from dateutil import tz

try:                                   # optional: only for the API-search fallback
    from cloudlabclient.portal.client import PortalClient
    from cloudlabclient.portal.v1.models import (
        ResGroupNodeType,
        ResGroupNodeTypes,
        ResGroupSearch,
    )
    HAVE_API = True
except ImportError:
    HAVE_API = False

def _cache_dir() -> Path:
    if os.environ.get("CLOUDSHELL_HOME"):
        return Path(os.environ["CLOUDSHELL_HOME"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "cloudshell"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cloudshell"


HERE = Path(__file__).resolve().parent
DEFAULT_PORTAL_URL = "https://boss.emulab.net:43794"
SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
             "1Jv27hFLqkmwtV3lCPkQSFNqWPy_dcA87NRytxihP7t4/export?format=csv")
HW_PAGE_URL = "https://www.cloudlab.us/portal-hardware.php"
CACHE_DIR = _cache_dir()
SHEET_TTL = 6 * 3600          # hardware spec sheet changes rarely
SEARCH_TTL = 300              # reuse availability answers for 5 minutes
SEARCH_WORKERS = 12
LOCAL = tz.tzlocal()
UTC = timezone.utc


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR and text else text


def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")
def dim(t): return c(t, "2")
def bold(t): return c(t, "1")
def cyan(t): return c(t, "36")


def term_width() -> int:
    return shutil.get_terminal_size((160, 40)).columns


def fmt_dt(dt: Optional[datetime], with_year=False) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(LOCAL).strftime("%a %m-%d %H:%M" if not with_year
                                         else "%Y-%m-%d %a %H:%M %Z")


def fmt_hours(h: int) -> str:
    d, r = divmod(h, 24)
    return (f"{d}d" if d else "") + (f"{r}h" if r or not d else "")


def fmt_left(h: float) -> str:
    """Hours remaining -> '3d4h', '5h07m', 'expired'."""
    if h <= 0:
        return "expired"
    if h < 24:
        return f"{int(h)}h{int(h * 60) % 60:02d}m"
    return fmt_hours(int(h))


def _plain(v) -> str:
    """Server text (may contain HTML) -> one plain line."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(v or "")))
    return re.sub(r"\s+", " ", s).strip()


def resv_summary(d: dict) -> tuple[bool, bool, list[str], str]:
    """A resgroup Validate/Reserve reply -> (ok, needs_approval, problems, uuid)."""
    code, v = d.get("code"), d.get("value")
    if code:
        problems = []
        if isinstance(v, dict):
            for k, msg in v.items():
                if k == "clusters" and isinstance(msg, dict):
                    for cl in msg.values():
                        for f, err in ((cl or {}).get("errors") or {}).items():
                            problems.append(f"{(cl or {}).get('type')}: {f}: {err}")
                else:
                    problems.append(f"{k}: {_plain(msg)}")
        else:
            problems.append(_plain(v))
        return False, False, problems, ""
    v = v if isinstance(v, dict) else {}
    cr = v.get("cluster_results") or {}
    problems, needs_approval, hard = [], False, bool(cr.get("errors"))
    for res in (cr.get("clusters") or {}).values():
        res = res or {}
        for k in ("errors", "error", "conflict"):
            if res.get(k):
                problems.append(f"{res.get('count')}x {res.get('type')}: {k}: {_plain(res[k])}")
                hard = hard or k != "conflict"
        if not res.get("approved"):
            needs_approval = True
    if cr.get("errors"):
        problems.append(_plain(cr["errors"]))
    return not hard, needs_approval, problems, str(v.get("uuid") or "")


class Output:
    """A block of printed lines kept around so '/' can search it."""

    def __init__(self):
        self.header: list[str] = []      # always shown (plain text)
        self.rows: list[tuple[str, str]] = []   # (plain, colored)
        self.footer: list[str] = []

    def add_header(self, line: str):
        self.header.append(line)

    def add(self, plain: str, colored: Optional[str] = None):
        self.rows.append((plain, colored if colored is not None else plain))

    def print(self):
        for h in self.header:
            print(bold(h) if h and not h.startswith(" ") else h)
        for _, col in self.rows:
            print(col)
        for f in self.footer:
            print(dim(f))

    def search(self, pattern: str):
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        hits = [p for p, _ in self.rows if rx.search(p)]
        for h in self.header:
            print(bold(h))
        for p in hits:
            print(rx.sub(lambda m: c(m.group(0), "7;1"), p) if USE_COLOR else p)
        print(dim(f"-- {len(hits)} of {len(self.rows)} lines match /{pattern}/"
                  "   ('/' alone re-prints everything)"))


def render_table(cols: list[str], rows: list[list[str]],
                 last_col_color: Optional[list[Callable[[str], str]]] = None,
                 shrink: tuple[int, ...] = ()) -> tuple[str, list[tuple[str, str]]]:
    """Return (header line, [(plain, colored) rows]) fitted to the terminal."""
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(cols)]
    total = sum(widths) + 2 * (len(cols) - 1)
    over = total - term_width()
    for i in shrink:           # shrink wide free-text columns first
        if over <= 0:
            break
        cut = min(over, max(0, widths[i] - 12))
        widths[i] -= cut
        over -= cut

    def clip(s, w):
        return s if len(s) <= w else s[: max(1, w - 1)] + "…"

    header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(cols)).rstrip()
    out = []
    for n, r in enumerate(rows):
        cells = [clip(v, widths[i]).ljust(widths[i]) for i, v in enumerate(r)]
        plain = "  ".join(cells).rstrip()
        colored = plain
        if last_col_color:
            colored = "  ".join(cells[:-1] + [last_col_color[n](cells[-1].rstrip())])
        out.append((plain, colored))
    return header, out


# --------------------------------------------------------------------------
# hardware catalog (same data sources as cloudlab.us/portal-hardware.php)
# --------------------------------------------------------------------------
NET_COLS = [("200 Gbps", "200G"), ("100 Gbps", "100G"), ("40 Gbps", "40G"),
            ("25 Gbps", "25G"), ("10 Gbps", "10G"), ("1 Gbps", "1G"),
            ("100 Mbps", "100M")]


def _int(v, default=0):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


@dataclass
class HW:
    idx: int
    type: str
    cluster: str
    urn: str
    row: dict
    total: Optional[int] = None
    free: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.type}@{self.cluster}"

    @property
    def cpu(self) -> str:
        name = re.sub(r"\s*(\(R\)|\(TM\)|®|™)\s*", " ", self.row.get("Processor type", ""))
        name = re.sub(r"\b(Processor|CPU|Scalable)\b", "", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip()
        procs = _int(self.row.get("Procs"), 1)
        cores = procs * _int(self.row.get("Cores per proc"), 0)
        thr = _int(self.row.get("Total threads"), 0)
        ghz = self.row.get("Base speed (GHz)", "").strip()
        s = (f"{procs}x " if procs > 1 else "") + name
        bits = []
        if cores:
            bits.append(f"{cores}c/{thr}t" if thr and thr != cores else f"{cores}c")
        if ghz:
            bits.append(f"{ghz}GHz")
        return s + (f" ({' '.join(bits)})" if bits else "")

    @property
    def ram(self) -> str:
        v = self.row.get("DRAM (GB)", "").strip()
        return f"{v}G" if v else "-"

    @property
    def disk(self) -> str:
        gb = _int(self.row.get("Total disk (GB)"), 0)
        size = (f"{gb / 1000:.1f}T" if gb >= 1000 else f"{gb}G") if gb else "-"
        kinds = []
        for col, lbl in (("NVMe SSD", "nvme"), ("SATA SSDs", "ssd"), ("HDDs", "hdd")):
            v = self.row.get(col, "").strip()
            if v and v != "0":
                kinds.append(f"{v}{lbl}")
        return size + (f" {'+'.join(kinds)}" if kinds else "")

    @property
    def net(self) -> str:
        parts = []
        for col, lbl in NET_COLS:
            v = self.row.get(col, "").strip()
            if v and v != "0":
                parts.append(f"{v}x{lbl}")
        return " ".join(parts) or "-"

    @property
    def gpu(self) -> str:
        model = self.row.get("GPU model", "").strip()
        if not model:
            return ""
        n = self.row.get("Count", "").strip()
        return f"{n}x {model}" if n else model

    @property
    def avail_now(self) -> str:
        if self.free is None and self.total is None:
            return "?"
        return f"{self.free if self.free is not None else '?'}/{self.total or 0}"

    def text(self) -> str:
        """Everything, for -f filtering."""
        extra = " gpu" if self.gpu else ""
        return " ".join([self.key, self.cpu, self.ram, self.disk, self.net,
                         self.gpu, self.row.get("GPU arch", ""),
                         self.row.get("Hardware Notes", ""), extra])


def _fetch(url: str, timeout=30) -> str:
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "cloudshell/1.0"})
    r.raise_for_status()
    return r.text


def load_catalog(force=False, log=print) -> tuple[list[HW], dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sheet_cache = CACHE_DIR / "hardware.csv"
    text = None
    if not force and sheet_cache.exists() and time.time() - sheet_cache.stat().st_mtime < SHEET_TTL:
        text = sheet_cache.read_text()
    if text is None:
        try:
            text = _fetch(SHEET_URL)
            sheet_cache.write_text(text)
        except Exception as e:
            if sheet_cache.exists():
                log(yellow(f"! could not refresh hardware sheet ({e}); using cached copy"))
                text = sheet_cache.read_text()
            else:
                raise SystemExit(f"cannot download hardware spec sheet: {e}")

    # live free/total counts embedded in the hardware page
    amlist = {}
    try:
        page = _fetch(HW_PAGE_URL)
        m = re.search(r"<script[^>]*id='amlist-json'[^>]*>(.*?)</script>", page, re.S)
        if m:
            amlist = json.loads(html.unescape(m.group(1)))
    except Exception as e:
        log(yellow(f"! could not fetch live free/total counts ({e})"))

    lines = text.splitlines()
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[1:]))))  # line 0 = column groups
    out = []
    for r in rows:
        t, urn = r.get("Type name", "").strip(), r.get("URN", "").strip()
        if not t or not urn:
            continue
        hw = HW(len(out) + 1, t, r.get("Cluster", "").strip(), urn, r)
        info = (amlist.get(urn) or {}).get("typeinfo") or {}
        if isinstance(info, dict) and t in info:
            hw.total = _int(info[t].get("count"))
            hw.free = _int(info[t].get("free"))
        else:
            hw.total = _int(r.get("Number in service"), 0) or None
        out.append(hw)
    return out, amlist


# --------------------------------------------------------------------------
# portal API
# --------------------------------------------------------------------------
@dataclass
class Slot:
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class Avail:
    hw: HW
    status: str                  # asap | ok | maybe | later | error
    start: Optional[datetime] = None   # slot that would be reserved
    end: Optional[datetime] = None
    msg: str = ""

    RANK = {"ok": 0, "maybe": 1, "asap": 2, "later": 3, "error": 4}

    def label(self) -> str:
        extra = f" ({self.msg})" if self.msg else ""
        if self.status == "asap":
            soon = self.start and self.start <= datetime.now(UTC) + timedelta(minutes=2)
            return ("now" if soon else f"from {fmt_dt(self.start)}") + extra
        if self.status == "ok":
            return "✓ fits" + extra
        if self.status == "maybe":
            return "? likely (server verifies on reserve)"
        if self.status == "later":
            return f"✗ next {fmt_dt(self.start)}" + extra
        return f"- {self.msg}"

    def color(self, s: str) -> str:
        return {"ok": green, "maybe": yellow, "asap": cyan,
                "later": red, "error": dim}[self.status](s)


def _err_text(resp) -> str:
    try:
        msg = json.loads(resp.content).get("error") or ""
    except Exception:
        msg = resp.content.decode(errors="replace")
    if "No fit" in msg or "could not find a start" in msg:
        return "no fit"
    first = msg.strip().splitlines()[0] if msg.strip() else ""
    return f"HTTP {resp.status_code}" + (f": {first[:60]}" if first else "")


def _parse_ts(v) -> Optional[datetime]:
    if not v:
        return None
    d = dtparser.isoparse(v)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class Portal:
    def __init__(self, url: str, token: str):
        self.url, self.token = url, token
        # The server occasionally never answers a request; keep reads short
        # and retry (search/list are idempotent).
        self.api = PortalClient(url, token, detailed=True,
                                raise_on_unexpected_status=False,
                                timeout=httpx.Timeout(20.0, connect=10.0))
        self.api.get_httpx_client()      # create once, before threads use it
        self.write_api = PortalClient(url, token, detailed=True,
                                      raise_on_unexpected_status=False,
                                      timeout=httpx.Timeout(180.0, connect=15.0))
        self._cache: dict[tuple, tuple[float, Slot]] = {}

    def _retry(self, fn, attempts=2, **kw):
        last = None
        for _ in range(attempts):
            try:
                return fn(**kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
        raise last

    def search(self, hw: HW, count: int, hours: int, project: str) -> Slot:
        key = (hw.urn, hw.type, count, hours, project)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < SEARCH_TTL:
            return hit[1]
        body = ResGroupSearch(project=project, nodetypes=ResGroupNodeTypes(
            nodetypes=[ResGroupNodeType(nodetype=hw.type, count=count, urn=hw.urn)]))
        try:
            r = self._retry(self.api.search_resgroup, duration=hours, body=body)
        except httpx.TimeoutException:
            return Slot(error="timeout")          # not cached; retry later
        except httpx.TransportError as e:
            return Slot(error=f"net error: {type(e).__name__}")
        if r.status_code == 200:
            d = json.loads(r.content)
            slot = Slot(_parse_ts(d.get("start_at")), _parse_ts(d.get("expires_at")))
        else:
            slot = Slot(error=_err_text(r))
        self._cache[key] = (time.time(), slot)
        return slot

    def evaluate(self, hw: HW, count: int, hours: int, project: str,
                 start: Optional[datetime]) -> Avail:
        """Decide whether `count` x hw fits [start, start+hours] (or ASAP)."""
        first = self.search(hw, count, hours, project)
        if first.error:
            return Avail(hw, "error", msg=first.error)
        if start is None:
            return Avail(hw, "asap", first.start, first.start + timedelta(hours=hours))
        want_end = start + timedelta(hours=hours)
        if first.start > start:
            # earliest possible fit begins after the requested start
            return Avail(hw, "later", first.start, first.start + timedelta(hours=hours))
        # Earliest fit is at/before the requested start. Confirm the whole span
        # [earliest, requested end] is free by asking for one long block.
        span = math.ceil((want_end - first.start).total_seconds() / 3600)
        if span <= hours:
            return Avail(hw, "ok", start, want_end)
        second = self.search(hw, count, span, project)
        if not second.error and second.start <= first.start:
            return Avail(hw, "ok", start, want_end)
        return Avail(hw, "maybe", start, want_end,
                     msg=f"a {hours}h block fits from {fmt_dt(first.start)}")

    # thin wrappers -------------------------------------------------------
    def list_experiments(self):
        return self._retry(self.api.list_experiments)


# --------------------------------------------------------------------------
# secrets: OS keychain via `keyring` (macOS Keychain, Secret Service on
# Linux). Nothing secret is written to disk.
# --------------------------------------------------------------------------
KEYRING_SERVICE = "cloudshell:cloudlab.us"


class SecretStore:
    def __init__(self):
        self.kr = None
        self.backend = "none"
        try:
            import keyring
            from keyring.backends import fail
            kr = keyring.get_keyring()
            if not isinstance(kr, fail.Keyring) and getattr(kr, "priority", 1) > 0:
                self.kr, self.backend = keyring, type(kr).__name__
        except Exception:
            pass

    @property
    def ok(self) -> bool:
        return self.kr is not None

    def get(self, name: str) -> Optional[str]:
        if not self.kr:
            return None
        try:
            return self.kr.get_password(KEYRING_SERVICE, name)
        except Exception:
            return None

    def set(self, name: str, value: str) -> bool:
        if not self.kr:
            return False
        try:
            self.kr.set_password(KEYRING_SERVICE, name, value)
            return True
        except Exception:
            return False

    def delete(self, name: str):
        if not self.kr:
            return
        try:
            self.kr.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass


# --------------------------------------------------------------------------
# web portal client: the same server-ajax.php calls the cloudlab.us pages make
#
#   login        login/DoLogin                       (login.php)
#   forecast     reserve/ReservationInfo per cluster (resinfo.php)
#   list         resgroup/ListReservationGroups      (user-dashboard.php#resgroups)
#   check/create resgroup/Validate, resgroup/Reserve (resgroup.php)
#   delete       resgroup/Delete
#
# (The forecast is only reachable this way: the cluster RPC behind it,
# ReservationPrediction, accepts nothing but the portal's root credential.)
# --------------------------------------------------------------------------
WEB_BASE = "https://www.cloudlab.us"
WEB_UA = "Mozilla/5.0 cloudshell/2.0"
FORECAST_TTL = 120


class WebLoginError(Exception):
    pass


def php_form(prefix: str, obj, out: dict):
    """Encode nested dicts like jQuery does: a[b][c]=v."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            php_form(f"{prefix}[{k}]", v, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            php_form(f"{prefix}[{i}]", v, out)
    elif isinstance(obj, bool):
        out[prefix] = "1" if obj else "0"
    elif obj is not None:
        out[prefix] = str(obj)


def _looks_logged_out(d: dict) -> bool:
    v = d.get("value")
    return bool(d.get("code")) and isinstance(v, str) and re.search(
        r"log ?in|logged|session", v, re.I) is not None


class WebPortal:
    def __init__(self, uid: Optional[str] = None, base: str = WEB_BASE,
                 secrets: Optional[SecretStore] = None):
        self.base = base
        self.secrets = secrets or SecretStore()
        self.uid = uid or load_state().get("web_uid")
        self.client = httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0),
                                   follow_redirects=True, headers={"User-Agent": WEB_UA})
        self.clusters: dict[str, str] = {}       # urn -> nickname
        self._login_failed: Optional[str] = None
        self._cache: tuple[float, Optional[str], dict] = (0.0, None, {})
        self._restore_session()

    # ---- session persistence (in the keychain) -------------------------
    def _session_key(self) -> str:
        return f"session:{self.uid}"

    def _restore_session(self):
        if not self.uid:
            return
        raw = self.secrets.get(self._session_key())
        if not raw:
            return
        try:
            for ck in json.loads(raw):
                self.client.cookies.set(ck["name"], ck["value"],
                                        domain=ck.get("domain", ""), path=ck.get("path", "/"))
        except Exception:
            pass

    def _save_session(self):
        if not self.uid:
            return
        data = [{"name": ck.name, "value": ck.value, "domain": ck.domain, "path": ck.path}
                for ck in self.client.cookies.jar]
        self.secrets.set(self._session_key(), json.dumps(data))

    def logout(self, forget_password=False):
        self.client.cookies.clear()
        self._cache = (0.0, None, {})
        if self.uid:
            self.secrets.delete(self._session_key())
            if forget_password:
                self.secrets.delete(f"password:{self.uid}")

    # ---- login ---------------------------------------------------------
    def login(self, ask_user=False):
        """Log in once. Uses the keychain password if there is one, otherwise
        prompts. Never retries on its own: repeated failures can freeze the
        account or IP (DOLOGIN_STATUS_*FREEZE)."""
        if self._login_failed and not ask_user:
            raise WebLoginError(f"login already failed this session: {self._login_failed}"
                                " (type `login` to try again)")
        if ask_user or not self.uid:
            default = f" [{self.uid}]" if self.uid else ""
            uid = ask(f"CloudLab username{default}: ").strip() or self.uid
            if not uid:
                raise WebLoginError("no username given")
            self.uid = uid
        pw = None if ask_user else self.secrets.get(f"password:{self.uid}")
        from_store = pw is not None
        if not pw:
            if not sys.stdin.isatty():
                raise WebLoginError("not logged in; run cloudshell interactively and type `login`")
            pw = getpass.getpass(f"CloudLab password for {self.uid}: ")
            if not pw:
                raise WebLoginError("no password given")
        self.client.cookies.clear()
        self.client.get(self.base + "/login.php")
        r = self.client.post(self.base + "/server-ajax.php", data={
            "ajax_route": "login", "ajax_method": "DoLogin",
            "ajax_args[uid]": self.uid, "ajax_args[password]": pw})
        try:
            d = r.json()
        except Exception:
            d = {"code": -1, "value": f"HTTP {r.status_code}"}
        if d.get("code") != 0:
            self._login_failed = str(d.get("value"))[:160]
            if from_store:               # stored password is stale; don't reuse it
                self.secrets.delete(f"password:{self.uid}")
            raise WebLoginError(f"CloudLab login failed: {self._login_failed}")
        self._login_failed = None
        save_state({"web_uid": self.uid})
        if not from_store:
            if self.secrets.set(f"password:{self.uid}", pw):
                print(dim(f"password saved in your system keychain ({self.secrets.backend})"))
            else:
                print(yellow("! no system keychain available — you'll be asked for "
                             "the password again next time"))
        del pw
        self._save_session()

    # ---- raw ajax ------------------------------------------------------
    def _post(self, route: str, method: str, args: Optional[dict]) -> dict:
        data = {"ajax_route": route, "ajax_method": method}
        php_form("ajax_args", args or {"noargs": "noargs"}, data)
        r = self.client.post(self.base + "/server-ajax.php", data=data)
        try:
            return r.json()
        except Exception:
            if "login.php" in str(r.url):
                return {"code": -1, "value": "not logged in"}
            return {"code": -1, "value": f"HTTP {r.status_code}: unexpected reply"}

    def ajax(self, route: str, method: str, args: Optional[dict] = None) -> dict:
        """Call a server-ajax method; logs in (once) if the session is gone."""
        if not self.client.cookies:
            self.login()
        d = self._post(route, method, args)
        if _looks_logged_out(d):
            self.login()
            d = self._post(route, method, args)
        self._save_session()
        return d

    # ---- clusters & forecast -------------------------------------------
    def _resinfo_page(self) -> Optional[str]:
        r = self.client.get(self.base + "/resinfo.php?embedded=true")
        if "login.php" in str(r.url) or r.status_code != 200:
            return None
        return r.text

    def _load_clusters(self):
        page = self._resinfo_page() if self.client.cookies else None
        if page is None:                       # no/expired session
            self.login()
            page = self._resinfo_page()
            if page is None:
                raise WebLoginError("logged in, but resinfo.php still redirects to login")
        m = re.search(r"id='amlist-json'[^>]*>(.*?)</script>", page, re.S)
        if not m:
            raise WebLoginError("resinfo.php layout changed (no amlist-json)")
        am = json.loads(html.unescape(m.group(1)))
        self.clusters = {urn: a["nickname"] for urn, a in am.items()
                         if a.get("nickname") and not a.get("isME")}

    def forecasts(self, project: Optional[str], force=False) -> dict:
        """urn -> {"types": {type: [(epoch, free), ...]}, "error": str|None}"""
        ts, proj, data = self._cache
        if not force and data and proj == project and time.time() - ts < FORECAST_TTL:
            return data
        self._load_clusters()            # also (re)validates the session
        out: dict = {}
        with ThreadPoolExecutor(max_workers=max(1, len(self.clusters))) as ex:
            futs = {ex.submit(self._post, "reserve", "ReservationInfo", {"cluster": nick}): urn
                    for urn, nick in self.clusters.items()}
            for f, urn in futs.items():
                try:
                    d = f.result()
                except Exception as e:
                    out[urn] = {"types": {}, "error": f"{type(e).__name__}"}
                    continue
                if d.get("code"):
                    out[urn] = {"types": {}, "error": str(d.get("value"))[:80]}
                    continue
                v = d.get("value") or {}
                fc = dict(v.get("forecast") or {})
                # project view: nodes already reserved for you count as "held"
                pf = (v.get("pforecasts") or {}).get(project) if project else None
                if isinstance(pf, dict):
                    fc.update(pf)
                prune = set((v.get("prunelist") or {}).keys())
                out[urn] = {"types": {t: forecast_steps(s) for t, s in fc.items()
                                      if t not in prune and s},
                            "error": None}
        self._save_session()
        self._cache = (time.time(), project, out)
        return out

    # ---- reservations --------------------------------------------------
    def list_resgroups(self, project: str) -> dict:
        d = self.ajax("resgroup", "ListReservationGroups", {"project": project})
        if d.get("code"):
            raise ValueError(f"could not list reservations: {d.get('value')}")
        v = d.get("value")
        return v if isinstance(v, dict) else {}

    def reserve(self, project: str, reason: str, start: Optional[datetime],
                end: datetime, clusters: list[tuple[str, str, int]],
                validate_only: bool) -> dict:
        """clusters: [(cluster_urn, node_type, count)]. start=None means now."""
        pid, _, gid = project.partition("/")
        ff: dict = {"pid": pid, "reason": reason,
                    "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if start else "",
                    "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "clusters": {}}
        if gid:
            ff["gid"] = gid
        for urn, ntype, count in clusters:
            key = str(uuidlib.uuid4())
            ff["clusters"][key] = {"cluster": urn, "type": ntype,
                                   "count": count, "uuid": key}
        return self.ajax("resgroup", "Validate" if validate_only else "Reserve",
                         {"formfields": ff, "checkonly": 0})

    def delete_resgroup(self, uuid: str) -> dict:
        return self.ajax("resgroup", "Delete", {"uuid": uuid})

    # ---- experiments ---------------------------------------------------
    def list_experiments(self) -> list[dict]:
        """Experiments you created plus those in your projects (same call as
        user-dashboard.php#experiments). Each dict gets 'pid' and 'exp' added."""
        if not self.uid:
            self.login()
        d = self.ajax("user-dashboard", "ExperimentList", {"uid": self.uid})
        if d.get("code"):
            raise ValueError(f"could not list experiments: {d.get('value')}")
        v = d.get("value")
        seen: dict[str, dict] = {}
        for group in ("user_experiments", "project_experiments"):
            exps = v.get(group) if isinstance(v, dict) else None
            for key, b in (exps.items() if isinstance(exps, dict) else []):
                if not isinstance(b, dict) or not b.get("uuid"):
                    continue
                b = dict(b)
                b["pid"], _, b["exp"] = key.partition(":")       # key is "pid:name"
                seen.setdefault(b["uuid"], b)
        return list(seen.values())

    def experiment_nodes(self, uuid: str) -> list[dict]:
        """Nodes of one experiment, parsed from its manifest(s)."""
        d = self.ajax("status", "GetInstanceManifest", {"uuid": uuid})
        if d.get("code"):
            raise ValueError(f"could not get manifest: {d.get('value')}")
        v = d.get("value")
        nodes = []
        for agg_urn, xml in (v.items() if isinstance(v, dict) else []):
            nodes.extend(parse_manifest_nodes(xml, agg_urn, self.uid))
        return nodes

    def max_extension(self, uuid: str) -> dict:
        """How far the experiment could be extended right now (no change made)."""
        return self.ajax("status", "MaxExtension", {"uuid": uuid})

    def request_extension(self, uuid: str, hours: int, reason: str) -> dict:
        """Ask CloudLab to add `hours` to the experiment. Reply value on success:
        {expiration, granted (hours), message, warning}; granted may be less than
        asked, with the rest waiting for an administrator."""
        return self.ajax("status", "RequestExtension",
                         {"uuid": uuid, "howlong": str(int(hours)), "reason": reason})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _urn_tail(urn: str) -> str:
    """'urn:...+node+flex12' -> 'flex12'; '...+image+emulab-ops//UBUNTU24' -> 'UBUNTU24'."""
    tail = urn.rsplit("+", 1)[-1]
    return tail.rsplit("//", 1)[-1] or urn


def parse_manifest_nodes(xml: str, agg_urn: str, uid: Optional[str]) -> list[dict]:
    """Return [{client_id, host, type, image, image_urn, ssh, blockstores, cluster_urn}]."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for n in root:
        if _local(n.tag) != "node":
            continue
        info = {"client_id": n.get("client_id", "?"),
                "host": _urn_tail(n.get("component_id", "")) or "-",
                "cluster_urn": n.get("component_manager_id", agg_urn),
                "type": "-", "image": "-", "image_urn": "", "ssh": "-",
                "blockstores": []}
        logins = []
        for e in n.iter():
            t = _local(e.tag)
            if t == "hardware_type" and e.get("name"):
                info["type"] = e.get("name")
            elif t == "vnode" and e.get("hardware_type") and info["type"] == "-":
                info["type"] = e.get("hardware_type")
            elif t == "disk_image" and e.get("name"):
                info["image_urn"] = e.get("name")
                info["image"] = _urn_tail(e.get("name"))
            elif t == "blockstore" and e.get("mountpoint"):
                info["blockstores"].append(e.get("mountpoint"))
            elif t == "login" and e.get("hostname"):
                logins.append((e.get("username"), e.get("hostname"), e.get("port", "22")))
        mine = [lg for lg in logins if uid and lg[0] == uid]
        if mine or logins:
            user, host, port = (mine or logins)[0]
            user = user if mine else (uid or user)
            info["ssh"] = f"ssh {user}@{host}" + ("" if port == "22" else f" -p {port}")
        out.append(info)
    return out


def forecast_steps(series: list) -> list[tuple[int, int]]:
    """[{t, free, ...}, ...] -> sorted [(t, free)]; for duplicate timestamps
    the last entry wins (same rule as resinfo.js FixForecast)."""
    last: dict[int, int] = {}
    for e in series:
        try:
            last[int(e["t"])] = int(e.get("free") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(last.items())


def min_free(steps: list[tuple[int, int]], a: float, b: float) -> int:
    """Minimum free count over [a, b). A step's value holds until the next
    step; the last value holds forever; before the first step use the first."""
    cur = steps[0][1]
    for t, v in steps:
        if t <= a:
            cur = v
        else:
            break
    m = cur
    for t, v in steps:
        if a < t < b:
            m = min(m, v)
    return m


def earliest_fit(steps: list[tuple[int, int]], count: int, hours: int,
                 not_before: float) -> Optional[float]:
    """Earliest start >= not_before where `count` nodes stay free for `hours`."""
    span = hours * 3600
    for s in [not_before] + [t for t, _ in steps if t > not_before]:
        if min_free(steps, s, s + span) >= count:
            return s
    return None


# --------------------------------------------------------------------------
# argument parsing for the shell commands
# --------------------------------------------------------------------------
DUR_RX = re.compile(r"^(?:(\d+(?:\.\d+)?)([wdhm]))+$", re.I)
DUR_PART = re.compile(r"(\d+(?:\.\d+)?)([wdhm])", re.I)


def parse_duration(tok: str) -> Optional[int]:
    """'4h' '2d' '1d12h' '90m' '6' -> whole hours (rounded up)."""
    tok = tok.strip().lower()
    if re.fullmatch(r"\d+", tok):
        return int(tok)
    if not DUR_RX.match(tok):
        return None
    mult = {"w": 168, "d": 24, "h": 1, "m": 1 / 60}
    hours = sum(float(n) * mult[u] for n, u in DUR_PART.findall(tok))
    return max(1, math.ceil(hours - 1e-9))


def parse_start(text: str, now: datetime) -> Optional[datetime]:
    """Free-form local date/time -> aware datetime; None means ASAP."""
    t = text.strip().lower()
    if t in ("", "now", "asap"):
        return None
    base = now.astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\btomorrow\b", t):
        base += timedelta(days=1)
        t = re.sub(r"\btomorrow\b", "", t)
    t = re.sub(r"\btoday\b", "", t).strip()
    if not t:
        return base if base > now else now
    # parse twice with different default dates to tell whether a date was given
    alt = base + timedelta(days=3)
    d1 = dtparser.parse(t, default=base.replace(tzinfo=None))
    d2 = dtparser.parse(t, default=alt.replace(tzinfo=None))
    date_given = d1.date() == d2.date()
    if d1.tzinfo is None:
        d1 = d1.replace(tzinfo=LOCAL)
    if not date_given and d1 < now:
        d1 += timedelta(days=1)          # "3pm" already passed -> tomorrow 3pm
    return d1


def split_opts(tokens: list[str], spec: dict[str, str]) -> tuple[dict, list[str]]:
    """Tiny option parser: spec maps flag -> name; all flags take a value
    except names ending in '!'."""
    opts, rest, i = {}, [], 0
    while i < len(tokens):
        tk = tokens[i]
        if tk in spec:
            name = spec[tk]
            if name.endswith("!"):
                opts[name[:-1]] = True
            else:
                if i + 1 >= len(tokens):
                    raise ValueError(f"{tk} needs a value")
                opts[name] = tokens[i + 1]
                i += 1
        else:
            rest.append(tk)
        i += 1
    return opts, rest


# --------------------------------------------------------------------------
# the shell
# --------------------------------------------------------------------------
HELP = """\
Times are local ({tz}).  # = a row number from the last listing.

FIND & RESERVE MACHINES
  find [START] [LENGTH] [-n COUNT] [-f REGEX]
        Machine types free for LENGTH from START. No START = as soon as possible.
        LENGTH  4h, 2d, 1d12h, 90m (default 1h; `find 6` = 6h)
        START   'tomorrow 9am', '12/2 15:00', '2026-12-02 3pm', 'fri 10:00'
        -n      number of nodes (default 1)     -f  filter specs: gpu, wisc, 100G ...
        e.g.  find 8h      find tomorrow 9am 4h      find 12/2 13:00 2d -n 2 -f gpu
  hw [-f REGEX]            Hardware catalog with free/total counts right now
  show <#|type[@cluster]>  All specs of one entry
  reserve <#> [-r REASON] [-y]
                           Reserve that row's slot from the last `find`
                           (checked with CloudLab first, then asks to confirm)
  mine                     Your project's reservations
  cancel <id>              Delete a reservation (id prefix is fine)

EXPERIMENTS
  exps [-a] [-m] [-f REGEX]
                           Running experiments (yours + your projects'), soonest
                           to expire first. -a include ended, -m only yours
  exp <#|name>             Nodes of one experiment: host, type, image, ssh command

KEEP EXPERIMENTS ALIVE
  keep-alive <#|name> [--by 3d] [--lead 2d] [--until DATE] [-r REASON] [--no-reserve] [-n]
                           Add an experiment to the keep-alive list. Once less than
                           LEAD is left, ask CloudLab to extend it by BY (not past
                           --until). If not fully granted, reserve the same nodes
                           from its expiry instead. Waits 24h before re-asking.
                           -n = dry run
  keep-alive               Show the list, time left, and what was last done
  unalive <#|name>         Take an experiment off the list
  keep-alive tick [-n]     Check the list now
  keep-alive install       Check every 30 min in the background (systemd/launchd),
                           also after you quit cloudshell. `keep-alive uninstall`
                           stops it; `keep-alive log` shows what it did.
        ! Without `keep-alive install`, checks happen ONLY when you run
          `keep-alive tick` (or add an experiment), not while the shell sits idle
          or after you quit.

ACCOUNT & MISC
  project [NAME]           Show / set the project for searches and reservations
  login                    Enter CloudLab username + password (saved in the keychain)
  logout [--forget]        Forget the session (--forget: and the saved password)
  refresh                  Re-download specs, live counts and forecasts
  /TEXT                    Search the last output (regex); '/' alone shows it all
  help, quit

FIND RESULTS
  now / from ..             earliest time COUNT nodes stay free for LENGTH (ASAP mode)
  ✓ fits (N free)           free for your whole window (N = fewest free during it)
  ✗ next .. (only N free)   not enough at your start; next slot that fits
  ? likely                  (API fallback only) slot exists, window not fully confirmed
"""


class Shell:
    def __init__(self, portal: Optional[Portal], project: Optional[str],
                 web: WebPortal):
        self.portal = portal
        self.web = web
        self.project = project
        self.catalog: list[HW] = []
        self.amlist: dict = {}
        self.last_out: Optional[Output] = None
        self.last_find: dict = {}        # {"results": {key: Avail}, "count":..,...}
        self.last_resgroups: list = []
        self.last_exps: list = []

    # ---- setup ---------------------------------------------------------
    def load(self, force=False):
        tty = sys.stdout.isatty()
        if tty:
            print(dim("loading hardware catalog…"), end="\r", flush=True)
        self.catalog, self.amlist = load_catalog(force)
        if tty:
            print(" " * 40, end="\r")

    def cluster_name(self, urn: str) -> str:
        for hw in self.catalog:
            if hw.urn == urn:
                return hw.cluster
        return (self.amlist.get(urn) or {}).get("name") or urn

    def ensure_project(self) -> bool:
        if self.project:
            return True
        projects = []
        if self.portal:
            print(dim("detecting project from your experiments…"))
            try:
                r = self.portal.list_experiments()
                if r.status_code == 200:
                    projects = sorted({e.project for e in (r.parsed.experiments or [])
                                       if getattr(e, "project", None)})
            except Exception:
                pass
        if len(projects) == 1:
            self.set_project(projects[0])
            return True
        hint = f" (yours: {', '.join(projects)})" if projects else ""
        name = ask(f"CloudLab project to use{hint} (shown on your dashboard): ").strip()
        if not name:
            print(red("a project is required (use: project NAME)"))
            return False
        self.set_project(name)
        return True

    def set_project(self, name: str):
        self.project = name
        save_state({"project": name})
        print(f"project: {bold(name)}")

    # ---- dispatch ------------------------------------------------------
    def run(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if line.startswith("/"):
            if not self.last_out:
                print(dim("nothing to search yet"))
            elif line == "/":
                self.last_out.print()
            else:
                self.last_out.search(line[1:])
            return True
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        cmd, args = tokens[0].lower(), tokens[1:]
        handler = {
            "find": self.cmd_find, "f": self.cmd_find, "search": self.cmd_find,
            "hw": self.cmd_hw, "hardware": self.cmd_hw, "ls": self.cmd_hw,
            "show": self.cmd_show, "info": self.cmd_show,
            "reserve": self.cmd_reserve, "r": self.cmd_reserve,
            "mine": self.cmd_mine, "resgroups": self.cmd_mine, "list": self.cmd_mine,
            "cancel": self.cmd_cancel, "delete": self.cmd_cancel,
            "exps": self.cmd_exps, "experiments": self.cmd_exps,
            "exp": self.cmd_exp,
            "keep-alive": self.cmd_keepalive, "keepalive": self.cmd_keepalive,
            "alive": self.cmd_keepalive, "unalive": self.cmd_unalive,
            "project": self.cmd_project, "refresh": self.cmd_refresh,
            "login": self.cmd_login, "logout": self.cmd_logout,
            "help": self.cmd_help, "?": self.cmd_help,
        }.get(cmd)
        if cmd in ("quit", "exit", "q"):
            return False
        if handler is None:
            print(red(f"unknown command '{cmd}'") + dim("  (try: help)"))
            return True
        try:
            handler(args)
        except (ValueError, WebLoginError) as e:
            print(red(str(e)))
        except httpx.HTTPError as e:
            print(red(f"network error: {type(e).__name__}: {e}"))
        except KeyboardInterrupt:
            print(yellow("\ninterrupted"))
        return True

    # ---- commands ------------------------------------------------------
    def cmd_help(self, args):
        print(HELP.format(tz=datetime.now(LOCAL).strftime("%Z")))

    def cmd_refresh(self, args):
        self.load(force=True)
        if self.portal:
            self.portal._cache.clear()
        self.web._cache = (0.0, None, {})
        print(f"{len(self.catalog)} hardware entries loaded")

    def cmd_project(self, args):
        if args:
            self.set_project(args[0])
        else:
            print(f"project: {bold(self.project or '(not set)')}")

    def _filter(self, rx: Optional[str]) -> list[HW]:
        if not rx:
            return list(self.catalog)
        try:
            r = re.compile(rx, re.I)
        except re.error:
            r = re.compile(re.escape(rx), re.I)
        return [h for h in self.catalog if r.search(h.text())]

    def _hw_row(self, h: HW) -> list[str]:
        return [str(h.idx), h.type, h.cluster, h.avail_now, h.cpu, h.ram,
                h.disk, h.net, h.gpu]

    HW_COLS = ["#", "Type", "Cluster", "Free", "CPU", "RAM", "Disk", "Expt NICs", "GPU"]

    def cmd_hw(self, args):
        opts, rest = split_opts(args, {"-f": "filter"})
        flt = opts.get("filter") or (" ".join(rest) if rest else None)
        hws = self._filter(flt)
        header, rows = render_table(self.HW_COLS, [self._hw_row(h) for h in hws],
                                    shrink=(4, 7))
        out = Output()
        out.add_header(header)
        for p, col in rows:
            out.add(p, col)
        out.footer.append(f"{len(hws)} entries · Free = free/total right now · "
                          "use `find` for future time ranges")
        self.last_out = out
        out.print()

    def cmd_find(self, args):
        opts, rest = split_opts(args, {"-n": "count", "-f": "filter",
                                       "--count": "count", "--filter": "filter"})
        count = _int(opts.get("count", 1), 0)
        if count < 1:
            raise ValueError("-n must be a positive integer")
        # the LAST duration-looking token is the length ("find dec 2 4h");
        # everything else is the start time
        # A bare number counts as hours only when it is the sole argument.
        hours, start_toks = 1, list(rest)
        for i in range(len(rest) - 1, -1, -1):
            d = parse_duration(rest[i])
            if d is not None and (not rest[i].isdigit() or len(rest) == 1):
                hours = d
                del start_toks[i]
                break
        now = datetime.now(UTC)
        try:
            start = parse_start(" ".join(start_toks), now)
        except (ValueError, OverflowError):
            raise ValueError(f"could not understand start time '{' '.join(start_toks)}'")
        if start is not None and start < now - timedelta(minutes=5):
            raise ValueError(f"start {fmt_dt(start, True)} is in the past")
        if not self.ensure_project():
            return
        hws = self._filter(opts.get("filter"))
        if not hws:
            print(yellow("no hardware matches that filter"))
            return

        when = (f"starting {bold(fmt_dt(start, True))}" if start else bold("ASAP"))
        print(f"Checking {len(hws)} node types · {count} node(s) · "
              f"{bold(fmt_hours(hours))} · {when} · project {self.project}")
        results = None
        try:
            results = self._forecast_find(hws, count, hours, start)
        except WebLoginError as e:
            print(yellow(f"! web forecast unavailable: {e}"))
        except httpx.HTTPError as e:
            print(yellow(f"! web forecast unavailable: {type(e).__name__}"))
        if results is None:
            if not self.portal:
                return
            print(yellow("  falling back to the (slow) Portal API search…"))
            results = self._run_searches(hws, count, hours, start)
        if results is None:
            return
        results.sort(key=lambda a: (Avail.RANK[a.status],
                                    a.start or datetime.max.replace(tzinfo=UTC),
                                    a.hw.idx))
        self.last_find = {"results": {a.hw.key: a for a in results},
                          "count": count, "hours": hours, "start": start}

        rows = [self._hw_row(a.hw) + [a.label()] for a in results]
        header, rendered = render_table(self.HW_COLS + ["Availability"], rows,
                                        last_col_color=[a.color for a in results],
                                        shrink=(4, 7))
        out = Output()
        out.add_header(header)
        for p, col in rendered:
            out.add(p, col)
        n_ok = sum(a.status in ("ok", "asap") for a in results)
        if start:
            out.footer.append(f"{sum(a.status == 'ok' for a in results)} fit, "
                              f"{sum(a.status == 'maybe' for a in results)} likely, "
                              f"{sum(a.status == 'later' for a in results)} later, "
                              f"{sum(a.status == 'error' for a in results)} unavailable/error")
        else:
            out.footer.append(f"{n_ok} types have a slot · earliest first")
        out.footer.append("reserve <#> to book · show <#> for details · /TEXT to search")
        self.last_out = out
        out.print()

    def _forecast_find(self, hws, count, hours, start) -> list[Avail]:
        fc = self.web.forecasts(self.project)
        span = hours * 3600
        now = time.time()

        def dt(ts):
            return datetime.fromtimestamp(ts, UTC)

        results = []
        for h in hws:
            cl = fc.get(h.urn)
            if cl is None:
                results.append(Avail(h, "error", msg="no reservations at this cluster"))
                continue
            if cl["error"]:
                results.append(Avail(h, "error", msg=cl["error"][:50]))
                continue
            steps = cl["types"].get(h.type)
            if not steps:
                results.append(Avail(h, "error", msg="not reservable"))
                continue
            if start is None:
                s = earliest_fit(steps, count, hours, now)
                if s is None:
                    results.append(Avail(h, "error", msg=f"never {count} free in forecast"))
                else:
                    results.append(Avail(h, "asap", dt(s), dt(s + span),
                                         msg=f"{min_free(steps, s, s + span)} free"))
                continue
            a = start.timestamp()
            m = min_free(steps, a, a + span)
            if m >= count:
                results.append(Avail(h, "ok", start, start + timedelta(hours=hours),
                                     msg=f"{m} free"))
                continue
            s = earliest_fit(steps, count, hours, a)
            if s is None:
                results.append(Avail(h, "error", msg=f"only {m} free; no later fit"))
            else:
                results.append(Avail(h, "later", dt(s), dt(s + span), msg=f"only {m} free"))
        return results

    def cmd_login(self, args):
        """Ask for username + password, verify, store in the keychain."""
        self.web.login(ask_user=True)
        print(green(f"✓ logged into {WEB_BASE} as {self.web.uid}"))

    def cmd_logout(self, args):
        forget = "--forget" in args or "-f" in args
        self.web.logout(forget_password=forget)
        print("web session forgotten" + (" and saved password removed" if forget else
                                         dim("  (logout --forget also removes the saved password)")))

    def _run_searches(self, hws, count, hours, start) -> Optional[list[Avail]]:
        results: list[Avail] = []
        ex = ThreadPoolExecutor(max_workers=SEARCH_WORKERS)
        futs = {ex.submit(self.portal.evaluate, h, count, hours, self.project, start): h
                for h in hws}
        t0 = time.time()
        try:
            for i, f in enumerate(as_completed(futs), 1):
                h = futs[f]
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append(Avail(h, "error", msg=f"error: {type(e).__name__}"))
                if sys.stdout.isatty():
                    print(dim(f"  {i}/{len(hws)} checked ({time.time() - t0:.0f}s)"),
                          end="\r", flush=True)
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            print(yellow("\nsearch cancelled"))
            return None
        ex.shutdown(wait=False)
        if sys.stdout.isatty():
            print(" " * 40, end="\r")
        return results

    def _lookup(self, ref: str) -> HW:
        if ref.isdigit():
            for h in self.catalog:
                if h.idx == int(ref):
                    return h
            raise ValueError(f"no entry #{ref}")
        t, _, cl = ref.partition("@")
        cands = [h for h in self.catalog if h.type.lower() == t.lower()
                 and (not cl or cl.lower() in h.cluster.lower())]
        if not cands:
            raise ValueError(f"unknown node type '{ref}'")
        if len(cands) > 1:
            raise ValueError(f"'{ref}' is ambiguous: " + ", ".join(
                f"#{h.idx} {h.key}" for h in cands))
        return cands[0]

    def cmd_show(self, args):
        if not args:
            raise ValueError("usage: show <#|type[@cluster]>")
        h = self._lookup(args[0])
        out = Output()
        out.add_header(f"#{h.idx}  {h.type} @ {h.cluster}")
        skip = {"Type name", "Cluster", "Processor Link", "GPU Link"}
        out.add(f"  {'Free now / total':26} {h.avail_now}")
        for k, v in h.row.items():
            v = (v or "").strip().strip(",")
            if k in skip or not v:
                continue
            out.add(f"  {k:26} {v}")
        for k in ("Processor Link", "GPU Link"):
            if h.row.get(k, "").strip():
                out.add(f"  {k:26} {h.row[k].strip()}")
        url = (self.amlist.get(h.urn) or {}).get("url")
        if url:
            out.add(f"  {'Cluster page':26} {url}/portal/show-hardware.php?type={h.type}")
        a = self.last_find.get("results", {}).get(h.key)
        if a:
            extra = f"  ({a.msg})" if a.msg and a.status != "error" else ""
            out.add(f"  {'Last find':26} {a.label()}{extra}")
            if a.start:
                out.add(f"  {'Slot':26} {fmt_dt(a.start, True)} -> {fmt_dt(a.end, True)}")
        self.last_out = out
        out.print()

    def cmd_reserve(self, args):
        opts, rest = split_opts(args, {"-r": "reason", "--reason": "reason", "-y": "yes!"})
        if not rest:
            raise ValueError("usage: reserve <#|type[@cluster]> [-r REASON] [-y]")
        h = self._lookup(rest[0])
        lf = self.last_find
        a = lf.get("results", {}).get(h.key)
        if a is None:
            raise ValueError(f"{h.key} wasn't in the last `find`; run find first "
                             f"(e.g. find 4h -f '^{h.type}@')")
        if a.status == "error" or not a.start:
            raise ValueError(f"{h.key} has no reservable slot ({a.msg})")
        if not self.ensure_project():
            return
        count, hours = lf["count"], lf["hours"]
        starts_now = a.start <= datetime.now(UTC) + timedelta(minutes=5)
        print(bold("Reservation request"))
        print(f"  project : {self.project}")
        print(f"  nodes   : {count} x {h.type} @ {h.cluster}   ({h.cpu}, {h.ram} RAM"
              + (f", {h.gpu}" if h.gpu else "") + ")")
        print(f"  start   : {'now' if starts_now else fmt_dt(a.start, True)}")
        print(f"  end     : {fmt_dt(a.end, True)}   ({fmt_hours(hours)})")
        if a.status == "later":
            print(yellow("  note    : this is the NEXT available slot, not the start you asked for"))
        reason = opts.get("reason") or ask("Reason (why you need these nodes; admins may read it): ").strip()
        if not reason:
            print(red("a reason is required — aborted"))
            return
        req = dict(project=self.project, reason=reason,
                   start=None if starts_now else a.start, end=a.end,
                   clusters=[(h.urn, h.type, count)])

        # 1) dry run: same check the web form does before submitting
        print(dim("checking with CloudLab…"))
        d = self.web.reserve(validate_only=True, **req)
        if not self._show_reserve_result(d, validate=True):
            return
        if not opts.get("yes") and ask("Submit reservation? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return
        # 2) the real thing
        print(dim("submitting…"))
        d = self.web.reserve(validate_only=False, **req)
        if self._show_reserve_result(d, validate=False):
            self.web._cache = (0.0, None, {})
            self.last_resgroups = []

    def _show_reserve_result(self, d: dict, validate: bool) -> bool:
        """Print a Validate/Reserve reply; True if it went through."""
        code, v = d.get("code"), d.get("value")
        if code:
            print(red("✗ " + ("check failed" if validate else "reservation failed")))
            if isinstance(v, dict):              # form errors: {field: msg, clusters: {...}}
                for k, msg in v.items():
                    if k == "clusters" and isinstance(msg, dict):
                        for cl in msg.values():
                            for f, e in ((cl or {}).get("errors") or {}).items():
                                print(f"  {cl.get('type')}: {f}: {e}")
                    else:
                        print(f"  {k}: {msg}")
            else:
                print("  " + str(v).strip())
            return False
        v = v if isinstance(v, dict) else {}
        cr = v.get("cluster_results") or {}
        problems, needs_approval = [], 0
        for res in (cr.get("clusters") or {}).values():
            res = res or {}
            label = f"{res.get('count')} x {res.get('type')}"
            for k in ("errors", "error", "conflict"):
                if res.get(k):
                    problems.append(f"{label}: {k}: {res[k]}")
            if not res.get("approved"):
                needs_approval += 1
        if cr.get("errors"):
            problems.append(str(cr["errors"]))
        if problems and (cr.get("errors") or any("error" in p for p in problems)):
            print(red("✗ CloudLab can't accommodate this request:"))
            for p in problems:
                print("  " + p)
            return False
        if validate:
            if needs_approval:
                print(yellow("! this request can't be auto-approved; it will wait for an "
                             "administrator" + (f" (conflicts: {'; '.join(problems)})" if problems else "")))
                if v.get("nbd"):
                    print(yellow(f"  admins reply by about {fmt_dt(_parse_ts(v['nbd']), True)}"))
            else:
                print(green("✓ fits — this will be approved automatically"))
            return True
        rid = v.get("uuid") or "?"
        state = "pending admin approval" if needs_approval else "approved"
        print(green(f"✓ reservation created ({state}): {rid}"))
        return True

    def cmd_mine(self, args):
        if not self.ensure_project():
            return
        groups = list(self.web.list_resgroups(self.project).values())
        self.last_resgroups = groups
        rows = []
        for g in sorted(groups, key=lambda g: g.get("start") or ""):
            nodes, using = [], []
            for cl in (g.get("clusters") or {}).values():
                nodes.append(f"{cl.get('count')}x {cl.get('type')}@{cl.get('cluster_id')}")
                using.append(f"{cl.get('using', 0)}/{cl.get('count')}")
            rows.append([(g.get("uuid") or "")[:8], fmt_dt(_parse_ts(g.get("start"))),
                         fmt_dt(_parse_ts(g.get("end"))), ", ".join(nodes) or "-",
                         g.get("status") or "-", ", ".join(using), g.get("uid") or "",
                         (g.get("notes") or "").replace("\n", " ")])
        header, rendered = render_table(
            ["ID", "Start", "End", "Nodes", "Status", "In use", "By", "Reason"],
            rows, shrink=(7, 3))
        out = Output()
        out.add_header(header)
        for p, col in rendered:
            out.add(p, col)
        out.footer.append(f"{len(groups)} reservation group(s) in project {self.project}"
                          " · cancel <ID> to delete")
        self.last_out = out
        out.print()

    def cmd_cancel(self, args):
        opts, rest = split_opts(args, {"-y": "yes!"})
        if not rest:
            raise ValueError("usage: cancel <reservation-id or prefix>")
        pref = rest[0].lower()
        if not self.last_resgroups:
            self.cmd_mine([])
        matches = [g for g in self.last_resgroups
                   if (g.get("uuid") or "").lower().startswith(pref)]
        if len(matches) != 1:
            raise ValueError(f"'{pref}' matches {len(matches)} reservation groups (run `mine`)")
        g = matches[0]
        nodes = ", ".join(f"{cl.get('count')}x {cl.get('type')}@{cl.get('cluster_id')}"
                          for cl in (g.get("clusters") or {}).values())
        print(f"Delete reservation {g['uuid']}  ({nodes}, "
              f"{fmt_dt(_parse_ts(g.get('start')))} -> {fmt_dt(_parse_ts(g.get('end')))}, "
              f"by {g.get('uid')})")
        if not opts.get("yes") and ask("Really delete? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return
        d = self.web.delete_resgroup(g["uuid"])
        if d.get("code"):
            print(red(f"✗ delete failed: {d.get('value')}"))
            return
        print(green("✓ deleted"))
        self.last_resgroups = [x for x in self.last_resgroups if x is not g]
        self.web._cache = (0.0, None, {})

    # ---- experiments ---------------------------------------------------
    ENDED = {"expired", "canceled", "terminated", "terminating", "quarantined"}

    def cmd_exps(self, args, quiet=False):
        opts, _ = split_opts(args, {"-a": "all!", "--all": "all!", "-m": "mine!",
                                    "--mine": "mine!", "-f": "filter"})
        exps = self.web.list_experiments()
        total = len(exps)
        if not opts.get("all"):
            exps = [e for e in exps if (e.get("status") or "").lower() not in self.ENDED]
        if opts.get("mine"):
            exps = [e for e in exps if e.get("creator") == self.web.uid]
        now = datetime.now(UTC)

        def left(e) -> Optional[float]:
            t = _parse_ts(e.get("expires"))
            return (t - now).total_seconds() / 3600 if t else None

        exps.sort(key=lambda e: (left(e) is None, left(e) or 0))
        rows, colors = [], []
        for e in exps:
            h = left(e)
            if h is None:
                exp_s, col = "-", (lambda s: s)
            elif h <= 0:
                exp_s, col = "expired", red
            else:
                exp_s = f"{fmt_hours(int(h))} {int(h * 60) % 60:02d}m" if h < 24 else fmt_hours(int(h))
                exp_s += f"  ({fmt_dt(_parse_ts(e.get('expires')))})"
                col = red if h < 1 else yellow if h < 6 else green
            rows.append([e.get("exp") or "?", e.get("pid") or "", e.get("cluster") or "-",
                         str(e.get("pcount") or 0), e.get("status") or "-",
                         e.get("creator") or "", e.get("profile_name") or "", exp_s])
            colors.append(col)
        if opts.get("filter"):
            try:
                rx = re.compile(opts["filter"], re.I)
            except re.error:
                rx = re.compile(re.escape(opts["filter"]), re.I)
            keep = [i for i, r in enumerate(rows) if rx.search(" ".join(r))]
            rows, colors, exps = ([rows[i] for i in keep], [colors[i] for i in keep],
                                  [exps[i] for i in keep])
        self.last_exps = exps
        rows = [[str(i + 1)] + r for i, r in enumerate(rows)]
        header, rendered = render_table(
            ["#", "Experiment", "Project", "Cluster", "Nodes", "Status", "By",
             "Profile", "Expires in"], rows, last_col_color=colors, shrink=(7, 1))
        out = Output()
        out.add_header(header)
        for p, colored in rendered:
            out.add(p, colored)
        shown = "" if opts.get("all") else " running"
        out.footer.append(f"{len(exps)}{shown} experiment(s) of {total} visible to "
                          f"{self.web.uid} · exp <#> for nodes"
                          + ("" if opts.get("all") else " · -a to include ended ones"))
        if not quiet:
            self.last_out = out
            out.print()

    def _lookup_exp(self, ref: str) -> dict:
        if not self.last_exps:
            self.cmd_exps([], quiet=True)
        if ref.isdigit():
            i = int(ref) - 1
            if not 0 <= i < len(self.last_exps):
                raise ValueError(f"no row {ref} in the last `exps` output")
            return self.last_exps[i]
        r = ref.lower()
        exact = [e for e in self.last_exps if (e.get("exp") or "").lower() == r]
        matches = exact or [e for e in self.last_exps
                            if (e.get("exp") or "").lower().startswith(r)
                            or (e.get("uuid") or "").lower().startswith(r)]
        if len(matches) != 1:
            raise ValueError(f"'{ref}' matches {len(matches)} experiments (run `exps`)")
        return matches[0]

    def cmd_exp(self, args):
        if not args:
            raise ValueError("usage: exp <#|name|uuid-prefix>")
        e = self._lookup_exp(args[0])
        nodes = self.web.experiment_nodes(e["uuid"])
        print(bold(f"{e.get('pid')}/{e.get('exp')}") +
              f"  {e.get('status')}  by {e.get('creator')}  profile {e.get('profile_name')}")
        print(dim(f"  uuid {e['uuid']} · started {fmt_dt(_parse_ts(e.get('started')))}"
                  f" · expires {fmt_dt(_parse_ts(e.get('expires')))}"))
        print(dim(f"  {self.web.base}/status.php?uuid={e['uuid']}"))
        rows = [[n["client_id"], n["host"], n["type"], self.cluster_name(n["cluster_urn"]),
                 n["image"], ",".join(n["blockstores"]) or "-", n["ssh"]] for n in nodes]
        header, rendered = render_table(
            ["Node", "Host", "Type", "Cluster", "Image", "Blockstores", "SSH"],
            rows, shrink=(4,))
        out = Output()
        out.add_header(header)
        for p, colored in rendered:
            out.add(p, colored)
        out.footer.append(f"{len(nodes)} node(s)")
        self.last_out = out
        out.print()

    # ---- keep-alive ----------------------------------------------------
    # CloudLab's extension rules (apt/manage_instance.in, DoExtend): you can't ask
    # while more than 7 days are left; requests made within 24h of expiry, 5 times
    # in a row, count as "gaming" and get extensions refused. So: ask early
    # (LEAD before expiry), and after any request that isn't fully granted, wait
    # KA_BACKOFF_H before asking again (an admin may be reviewing it).
    KA_BY_H = 72
    KA_LEAD_H = 48
    KA_MAX_LEAD_H = 6 * 24
    KA_BACKOFF_H = 24
    KA_REASON = "Ongoing work; kept alive by cloudshell for {uid}."
    KA_LOG = CACHE_DIR / "keepalive.log"

    @staticmethod
    def _ka_load() -> dict:
        a = load_state().get("alive")
        return a if isinstance(a, dict) else {}

    @staticmethod
    def _ka_write(set_=None, remove=()):
        """Update only the given entries, so a concurrent add/unalive isn't undone.
        set_ entries whose key is no longer present are dropped (unalived meanwhile)
        unless they are marked "_new"."""
        cur = Shell._ka_load()
        for k, v in (set_ or {}).items():
            if k in cur or v.pop("_new", False):
                cur[k] = v
        for k in remove:
            cur.pop(k, None)
        save_state({"alive": cur})

    def _ka_note(self, name: str, ent: dict, msg: str, color=None, dry=False):
        stamp = datetime.now(LOCAL).strftime("%m-%d %H:%M")
        if not dry:
            ent["last"] = f"{stamp} {msg}"
            if ent.get("last_msg") == msg:       # unchanged since last tick: don't spam the log
                print(color(f"{name}: {msg}") if color else f"{name}: {msg}")
                return
            ent["last_msg"] = msg
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with open(self.KA_LOG, "a") as f:
                    f.write(f"{datetime.now(LOCAL).isoformat(timespec='seconds')} {name}: {msg}\n")
            except OSError:
                pass
        line = f"{name}: {msg}"
        print((dim("[dry run] ") if dry else "") + (color(line) if color else line))

    def cmd_keepalive(self, args):
        sub = args[0].lower() if args else ""
        if not args:
            return self._ka_list()
        if sub == "tick":
            opts, _ = split_opts(args[1:], {"-n": "dry!", "--dry-run": "dry!"})
            return self.keepalive_tick(dry=bool(opts.get("dry")))
        if sub == "install":
            return self._ka_install()
        if sub == "uninstall":
            return self._ka_uninstall()
        if sub == "log":
            try:
                lines = self.KA_LOG.read_text().splitlines()[-30:]
            except OSError:
                lines = []
            print("\n".join(lines) if lines else dim("(no keep-alive activity yet)"))
            return
        opts, rest = split_opts(args, {
            "--by": "by", "--lead": "lead", "--until": "until", "-r": "reason",
            "--reason": "reason", "--no-reserve": "noreserve!", "-n": "dry!",
            "--dry-run": "dry!"})
        if not rest:
            raise ValueError("usage: keep-alive <#|name> [--by 3d] [--lead 2d] "
                             "[--until DATE] [-r REASON] [--no-reserve] [-n]")
        e = self._lookup_exp(rest[0])
        by = parse_duration(opts.get("by", f"{self.KA_BY_H}h"))
        lead = parse_duration(opts.get("lead", f"{self.KA_LEAD_H}h"))
        if not by:
            raise ValueError(f"bad --by '{opts.get('by')}' (e.g. 1d, 72h)")
        if not lead or lead > self.KA_MAX_LEAD_H:
            raise ValueError(f"--lead must be between 1h and {fmt_hours(self.KA_MAX_LEAD_H)} "
                             "(CloudLab refuses requests made more than 7 days ahead)")
        now = datetime.now(UTC)
        until = None
        if opts.get("until"):
            until = parse_start(opts["until"], now)
            if until is None or until <= now:
                raise ValueError(f"bad --until '{opts['until']}' (must be a future date)")
        uuid = e["uuid"]
        old = self._ka_load().get(uuid, {})
        ent = dict(old)
        ent.update({"exp": e.get("exp"), "pid": e.get("pid"), "by_h": by, "lead_h": lead,
                    "until": until.astimezone(UTC).isoformat() if until else None,
                    "reason": opts.get("reason") or old.get("reason")
                    or self.KA_REASON.format(uid=self.web.uid),
                    "reserve": not opts.get("noreserve"),
                    "added": old.get("added") or now.isoformat()})
        ent.pop("stopped", None)
        name = f"{ent['pid']}/{ent['exp']}"
        policy = (f"extend by {fmt_hours(by)} when < {fmt_hours(lead)} left"
                  + (f", until {fmt_dt(until)}" if until else "")
                  + ("" if ent["reserve"] else ", no reservation fallback"))
        if e.get("creator") and e.get("creator") != self.web.uid:
            print(yellow(f"! {name} was created by {e.get('creator')}; extending it needs "
                         "project permissions and they'll get CloudLab's emails"))
        if opts.get("dry"):
            print(f"would keep {bold(name)} alive: {policy}")
            self.keepalive_tick(dry=True, entries={uuid: ent})
            return
        ent["_new"] = True
        self._ka_write({uuid: ent})
        print(green(f"✓ keeping {name} alive: {policy}"))
        self.keepalive_tick(only={uuid})
        if not self._ka_timer_installed():
            print(dim("  note: this only runs when `keep-alive tick` runs; "
                      "`keep-alive install` runs it every 30 min in the background"))

    def cmd_unalive(self, args):
        if not args:
            raise ValueError("usage: unalive <#|name|uuid-prefix>   (see `keep-alive`)")
        alive = self._ka_load()
        items = sorted(alive.items(), key=lambda kv: kv[1].get("expires") or "")
        ref = args[0].lower()
        if ref.isdigit() and 0 < int(ref) <= len(items):
            matches = [items[int(ref) - 1]]
        else:
            exact = [kv for kv in items if (kv[1].get("exp") or "").lower() == ref]
            matches = exact or [kv for kv in items
                                if (kv[1].get("exp") or "").lower().startswith(ref)
                                or kv[0].lower().startswith(ref)]
        if len(matches) != 1:
            raise ValueError(f"'{args[0]}' matches {len(matches)} kept-alive experiments "
                             "(run `keep-alive`)")
        uuid, ent = matches[0]
        self._ka_write(remove=[uuid])
        msg = f"✓ {ent.get('pid')}/{ent.get('exp')} will no longer be extended"
        if ent.get("resgroup"):
            msg += (f"; its reservation {ent['resgroup'][:8]} is kept "
                    f"(`cancel {ent['resgroup'][:8]}` to free it)")
        print(green(msg))

    def _ka_list(self):
        alive = self._ka_load()
        if not alive:
            print(dim("nothing is kept alive (keep-alive <#> after `exps` to add one)"))
            return
        now = datetime.now(UTC)
        rows = []
        for uuid, ent in sorted(alive.items(), key=lambda kv: kv[1].get("expires") or ""):
            exp_t = _parse_ts(ent.get("expires"))
            left = fmt_left((exp_t - now).total_seconds() / 3600) if exp_t else "?"
            policy = f"+{fmt_hours(ent.get('by_h', 0))} at <{fmt_hours(ent.get('lead_h', 0))}"
            if ent.get("until"):
                policy += f" until {fmt_dt(_parse_ts(ent['until']))}"
            resv = (ent.get("resgroup") or "")[:8] or ("-" if ent.get("reserve") else "off")
            rows.append([str(len(rows) + 1), f"{ent.get('pid')}/{ent.get('exp')}", left,
                         policy, resv, (ent.get("last") or "not checked yet")])
        header, rendered = render_table(
            ["#", "Experiment", "Expires in", "Policy", "Resv", "Last check"], rows,
            shrink=(5, 3))
        out = Output()
        out.add_header(header)
        for p, colored in rendered:
            out.add(p, colored)
        timer = "background timer on" if self._ka_timer_installed() else \
            "no background timer: `keep-alive install`"
        out.footer.append(f"{len(rows)} kept alive · {timer} · `keep-alive tick` checks now"
                          " · `unalive <#>` stops")
        self.last_out = out
        out.print()

    def keepalive_tick(self, dry=False, only=None, entries=None):
        """One pass over the kept-alive experiments (run by the background timer)."""
        alive = entries if entries is not None else self._ka_load()
        if only:
            alive = {k: v for k, v in alive.items() if k in only}
        if not alive:
            print(dim("nothing is kept alive"))
            return
        exps = {e["uuid"]: e for e in self.web.list_experiments()}
        now = datetime.now(UTC)
        for uuid, ent in alive.items():
            name = f"{ent.get('pid')}/{ent.get('exp')}"
            try:
                self._ka_check(uuid, ent, exps.get(uuid), now, dry, name)
            except (ValueError, WebLoginError, httpx.HTTPError) as ex:
                self._ka_note(name, ent, f"error: {ex}", red, dry)
        if not dry and entries is None:
            self._ka_write(alive)

    def _ka_check(self, uuid: str, ent: dict, e: Optional[dict], now: datetime,
                  dry: bool, name: str):
        def note(msg, color=None):
            self._ka_note(name, ent, msg, color, dry)

        status = ((e or {}).get("status") or "").lower()
        if not e or status in self.ENDED:
            ent["state"] = "ended"
            note(f"experiment is {status or 'gone'}; nothing to extend (`unalive` it)", red)
            return
        exp_t = _parse_ts(e.get("expires"))
        if not exp_t:
            note("CloudLab shows no expiration time; skipped", yellow)
            return
        prev_exp = _parse_ts(ent.get("expires"))
        ent["expires"] = e["expires"]
        left_h = (exp_t - now).total_seconds() / 3600
        until = _parse_ts(ent.get("until"))
        if until and exp_t >= until - timedelta(minutes=30):
            note(f"already runs until {fmt_dt(exp_t)} (--until {fmt_dt(until)}); done")
            return
        if ent.get("stopped"):
            note(f"auto-extend stopped: {ent['stopped']} (re-run keep-alive to resume)", yellow)
            return
        nxt = _parse_ts(ent.get("next_try"))
        if nxt and prev_exp and exp_t > prev_exp + timedelta(minutes=5):
            ent.pop("next_try", None)          # expiry moved: an admin granted it
            nxt = None
            note(f"expiry moved to {fmt_dt(exp_t)} (granted)", green)
        if left_h > ent.get("lead_h", self.KA_LEAD_H):
            ask_at = exp_t - timedelta(hours=ent.get("lead_h", self.KA_LEAD_H))
            note(f"{fmt_left(left_h)} left; will ask for more at {fmt_dt(ask_at)}")
            return
        if nxt and now < nxt:
            note(f"{fmt_left(left_h)} left; last request wasn't fully granted, "
                 f"asking again after {fmt_dt(nxt)}", yellow)
            return

        target = exp_t + timedelta(hours=ent.get("by_h", self.KA_BY_H))
        if until:
            target = min(target, until)
        hours = math.ceil((target - exp_t).total_seconds() / 3600)
        if hours < 1:
            return
        if dry:
            note(f"{fmt_left(left_h)} left; would ask CloudLab to extend by {hours}h "
                 f"(to {fmt_dt(target)})"
                 + ("; if not granted, would reserve the same nodes until then"
                    if ent.get("reserve") else ""))
            return

        d = self.web.request_extension(uuid, hours, ent.get("reason") or "")
        v = d.get("value")
        granted, new_exp = 0, exp_t
        if d.get("code") == 0 and isinstance(v, dict):
            granted = _int(v.get("granted"))
            new_exp = _parse_ts(v.get("expiration")) or exp_t
            ent["expires"] = v.get("expiration") or e["expires"]
            msg = _plain(v.get("message"))
            if granted:
                note(f"extended by {granted}h; now expires {fmt_dt(new_exp)}", green)
            if granted < hours:
                note(f"{hours - granted}h more is waiting for an administrator"
                     + (f" ({msg})" if msg else ""), yellow)
            if v.get("warning"):
                ent["stopped"] = _plain(v["warning"])[:200]
                note("CloudLab warns: " + ent["stopped"], yellow)
        else:
            note(f"extension not granted: {_plain(v)[:200]}", yellow)
        if granted < hours:
            ent["next_try"] = (now + timedelta(hours=self.KA_BACKOFF_H)).isoformat()
            if ent.get("reserve"):
                self._ka_reserve(uuid, ent, new_exp, target, now, note)
        else:
            ent.pop("next_try", None)

    def _ka_reserve(self, uuid, ent, start, end, now, note):
        """Hold the experiment's node types for [start, end) via a reservation."""
        resv_end = _parse_ts(ent.get("resgroup_end"))
        if resv_end and resv_end >= end - timedelta(minutes=30):
            return                                   # already covered
        if resv_end and resv_end > start:
            start = resv_end
        if end - start < timedelta(hours=1):
            return
        counts: dict[tuple[str, str], int] = {}
        for n in self.web.experiment_nodes(uuid):
            if n["type"] != "-":
                key = (n["cluster_urn"], n["type"])
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            note("can't tell the node types; no reservation made", red)
            return
        clusters = [(u, t, c) for (u, t), c in counts.items()]
        desc = ", ".join(f"{c}x {t}@{self.cluster_name(u)}" for u, t, c in clusters)
        project = ent.get("pid") or self.project
        if self.project and self.project.split("/")[0] == project:
            project = self.project
        when = f"{fmt_dt(start)} -> {fmt_dt(end)}"
        st = start if start - now > timedelta(minutes=5) else None
        ok, approval, problems, _ = resv_summary(
            self.web.reserve(project, ent.get("reason") or "", st, end, clusters, True))
        if not ok:
            note(f"can't reserve {desc} {when}: {'; '.join(problems)[:200]}", red)
            return
        ok, approval, problems, rid = resv_summary(
            self.web.reserve(project, ent.get("reason") or "", st, end, clusters, False))
        if not ok:
            note(f"reserving {desc} {when} failed: {'; '.join(problems)[:200]}", red)
            return
        ent["resgroup"] = rid
        ent["resgroup_end"] = end.astimezone(UTC).isoformat()
        note(f"reserved {desc} {when} ("
             + ("waiting for admin approval" if approval else "approved")
             + f"), id {rid[:8]}", green)

    # background timer: systemd user timer (Linux) or launchd agent (macOS)
    KA_UNIT = "cloudshell-keepalive"
    KA_LABEL = "us.cloudlab.cloudshell.keepalive"

    def _ka_paths(self) -> list[Path]:
        if sys.platform == "darwin":
            return [Path.home() / "Library/LaunchAgents" / f"{self.KA_LABEL}.plist"]
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return [base / "systemd/user" / f"{self.KA_UNIT}.service",
                base / "systemd/user" / f"{self.KA_UNIT}.timer"]

    def _ka_timer_installed(self) -> bool:
        return self._ka_paths()[-1].exists()

    def ka_unattended_warning(self) -> Optional[str]:
        """Warning text if experiments are kept alive but nothing will check them."""
        n = len(self._ka_load())
        if not n or self._ka_timer_installed():
            return None
        return (f"! {n} experiment(s) on the keep-alive list, but no background timer: "
                "they are only extended when you run `keep-alive tick`. "
                "Run `keep-alive install` to keep them alive after you quit.")

    def _ka_install(self):
        import subprocess
        cmd = [sys.executable, str(Path(__file__).resolve()), "-c", "keep-alive tick"]
        paths = self._ka_paths()
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            import plistlib
            with open(paths[0], "wb") as f:
                plistlib.dump({"Label": self.KA_LABEL, "ProgramArguments": cmd,
                               "StartInterval": 1800, "RunAtLoad": True,
                               "StandardOutPath": str(CACHE_DIR / "keepalive.out"),
                               "StandardErrorPath": str(CACHE_DIR / "keepalive.out")}, f)
            r = subprocess.run(["launchctl", "load", "-w", str(paths[0])],
                               capture_output=True, text=True)
        else:
            if not shutil.which("systemctl"):
                print(red("systemctl not found. Add this line with `crontab -e` instead:"))
                print(f"*/30 * * * * DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus "
                      f"{shlex.join(cmd)} >> {CACHE_DIR / 'keepalive.out'} 2>&1")
                return
            paths[0].write_text(
                "[Unit]\nDescription=cloudshell keep-alive: extend CloudLab experiments\n\n"
                f"[Service]\nType=oneshot\nExecStart={shlex.join(cmd)}\n")
            paths[1].write_text(
                "[Unit]\nDescription=Run cloudshell keep-alive every 30 minutes\n\n"
                "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=30min\nPersistent=true\n\n"
                "[Install]\nWantedBy=timers.target\n")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            r = subprocess.run(["systemctl", "--user", "enable", "--now",
                                f"{self.KA_UNIT}.timer"], capture_output=True, text=True)
        if r.returncode != 0:
            print(red(f"✗ could not start the timer: {(r.stderr or r.stdout).strip()}"))
            return
        print(green("✓ keep-alive now runs every 30 minutes in the background"))
        print(dim(f"  files: {', '.join(map(str, paths))}"))
        print(dim("  it uses the password/session in your keychain, so it works while you're "
                  "logged in (keychain unlocked). `keep-alive log` shows what it did."))

    def _ka_uninstall(self):
        import subprocess
        paths = self._ka_paths()
        if not any(p.exists() for p in paths):
            print(dim("no background timer installed"))
            return
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", "-w", str(paths[0])], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{self.KA_UNIT}.timer"],
                           capture_output=True)
        for p in paths:
            p.unlink(missing_ok=True)
        if sys.platform != "darwin":
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(green("✓ background keep-alive timer removed (kept-alive list is unchanged)"))


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
_session = None


def ask(prompt: str) -> str:
    if _session is not None:
        return _session.prompt(prompt)
    try:
        return input(prompt)
    except EOFError:
        return ""


def load_state() -> dict:
    try:
        return json.loads((CACHE_DIR / "state.json").read_text())
    except Exception:
        return {}


def save_state(upd: dict):
    st = load_state()
    st.update(upd)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "state.json").write_text(json.dumps(st))


def load_token(path: Optional[str]) -> Optional[str]:
    if path:
        return Path(path).expanduser().read_text().strip()
    if os.environ.get("PORTAL_TOKEN"):
        return os.environ["PORTAL_TOKEN"].strip()
    for p in (HERE / "cloudlab.jwt", Path.cwd() / "cloudlab.jwt"):
        if p.exists():
            return p.read_text().strip()
    return None


def check_token_expiry(token: str):
    try:
        p = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("exp")
    except Exception:
        return
    if not exp:
        return
    left = exp - time.time()
    if left <= 0:
        print(red("! your portal token has EXPIRED — download a new one "
                  "(CloudLab > your name > Portal API Token)"))
    elif left < 7 * 86400:
        print(yellow(f"! portal token expires in {left / 86400:.1f} days"))


def main(argv=None):
    global _session
    ap = argparse.ArgumentParser(description="Interactive CloudLab machine finder / reservation shell")
    ap.add_argument("--project", help="CloudLab project (default: remembered, or asked once)")
    ap.add_argument("--user", help="CloudLab username (default: remembered, or asked at first login)")
    ap.add_argument("--token-file", help="optional Portal API token, enables the slow API-search "
                                         "fallback (default: ./cloudlab.jwt or $PORTAL_TOKEN)")
    ap.add_argument("--portal-url", default=os.environ.get("PORTAL_HTTP", DEFAULT_PORTAL_URL))
    ap.add_argument("-c", "--command", action="append",
                    help="run command(s) non-interactively and exit (repeatable, or ';'-separated)")
    a = ap.parse_args(argv)

    portal = None
    token = load_token(a.token_file)
    if token and HAVE_API:
        check_token_expiry(token)
        portal = Portal(a.portal_url, token)
    project = a.project or os.environ.get("CLOUDLAB_PROJECT") or load_state().get("project")
    web = WebPortal(uid=a.user)
    if not web.secrets.ok:
        print(yellow("! no system keychain found — your password won't be saved between runs"
                     + (" (on Linux install gnome-keyring or KWallet)" if sys.platform.startswith("linux") else "")))
    sh = Shell(portal, project, web)
    if a.project:
        save_state({"project": a.project})
    sh.load()

    if a.command:
        for chunk in a.command:
            for cmd in chunk.split(";"):
                if not sh.run(cmd):
                    return
        return

    print(bold("cloudshell") + dim(f" — {len(sh.catalog)} hardware types · user "
                                   f"{web.uid or '(login on first use)'} · project "
                                   f"{project or '(asked on first use)'} · type `help`"))
    w = sh.ka_unattended_warning()
    if w:
        print(yellow(w))
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import FileHistory
        if sys.stdin.isatty():
            words = ["find", "hw", "show", "reserve", "mine", "cancel", "exps", "exp",
                     "keep-alive", "unalive", "tick", "install", "uninstall", "project",
                     "refresh", "login", "logout", "help", "quit"] + sorted({h.type for h in sh.catalog})
            _session = PromptSession(history=FileHistory(str(CACHE_DIR / "history")),
                                     completer=WordCompleter(words, ignore_case=True))
    except ImportError:
        pass
    while True:
        try:
            line = ask("cloudlab> ") if _session else input("cloudlab> ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            print()
            break
        if not sh.run(line):
            break
    w = sh.ka_unattended_warning()
    if w:
        print(yellow(w))


if __name__ == "__main__":
    main()
