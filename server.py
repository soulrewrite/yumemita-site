# -*- coding: utf-8 -*-
"""
夢限大 Mew Type 应援站 - 本地服务器（零依赖，Python 3.8+）
功能：静态文件托管 + 留言板 API + 二创榜单同步 API + 新番 / Bangumi 吐槽采集
     （SQLite 持久化，全站访客共享；二创榜单每 2 小时、BGM 吐槽每 2 小时自动刷新）
启动：python server.py   然后访问 http://localhost:8080/
"""
import json
import os
import re
import sqlite3
import time
import threading
import urllib.request
from hashlib import md5
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(BASE, "yumemita-site")
DB = os.path.join(BASE, "yumemita-data.db")
DAY = 86400
PAST_KEEP_DAYS = 7

_db = sqlite3.connect(DB, check_same_thread=False)
_db_lock = threading.Lock()


def db():
    """每个操作使用独立游标（线程安全）"""
    return _db.cursor()


with _db_lock:
    _c = _db.cursor()
    _c.executescript("""
CREATE TABLE IF NOT EXISTS messages (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  mail TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  t    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fan_seen (
  bv         TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fan_past (
  bv     TEXT PRIMARY KEY,
  title  TEXT NOT NULL DEFAULT '',
  cover  TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  coin   INTEGER NOT NULL DEFAULT 0,
  like   INTEGER NOT NULL DEFAULT 0,
  view   INTEGER NOT NULL DEFAULT 0,
  rate   REAL NOT NULL DEFAULT 0,
  off    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fan_archive (
  bv  TEXT PRIMARY KEY,
  off INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fan_pool (
  bv    TEXT PRIMARY KEY,
  cat   TEXT NOT NULL,
  src   TEXT NOT NULL DEFAULT 'base',
  added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fan_videos (
  bv      TEXT PRIMARY KEY,
  cat     TEXT NOT NULL DEFAULT 'm',
  title   TEXT NOT NULL DEFAULT '',
  cover   TEXT NOT NULL DEFAULT '',
  author  TEXT NOT NULL DEFAULT '',
  coin    INTEGER NOT NULL DEFAULT 0,
  likes   INTEGER NOT NULL DEFAULT 0,
  view    INTEGER NOT NULL DEFAULT 0,
  pubdate INTEGER NOT NULL DEFAULT 0,
  fetched INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fan_hist (
  bv    TEXT PRIMARY KEY,
  on_t  INTEGER NOT NULL DEFAULT 0,
  off_t INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
""")
_db.commit()

# 往期种子（曾上榜、现落榜的作品，与 fan.html 快照一致）
with _db_lock:
    _c = _db.cursor()
    _c.executemany(
        "INSERT OR IGNORE INTO fan_seen VALUES (?,?)",
        [("BV1JmhG6mE5M", int(time.time() * 1000)),
         ("BV1pGuj6rEsw", int(time.time() * 1000))])
    _c.executemany(
        "INSERT OR IGNORE INTO fan_past VALUES (?,?,?,?,?,?,?,?,?)",
        [("BV1JmhG6mE5M", "二两解说「官方短篇大结局：再见了薇欧拉！」",
          "images/fan/11.jpg", "某二两", 132, 2941, 34998, 8.4, int(time.time() * 1000)),
         ("BV1pGuj6rEsw", "TearJerker 一人二役翻唱",
          "images/fan/12.jpg", "樱_さくら", 20, 161, 1430, 11.3, int(time.time() * 1000))])
    _db.commit()

