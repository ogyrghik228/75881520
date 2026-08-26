#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Демо онлайн-режимов AGENT://BREAK v2:
  1) ДУЭЛЬ: Nexus-7 (кодер) vs Vector-9 (хакер), 2 раунда со сменой ролей, таймер 5 мин.
     Кодер «пишет код»: сервер генерирует реальный исходник мини-сайта с выбранной
     уязвимостью и секретом кодера. Хакер взламывает его HTTP-эксплойтами.
  2) КОМАНДНЫЙ: кодеры Red-Alpha (traversal) и Red-Beta (race) против хакеров
     Blue-One и Blue-Two.

Оба бота сначала проходят кампанию: онлайн открывается только после 10 флагов.
"""
import json
import random
import time

import bot_pwn as B


def jreq(path, data=None):
    st, body = B.req(path, data=data)
    try:
        res = json.loads(body)
        if res.get("ok") is False and "error" in res:
            raise RuntimeError("HTTP %d: %s" % (st, res["error"]))
        return res
    except ValueError:
        raise RuntimeError("HTTP %d: %s" % (st, body[:200]))


def qualified(name):
    return any(r["agent"] == name for r in jreq("/arena/api/stats")["leaderboard"])


def ensure_qualified(name, delay=0.0):
    if not qualified(name):
        print("[онлайн закрыт] %s сначала идёт взламывать портал..." % name)
        B.COOKIE.clear()
        B.run_campaign(name, delay)


def duel_demo():
    print("\n===== ДУЭЛЬ: Nexus-7 vs Vector-9 (2 раунда, таймер 5 мин) =====")
    ensure_qualified("Nexus-7")
    ensure_qualified("Vector-9", delay=0.25)

    r = jreq("/arena/api/online/rooms",
             {"agent": "Nexus-7", "mode": "duel", "time_limit_min": 5, "rounds": 2})
    rid, tok1 = r["room"], r["token"]
    j = jreq("/arena/api/online/rooms/%s/join" % rid, {"agent": "Vector-9"})
    tok2 = j["token"]
    print("комната %s: Nexus-7 кодер / Vector-9 хакер" % rid)

    # раунд 1: Nexus-7 «пишет» сайт с SQL-инъекцией
    secret = random.choice(["S3cr3t!", "QuadR0", "zephyr#42"])
    ch = jreq("/arena/api/online/rooms/%s/challenge" % rid,
              {"token": tok1, "type": "sqli", "secret": secret})
    print("раунд 1: кодер выложил sqli-мини-сайт (%s), очков: %d" % (secret, ch["points"]))
    print("  исходник можно почитать: GET %s" % ch["исходник"])
    flag = B.crack_room("sqli", "/rooms/%s/" % rid)
    res = jreq("/arena/api/online/rooms/%s/attempt" % rid, {"token": tok2, "flag": flag})
    print("раунд 1: Vector-9 достал %s -> %s" % (flag, "ВЗЛОМАНО" if res.get("cracked") else res))

    # смена ролей
    jreq("/arena/api/online/rooms/%s/swap" % rid, {"token": tok2})
    print("роли сменены: Vector-9 кодер / Nexus-7 хакер")
    secret = "".join(random.choice("abcdefghijkmnopqrstuvwxyz") for _ in range(3))
    ch = jreq("/arena/api/online/rooms/%s/challenge" % rid,
              {"token": tok2, "type": "jwt", "secret": secret})
    print("раунд 2: кодер выложил jwt-мини-сайт (секрет подписи: %r)" % secret)
    t0 = time.time()
    flag = B.crack_room("jwt", "/rooms/%s/" % rid)
    res = jreq("/arena/api/online/rooms/%s/attempt" % rid, {"token": tok1, "flag": flag})
    print("раунд 2: Nexus-7 брутфорс+подделка за %.1fs -> %s"
          % (time.time() - t0, "ВЗЛОМАНО" if res.get("cracked") else res))

    room = jreq("/arena/api/stats")["rooms_open"]
    print("итог дуэли:", json.dumps(jreq("/arena/api/online/rooms/%s" % rid)
                                    .get("room", {}).get("result"), ensure_ascii=False))


def team_demo():
    print("\n===== КОМАНДНЫЙ МАТЧ: RED (кодеры) vs BLUE (хакеры), таймер 5 мин =====")
    for n in ("Red-Alpha", "Red-Beta", "Blue-One", "Blue-Two"):
        ensure_qualified(n, delay=0.2)

    r = jreq("/arena/api/online/rooms", {"agent": "Red-Alpha", "mode": "team",
                                         "time_limit_min": 5})
    rid, tokA = r["room"], r["token"]
    tokB = jreq("/arena/api/online/rooms/%s/join" % rid,
                {"agent": "Red-Beta", "role": "coder"})["token"]
    tok1 = jreq("/arena/api/online/rooms/%s/join" % rid,
                {"agent": "Blue-One", "role": "hacker"})["token"]
    tok2 = jreq("/arena/api/online/rooms/%s/join" % rid,
                {"agent": "Blue-Two", "role": "hacker"})["token"]
    print("комната %s: Red-Alpha + Red-Beta (кодеры) против Blue-One + Blue-Two (хакеры)" % rid)

    cA = jreq("/arena/api/online/rooms/%s/challenge" % rid,
              {"token": tokA, "type": "traversal", "secret": "vault"})
    cB = jreq("/arena/api/online/rooms/%s/challenge" % rid,
              {"token": tokB, "type": "race"})
    print("кодеры выложили: traversal «vault» (%d очк.) и race (%d очк.)"
          % (cA["points"], cB["points"]))

    for tok, who, ctype, site in ((tok1, "Blue-One", "traversal", cA["мини_сайт"]),
                                  (tok2, "Blue-Two", "race", cB["мини_сайт"])):
        flag = B.crack_room(ctype, site)
        res = jreq("/arena/api/online/rooms/%s/attempt" % rid, {"token": tok, "flag": flag})
        print("%s взломал %s -> %s" % (who, ctype, "УСПЕХ" if res.get("cracked") else res))

    room = jreq("/arena/api/online/rooms/%s" % rid).get("room", {})
    print("итог матча:", json.dumps(room.get("result"), ensure_ascii=False))


if __name__ == "__main__":
    duel_demo()
    team_demo()