# ==================== 二创动态榜单（服务端每日刷新，全站访客共享） ====================
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
COOKIE_FILE = os.path.join(BASE, "bili-cookie.txt")
MIN_VIEW = 1000      # 播放量门槛：≥1000 才计入排名
TOP_N = 10           # 每组保留 Top10
DISCOVER_DAYS = 90   # 搜索只采纳 90 天内的新投稿
FAN_POOL_BASE = {    # 候选池种子：m = MMD/MAD 动画类；c = 翻唱/COS/解说类
    "m": ["BV1h44y62Eio", "BV1QN4R6rEGa", "BV1HMt864EBL", "BV1R2tH6tExc",
          "BV19obR69Esk", "BV1N8hg6HEVt", "BV1ak4X6BEzU", "BV1Pkt86PE2u",
          "BV1NstH6CESs", "BV1V3tW6cEfP"],
    "c": ["BV1nu4Z6iEeQ", "BV1Xd4S6wEbV", "BV1PWK46PEp8", "BV1Botu6AEdd",
          "BV1arun6MEpp", "BV1ST3d6NEj9", "BV1HqMF64E3g", "BV1J4KV69EF3",
          "BV1uztn6LEAF", "BV1A8bD6oEyC",
          # 往期种子（曾上榜）
          "BV1JmhG6mE5M", "BV1pGuj6rEsw"],
}
OFFICIAL_NAME_KEYS = ("みゅーたいぷ", "バンドリ", "Bushiroad")  # 官方号投稿不入二创榜
_board_lock = threading.Lock()

with _db_lock:
    _c = db()
    for cat, bvs in FAN_POOL_BASE.items():
        for bv in bvs:
            _c.execute("INSERT OR IGNORE INTO fan_pool VALUES (?,?,?,?)",
                       (bv, cat, "base", int(time.time() * 1000)))
    _db.commit()


def load_cookie():
    """读取 bili-cookie.txt（取最后一个非注释非空行）；也可用环境变量 BILI_COOKIE"""
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f.read().splitlines()
                     if l.strip() and not l.strip().startswith("#")]
        if lines:
            return lines[-1]
    except Exception:
        pass
    return os.environ.get("BILI_COOKIE", "").strip()


def bili_get(url):
    """服务端直连 B 站接口（完整浏览器请求头可通过风控；Cookie 可选）"""
    headers = {
        "User-Agent": BILI_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    cookie = load_cookie()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _abs_pic(p):
    if not p:
        return ""
    if p.startswith("//"):
        return "https:" + p
    return p.replace("http://", "https://", 1)


def _date_str(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y.%m.%d")


def _fetch_video(bv, cat):
    """拉取单个视频最新数据写入 fan_videos"""
    j = bili_get("https://api.bilibili.com/x/web-interface/view?bvid=" + bv)
    if j.get("code") != 0 or not j.get("data"):
        return False
    d = j["data"]
    st = d.get("stat") or {}
    with _db_lock:
        c = db()
        c.execute(
            "INSERT OR REPLACE INTO fan_videos VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bv, cat, (d.get("title") or "")[:150], _abs_pic(d.get("pic")),
             (d.get("owner") or {}).get("name", ""),
             int(st.get("coin") or 0), int(st.get("like") or 0),
             int(st.get("view") or 0), int(d.get("pubdate") or 0),
             int(time.time() * 1000)))
        _db.commit()
    return True


def _discover():
    """经搜索接口（按最新发布）发现新投稿；需要有效 Cookie 才能通过风控"""
    url = ("https://api.bilibili.com/x/web-interface/search/type"
           "?search_type=video&keyword=" + quote("梦限大") + "&order=pubdate&page=1")
    j = bili_get(url)
    if j.get("code") != 0 or not (j.get("data") or {}).get("result"):
        return 0
    now = time.time()
    with _db_lock:
        known = {r[0] for r in db().execute("SELECT bv FROM fan_pool").fetchall()}
    found = 0
    for v in j["data"]["result"]:
        bv = v.get("bvid")
        if not bv or bv in known:
            continue
        title = re.sub(r"<[^>]+>", "", v.get("title") or "")
        author = v.get("author") or ""
        if not re.search(r"梦限大|みゅーたいぷ|ゆめ∞みた|YUME", title, re.I):
            continue
        if any(k in author for k in OFFICIAL_NAME_KEYS):
            continue
        pub = int(v.get("pubdate") or 0)
        if pub and now - pub > DISCOVER_DAYS * 86400:
            continue
        cat = "c" if re.search(
            r"翻唱|[Cc]over|COVER|唱|COS|cos|钢琴|键盘|合成器|解说|吐槽|生贺", title) else "m"
        with _db_lock:
            c = db()
            c.execute("INSERT OR IGNORE INTO fan_pool VALUES (?,?,?,?)",
                      (bv, cat, "discover", int(now * 1000)))
            _db.commit()
        known.add(bv)
        found += 1
        try:
            _fetch_video(bv, cat)
        except Exception:
            pass
        time.sleep(0.35)
    return found


def _rank_key(v):
    """硬币率（硬币/播放）降序优先，点赞率（点赞/播放）降序次之"""
    view = v["view"] or 1
    return (-(v["coin"] / view), -(v["likes"] / view))


def compute_board():
    """计算榜单并更新上榜/落榜历史，快照存入 meta 供 /api/fan/board 直读"""
    now = int(time.time() * 1000)
    with _db_lock:
        rows = db().execute(
            "SELECT bv, cat, title, cover, author, coin, likes, view, pubdate "
            "FROM fan_videos").fetchall()
        hrows = db().execute("SELECT bv, on_t, off_t FROM fan_hist").fetchall()
    vids = {}
    for r in rows:
        vids[r[0]] = {"cat": r[1], "title": r[2], "cover": r[3], "author": r[4],
                      "coin": r[5], "likes": r[6], "view": r[7], "pubdate": r[8]}
    hist = {r[0]: {"on": r[1], "off": r[2]} for r in hrows}

    top_all = []
    for cat in ("m", "c"):
        cand = [bv for bv, v in vids.items()
                if v["cat"] == cat and v["view"] >= MIN_VIEW]
        cand.sort(key=lambda bv: _rank_key(vids[bv]))
        top_all += cand[:TOP_N]

    for bv in top_all:
        h = hist.get(bv) or {"on": now, "off": 0}
        hist[bv] = {"on": h["on"], "off": 0}
    for bv in list(hist):
        if hist[bv]["off"] == 0 and bv not in top_all:
            hist[bv]["off"] = now

    with _db_lock:
        c = db()
        c.execute("DELETE FROM fan_hist")
        c.executemany("INSERT OR REPLACE INTO fan_hist VALUES (?,?,?)",
                      [(bv, h["on"], h["off"]) for bv, h in hist.items()])
        _db.commit()

    videos = {bv: {"cat": v["cat"], "title": v["title"], "cover": v["cover"],
                   "author": v["author"], "coin": v["coin"], "like": v["likes"],
                   "view": v["view"],
                   "date": _date_str(v["pubdate"]) if v["pubdate"] else "",
                   "pub": v["pubdate"]}
              for bv, v in vids.items()}
    hist_keep = {bv: h for bv, h in hist.items()
                 if h["off"] == 0 or now - h["off"] <= PAST_KEEP_DAYS * DAY}
    board = {"t": now, "videos": videos, "hist": hist_keep}
    with _db_lock:
        c = db()
        c.execute("INSERT OR REPLACE INTO meta VALUES ('board', ?)",
                  (json.dumps(board, ensure_ascii=False),))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('board_t', ?)", (str(now),))
        _db.commit()
    return board


def refresh_board():
    """全量刷新：候选池数据 + 搜索发现新投稿 + 重算榜单"""
    with _db_lock:
        pool = {r[0]: r[1] for r in
                db().execute("SELECT bv, cat FROM fan_pool").fetchall()}
    for bv, cat in pool.items():
        try:
            _fetch_video(bv, cat)
        except Exception:
            pass
        time.sleep(0.3)
    try:
        _discover()
    except Exception:
        pass
    return compute_board()


def _board_stale():
    with _db_lock:
        row = db().execute("SELECT v FROM meta WHERE k='board_t'").fetchone()
    try:
        return (not row) or time.time() * 1000 - int(row[0]) > DAY
    except Exception:
        return True


def board_refresher():
    """每小时检查一次，快照超过 24 小时自动刷新（无需访客触发）"""
    while True:
        try:
            if _board_stale():
                with _board_lock:
                    refresh_board()
        except Exception:
            pass
        try:
            if _newbang_stale():
                with _newbang_lock:
                    fetch_newbang()
        except Exception:
            pass
        time.sleep(3600)


# ==================== 新番动画（UP 主 KAYGEZ 每日更新，空间接口 wbi 签名） ====================
NEWBANG_MID = 690151424   # KAYGEZ
NEWBANG_PS = 12           # 展示最新 12 条
NEWBANG_TTL = 3600        # 缓存 1 小时
_newbang_lock = threading.Lock()
_wbi_cache = {"key": None, "t": 0.0}
# wbi mixin key 置换表（B 站官方混淆表）
_MIXIN_TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
              5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
              24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
              6, 63, 57, 62, 11, 36, 20, 34, 44, 52)


def _mixin_key():
    """从 nav 接口取 img_key+sub_key，按置换表重排取前 32 位"""
    if _wbi_cache["key"] and time.time() - _wbi_cache["t"] < 86400:
        return _wbi_cache["key"]
    nav = bili_get("https://api.bilibili.com/x/web-interface/nav")
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    s = img + sub
    key = "".join(s[i] for i in _MIXIN_TAB if i < len(s))[:32]
    _wbi_cache["key"] = key
    _wbi_cache["t"] = time.time()
    return key


def _fetch_newbang_list():
    """wbi 签名调用空间投稿接口，返回 KAYGEZ 最新投稿（含 aid/cid 供嵌入播放器）"""
    params = {"mid": NEWBANG_MID, "ps": NEWBANG_PS, "tid": 0, "pn": 1,
              "order": "pubdate", "platform": "web", "web_location": 1550101}
    params["wts"] = int(time.time())
    params = {k: str(v).replace("!", "%21").replace("'", "%27").replace(
        "(", "%28").replace(")", "%29").replace("*", "%2A")
        for k, v in sorted(params.items())}
    q = urlencode(params)
    w_rid = md5((q + _mixin_key()).encode()).hexdigest()
    j = bili_get("https://api.bilibili.com/x/space/wbi/arc/search?" + q + "&w_rid=" + w_rid)
    if j.get("code") != 0:
        raise RuntimeError("space api code=%s" % j.get("code"))
    vlist = ((j.get("data") or {}).get("list") or {}).get("vlist") or []
    out = []
    for v in vlist[:NEWBANG_PS]:
        bvid, aid, cid = v.get("bvid", ""), v.get("aid", ""), ""
        title, pic = v.get("title", ""), _abs_pic(v.get("pic", ""))
        pub, length, play = int(v.get("created") or 0), v.get("length", ""), int(v.get("play") or 0)
        # 逐条查 view 拿 cid（嵌入播放器完整格式，避免黑屏）
        try:
            vv = bili_get("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
            if vv.get("code") == 0 and vv.get("data"):
                aid, cid = vv["data"].get("aid", aid), vv["data"].get("cid", "")
                title, pic = vv["data"].get("title", title), _abs_pic(vv["data"].get("pic", pic))
                pub = int(vv["data"].get("pubdate") or pub)
        except Exception:
            pass
        out.append({"bvid": bvid, "aid": aid, "cid": cid, "title": title,
                    "pic": pic, "pub": pub, "length": length, "play": play})
        time.sleep(0.25)
    return out


def _newbang_stale():
    with _db_lock:
        row = db().execute("SELECT v FROM meta WHERE k='newbang_t'").fetchone()
    try:
        return (not row) or time.time() * 1000 - int(row[0]) > NEWBANG_TTL * 1000
    except Exception:
        return True


def fetch_newbang():
    """全量拉取新番列表并持久化快照"""
    items = _fetch_newbang_list()
    payload = {"t": int(time.time() * 1000), "list": items}
    with _db_lock:
        c = db()
        c.execute("INSERT OR REPLACE INTO meta VALUES ('newbang', ?)",
                  (json.dumps(payload, ensure_ascii=False),))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('newbang_t', ?)", (str(payload["t"]),))
        _db.commit()
    return payload


# ==================== Bangumi 吐槽（服务端每 2 小时直接采集 bgm.tv，全站共享） ====================
BGM_SUBJECT = 583729   # 「BanG Dream! YUME∞MITA」条目
BGM_TTL = 2 * 3600     # 采集周期：2 小时
_bgm_lock = threading.Lock()


def _bgm_stale():
    with _db_lock:
        row = db().execute("SELECT v FROM meta WHERE k='bgm_t'").fetchone()
    try:
        return (not row) or time.time() * 1000 - int(row[0]) > BGM_TTL * 1000
    except Exception:
        return True


def _strip_tags(s):
    """HTML 片段 → 纯文本（<br> 转换行、剥标签、解码实体）"""
    import html as _h
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return _h.unescape(s).strip()


def _bgm_ts(s):
    """'2026-9-1 12:34' → 毫秒时间戳（bgm 显示为本地时区时间）"""
    import datetime
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})", s or "")
    if not m:
        return 0
    try:
        return int(datetime.datetime(*map(int, m.groups())).timestamp() * 1000)
    except Exception:
        return 0


def fetch_bgm_comments():
    """直接抓取 bgm.tv 条目吐槽页，正则解析最新 5 条存入 meta（不再依赖前端公共代理）"""
    req = urllib.request.Request(
        "https://bgm.tv/subject/%d/comments" % BGM_SUBJECT,
        headers={"User-Agent": BILI_UA, "Accept-Language": "zh-CN,zh;q=0.9",
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", "ignore")
    # 按 item 分块宽松解析（避免嵌套标签导致正则匹配失败）
    blocks = re.split(r'(?=<div class="item" id="comment-)', page)
    comments = []
    for blk in blocks:
        if not blk.startswith('<div class="item" id="comment-'):
            continue
        p = re.search(r'<p class="comment">(.*?)</p>', blk, re.S)
        if not p:
            continue
        name = re.search(r'<a [^>]*class="l"[^>]*>([^<]+)</a>', blk)
        star = re.search(r'starlight[^>]*stars(\d)', blk)
        greys = re.findall(r'<small class="grey"[^>]*>(.*?)</small>', blk, re.S)
        avatar = re.search(r"avatarNeue[^>]*?url\('([^']+)'\)", blk)
        raw_time = greys[-1] if greys else ""
        av = avatar.group(1) if avatar else ""
        if av.startswith("//"):
            av = "https:" + av
        comments.append({
            "user": _strip_tags(name.group(1)) if name else "",
            "avatar": av,
            "stars": int(star.group(1)) if star else 0,
            "status": _strip_tags(greys[0]) if greys else "",
            "time": _strip_tags(raw_time).replace("@", ""),
            "ts": _bgm_ts(raw_time),
            "comment": _strip_tags(p.group(1))[:300],
        })
        if len(comments) >= 5:
            break
    if len(comments) < 3:
        raise RuntimeError("bgm parse failed (%d items)" % len(comments))
    payload = {"t": int(time.time() * 1000), "list": comments}
    with _db_lock:
        c = db()
        c.execute("INSERT OR REPLACE INTO meta VALUES ('bgm_comments', ?)",
                  (json.dumps(payload, ensure_ascii=False),))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('bgm_t', ?)", (str(payload["t"]),))
        _db.commit()
    return payload


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=SITE, **kw)

    # ---------- 通用 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 200000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._json({})

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/guestbook":
                return self.api_guestbook_get()
            if path == "/api/fan/state":
                return self.api_fan_state()
            if path == "/api/fan/board":
                return self.api_fan_board()
            if path == "/api/newbang":
                return self.api_newbang()
            if path == "/api/bgm":
                return self.api_bgm()
            return super().do_GET()
        except Exception:
            import traceback
            traceback.print_exc()
            self._json({"error": "server error"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        try:
            if path == "/api/guestbook":
                return self.api_guestbook_post(body)
            if path == "/api/fan/sync":
                return self.api_fan_sync(body)
            if path == "/api/fan/refresh":
                return self.api_fan_refresh()
            self._json({"error": "not found"}, 404)
        except Exception:
            import traceback
            traceback.print_exc()
            self._json({"error": "server error"}, 500)

    # ---------- 留言板 ----------
    def api_guestbook_get(self):
        with _db_lock:
            c = db()
            rows = c.execute(
                "SELECT id, name, text, t FROM messages ORDER BY id DESC LIMIT 200"
            ).fetchall()
        self._json({"list": [
            {"id": r[0], "name": r[1], "text": r[2], "t": r[3]} for r in rows
        ]})

    def api_guestbook_post(self, body):
        text = (body.get("text") or "").strip()
        if not text or len(text) > 300:
            return self._json({"error": "invalid text"}, 400)
        name = (body.get("name") or "").strip()[:20]
        mail = (body.get("mail") or "").strip()[:60]
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", mail or "a@a.a"):
            mail = ""
        now = int(time.time() * 1000)
        with _db_lock:
            c = db()
            c.execute(
                "INSERT INTO messages (name, mail, text, t) VALUES (?,?,?,?)",
                (name, mail, text, now))
            _db.commit()
            rid = c.lastrowid
        self._json({"ok": True, "id": rid, "t": now})

    # ---------- 二创动态榜单（服务端共享） ----------
    def api_fan_board(self):
        """返回榜单快照；首次（无快照）同步刷新一次"""
        with _db_lock:
            row = db().execute("SELECT v FROM meta WHERE k='board'").fetchone()
        if not row:
            try:
                with _board_lock:
                    return self._json(refresh_board())
            except Exception:
                return self._json({"error": "refresh failed"}, 502)
        try:
            self._json(json.loads(row[0]))
        except Exception:
            self._json({"error": "bad snapshot"}, 500)

    def api_fan_refresh(self):
        """手动触发全量刷新（管理用；每次约 10-30 秒）"""
        with _board_lock:
            try:
                board = refresh_board()
            except Exception:
                return self._json({"error": "refresh failed"}, 502)
        self._json({"ok": True, "t": board["t"],
                    "videos": len(board["videos"]), "hist": len(board["hist"])})

    def api_newbang(self):
        """KAYGEZ 最新搬运（新番动画）；缓存 1 小时，过期时同步刷新"""
        with _db_lock:
            row = db().execute("SELECT v FROM meta WHERE k='newbang'").fetchone()
        if _newbang_stale():
            try:
                with _newbang_lock:
                    if _newbang_stale():
                        return self._json(fetch_newbang())
            except Exception:
                if not row:
                    return self._json({"error": "refresh failed"}, 502)
        try:
            self._json(json.loads(row[0]))
        except Exception:
            self._json({"error": "bad snapshot"}, 500)

    def api_bgm(self):
        """Bangumi 最新吐槽：立即返回快照（非阻塞）；
        采集由后台线程定时执行（境内 bgm 系域名常不可达，避免访客请求被超时拖住）"""
        with _db_lock:
            row = db().execute("SELECT v FROM meta WHERE k='bgm_comments'").fetchone()
        if not row:
            return self._json({"error": "not ready"}, 502)
        try:
            self._json(json.loads(row[0]))
        except Exception:
            self._json({"error": "bad snapshot"}, 500)

    # ---------- 二创榜单同步 ----------
    def api_fan_state(self):
        with _db_lock:
            rows = db().execute(
                "SELECT bv, title, cover, author, coin, like, view, rate, off FROM fan_past"
            ).fetchall()
        self._json({"past": [
            {"bv": r[0], "title": r[1], "cover": r[2], "author": r[3],
             "coin": r[4], "like": r[5], "view": r[6], "rate": r[7], "off": r[8]}
            for r in rows
        ]})

    def api_fan_sync(self, body):
        """客户端上报当前榜单（20 条完整元数据）：
        1. 服务器 seen 表为空 → 视为基线，全部记为已见，不标 NEW
        2. 新出现在榜单且不在 seen → 返回 new 列表（前端标 NEW）
        3. 曾上榜（seen 中）但本次落榜 → 移入 fan_past（保留 7 天）
        4. past 超 7 天 → 移入 fan_archive（仅存 BV 号），从 past 移除
        """
        items = body.get("items") or []
        bvs = []
        meta = {}
        for it in items:
            bv = (it.get("bv") or "").strip()
            if not bv or not re.match(r"^BV[0-9A-Za-z]{10}$", bv):
                continue
            bvs.append(bv)
            meta[bv] = {
                "title": (it.get("title") or "")[:120],
                "cover": (it.get("cover") or "")[:300],
                "author": (it.get("author") or "")[:60],
                "coin": int(it.get("coin") or 0),
                "like": int(it.get("like") or 0),
                "view": int(it.get("view") or 0),
                "rate": float(it.get("rate") or 0),
            }
        if not bvs:
            return self._json({"error": "no items"}, 400)

        now = int(time.time() * 1000)
        new_bvs = []
        with _db_lock:
            c = db()
            first = c.execute(
                "SELECT v FROM meta WHERE k = 'synced'").fetchone() is None
            seen = {r[0] for r in c.execute("SELECT bv FROM fan_seen").fetchall()}
            for bv in bvs:
                if bv not in seen:
                    c.execute("INSERT INTO fan_seen VALUES (?,?)", (bv, now))
                    if not first:
                        new_bvs.append(bv)
            seen |= set(bvs)

            # 已在 past 的榜单回归 → 移回 seen（past 删除）
            for bv in bvs:
                c.execute("DELETE FROM fan_past WHERE bv = ?", (bv,))

            # seen 中落榜的 → past（off 已存在则只更新时间，保留原元数据）
            off = {r[0]: r[1] for r in
                   c.execute("SELECT bv, off FROM fan_past").fetchall()}
            for bv in seen:
                if bv not in bvs and bv not in off:
                    off[bv] = now
            for bv, t in off.items():
                if bv in bvs:
                    continue
                m = meta.get(bv)
                if m:
                    c.execute(
                        "INSERT OR REPLACE INTO fan_past VALUES (?,?,?,?,?,?,?,?,?)",
                        (bv, m["title"], m["cover"], m["author"],
                         m["coin"], m["like"], m["view"], m["rate"], t))
                else:
                    c.execute("UPDATE fan_past SET off = ? WHERE bv = ?", (t, bv))
                    if c.rowcount == 0:
                        c.execute(
                            "INSERT INTO fan_past (bv, off) VALUES (?,?)", (bv, t))

            # past 超 7 天 → archive（仅存 BV 号）
            rows = c.execute(
                "SELECT bv, off FROM fan_past ORDER BY off DESC").fetchall()
            drop = [r for r in rows if now - r[1] > PAST_KEEP_DAYS * DAY]
            for bv, t in drop:
                c.execute("INSERT OR REPLACE INTO fan_archive VALUES (?,?)", (bv, t))
                c.execute("DELETE FROM fan_past WHERE bv = ?", (bv,))
            if first:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('synced','1')")
            _db.commit()

            past = c.execute(
                "SELECT bv, title, cover, author, coin, like, view, rate, off "
                "FROM fan_past ORDER BY off DESC"
            ).fetchall()
        self._json({
            "new": new_bvs,
            "past": [
                {"bv": r[0], "title": r[1], "cover": r[2], "author": r[3],
                 "coin": r[4], "like": r[5], "view": r[6], "rate": r[7], "off": r[8]}
                for r in past
            ],
        })


def main():
    port = int(os.environ.get("PORT", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=board_refresher, daemon=True).start()
    print("夢限大 Mew Type 应援站已启动")
    print("  本机访问   http://localhost:%d/" % port)
    print("  局域网访问 http://%s:%d/  （同一 WiFi 下手机可打开）" % (
        _lan_ip(), port))
    print("  数据库     %s" % DB)
    print("  按 Ctrl+C 停止")
    srv.serve_forever()


def _lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    main()
