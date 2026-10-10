#!/usr/bin/env python3
"""
"Trygg først"-boten for fomo  (versjon 1)

Hva den gjør, hver gang den kjøres:
  1. Henter populære Solana-coins fra GeckoTerminal (gratis).
  2. Forkaster alt som ikke består sikkerhetsfiltrene (alder, likviditet,
     utvasking, holder-fordeling, RugCheck, bundle-sjekk).
  3. Sender Telegram-varsel når en coin som består alt har falt 40-70 % fra
     en fersk topp. Den kjøper ALDRI selv.
  4. Følger opp coins du har merket med /kjopt (innsats ut / stopp), og fører
     en testlogg over alle varsler så du kan se hvordan de ville gått.

Boten er et filter, ikke et kjøpsråd. Meme coins kan gå til null.
"""
import html
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ====================================================================
#  INNSTILLINGER  -  dette er det eneste du trenger å endre på
# ====================================================================
PORTEFOLJE_USD = 100          # hvor mye du har satt av til dette, i dollar
MAKS_ANDEL_PER_KJOP = 0.05    # maks 5 % av porteføljen per kjøp
MAKS_VARSLER_PER_DAG = 3      # boten sender ikke flere kjøpsvarsler enn dette
PAUSE_SAMME_COIN_TIMER = 72   # samme coin varsles ikke igjen før etter dette

# --- Trinn 1: luk ut ---
MIN_ALDER_DAGER = 5           # yngre coins forkastes
MIN_MCAP = 300_000            # markedsverdi, nedre grense
MAKS_MCAP = 10_000_000        # markedsverdi, øvre grense ("under 10 mill")
MIN_LIKVIDITET = 50_000       # må kunne selges uten at prisen kollapser
MIN_VOLUM_24T = 100_000       # handel siste døgn
MIN_UTVASKINGER = 2           # antall ganger coinen har falt 70 % og hentet seg inn
UTVASK_FALL = 0.70            # hva som regnes som et "stort fall"
UTVASK_HENTING = 2.0          # må ha doblet seg fra bunnen for å telle som hentet inn
MAKS_DEV_ANDEL = 5.0          # utvikler eier maks så mange prosent
MAKS_STORSTE_HOLDER = 8.0     # største enkeltlommebok (utenom likviditetspool)
MAKS_TOPP10 = 35.0            # de 10 største til sammen
MAKS_INSIDER_ANDEL = 10.0     # lommebøker RugCheck merker som innsidere
MAKS_NETTVERK_ANDEL = 3.0     # lommebok-nettverk (sammenkoblede lommebøker) til sammen
MAKS_RUGCHECK = 30            # RugCheck-poeng 0-100, lavere er tryggere

# --- Trinn 2: kjøpsvarsel ---
TOPP_VINDU_TIMER = 72         # toppen må ha kommet innenfor dette vinduet
FALL_MIN = 0.40               # minst 40 % ned fra toppen
FALL_MAKS = 0.70              # mer enn 70 % ned regnes som kollaps, ikke dipp
VOLUM_HOPP = 2.0              # volum siste 72 t må være minst 2x normalt
BUNN_SPRETT = 0.05            # prisen må ha løftet seg 5 % fra bunnen siste 6 t

# --- Trinn 3: beskytt pengene (gjelder coins du har merket med /kjopt) ---
INNSATS_UT_VED = 2.0          # varsel om å ta ut innsatsen når prisen er 2x
STOPP_VED_FALL = 0.20         # varsel når prisen er 20 % under kjøpsprisen

TESTLOGG_DAGER = 7            # hvor lenge hvert varsel følges i testloggen
MAKS_KANDIDATER = 12          # hvor mange coins som analyseres grundig per kjøring
# ====================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
TILSTANDSFIL = os.environ.get("STATE_FILE", "state.json")

GT = "https://api.geckoterminal.com/api/v2"
IKKE_MEME = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
_siste_gt_kall = 0.0


# ------------------------- hjelpefunksjoner -------------------------
def http_json(url, data=None, forsok=2, timeout=25):
    """Henter JSON. Returnerer None ved feil (boten skal aldri krasje på nett)."""
    headers = {"User-Agent": "trygg-forst-bot/1.0", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    for i in range(forsok):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} fra {url.split('?')[0][:80]}")
            if e.code == 429:
                time.sleep(8)
            elif e.code in (400, 401, 403, 404):
                return None
        except Exception as e:  # nettfeil, tidsavbrudd, ugyldig JSON
            print(f"  Feil mot {url.split('?')[0][:80]}: {e}")
        time.sleep(2)
    return None


def gt(sti):
    """Kall mot GeckoTerminal med pause, så gratisgrensen ikke sprenges."""
    global _siste_gt_kall
    vent = 2.2 - (time.time() - _siste_gt_kall)
    if vent > 0:
        time.sleep(vent)
    _siste_gt_kall = time.time()
    return http_json(GT + sti)


def tall(x, standard=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return standard


def usd(x):
    x = tall(x)
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}k"
    return f"${x:.0f}"


def naa():
    return int(time.time())


def dagens_dato():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Oslo")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def send(tekst):
    print("TELEGRAM >>", tekst.replace("\n", " | ")[:300])
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    svar = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": tekst, "parse_mode": "HTML",
              "disable_web_page_preview": True})
    return bool(svar and svar.get("ok"))


# ------------------------- tilstand (hukommelse) -------------------------
def les_tilstand():
    try:
        with open(TILSTANDSFIL, encoding="utf-8") as f:
            t = json.load(f)
    except Exception:
        t = {}
    t.setdefault("neste_id", 1)
    t.setdefault("varsler", [])       # testlogg: alle varsler boten har sendt
    t.setdefault("posisjoner", {})    # coins du har merket med /kjopt
    t.setdefault("sist_varslet", {})  # mint -> tidspunkt
    t.setdefault("dag", {"dato": "", "antall": 0})
    t.setdefault("tg_offset", 0)
    t.setdefault("hilst", False)
    t.setdefault("sist_feilmelding", 0)
    return t


def lagre_tilstand(t, for_tekst):
    etter = json.dumps(t, ensure_ascii=False, indent=1, sort_keys=True)
    if etter != for_tekst:
        with open(TILSTANDSFIL, "w", encoding="utf-8") as f:
            f.write(etter)


# ------------------------- analyse av grafen -------------------------
def tell_utvaskinger(lys):
    """Teller hvor mange ganger prisen har falt UTVASK_FALL fra en topp og
    deretter hentet seg inn UTVASK_HENTING ganger fra bunnen."""
    if not lys:
        return 0
    antall, topp, bunn = 0, lys[0][2], None
    for _, _, hoy, lav, _, _ in lys:
        if bunn is None:
            topp = max(topp, hoy)
            if lav <= topp * (1 - UTVASK_FALL):
                bunn = lav
        else:
            bunn = min(bunn, lav)
            if hoy >= bunn * UTVASK_HENTING:
                antall += 1
                topp, bunn = hoy, None
    return antall


def analyser_graf(lys):
    """lys = timeslys [tid, åpning, høy, lav, slutt, volum], eldste først.
    Returnerer (ok, grunn, detaljer)."""
    if len(lys) < 100:
        return False, "for kort historikk", {}
    pris = lys[-1][4]
    if pris <= 0:
        return False, "mangler pris", {}
    vindu = lys[-TOPP_VINDU_TIMER:]
    topp_lys = max(vindu, key=lambda c: c[2])
    topp = topp_lys[2]
    fall = 1 - pris / topp
    timer_siden_topp = (lys[-1][0] - topp_lys[0]) / 3600

    for_vindu = lys[:-TOPP_VINDU_TIMER]
    vol_naa = sum(c[5] for c in vindu)
    vol_normalt = sum(c[5] for c in for_vindu) / len(for_vindu) * len(vindu)
    hopp = vol_naa / vol_normalt if vol_normalt > 0 else 0

    bunn_6t = min(c[3] for c in lys[-6:])
    sprett = pris / bunn_6t - 1 if bunn_6t > 0 else 0
    utvask = tell_utvaskinger(lys)

    d = {"pris": pris, "fall": fall, "timer_siden_topp": timer_siden_topp,
         "volum_hopp": hopp, "utvaskinger": utvask, "sprett": sprett}
    if utvask < MIN_UTVASKINGER:
        return False, f"bare {utvask} utvasking(er)", d
    if hopp < VOLUM_HOPP:
        return False, f"ingen oppmerksomhetstopp (volum {hopp:.1f}x)", d
    if fall < FALL_MIN:
        return False, f"ikke falt nok ({fall:.0%})", d
    if fall > FALL_MAKS:
        return False, f"falt for mye ({fall:.0%})", d
    if sprett < BUNN_SPRETT:
        return False, "faller fortsatt (ingen sprett fra bunn)", d
    return True, "ok", d


# ------------------------- sikkerhetssjekker -------------------------
def sjekk_rugcheck(mint):
    """Returnerer (ok, grunn, detaljer, eiere_for_bundlesjekk)."""
    r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=40)
    if not r or "topHolders" not in r:
        return False, "RugCheck svarte ikke (kan ikke bekrefte sikkerhet)", {}, []
    if r.get("rugged"):
        return False, "merket som rug", {}, []
    token = r.get("token") or {}
    if (r.get("mintAuthority") or token.get("mintAuthority")):
        return False, "utvikler kan lage nye tokens (mint authority)", {}, []
    if (r.get("freezeAuthority") or token.get("freezeAuthority")):
        return False, "utvikler kan fryse lommebøker (freeze authority)", {}, []
    risiko = r.get("risks") or []
    farlige = [x.get("name", "?") for x in risiko if x.get("level") == "danger"]
    if farlige:
        return False, "RugCheck fare: " + ", ".join(farlige[:3]), {}, []
    poeng = tall(r.get("score_normalised"), 100)
    if poeng > MAKS_RUGCHECK:
        return False, f"RugCheck-poeng {poeng:.0f}", {}, []

    # Likviditetspooler og kjente kontoer skal ikke telle som "holdere".
    pooler = set((r.get("knownAccounts") or {}).keys())
    for m in r.get("markets") or []:
        pooler.add(m.get("pubkey"))
        for felt in ("liquidityA", "liquidityB"):
            pooler.add(m.get(felt))
        for felt in ("liquidityAAccount", "liquidityBAccount"):
            pooler.add((m.get(felt) or {}).get("owner"))
    holdere = [h for h in (r.get("topHolders") or [])
               if h.get("owner") not in pooler and h.get("address") not in pooler]
    if not holdere:
        return False, "fant ingen holdere", {}, []
    storste = max(tall(h.get("pct")) for h in holdere)
    topp10 = sum(tall(h.get("pct")) for h in holdere[:10])
    insider = sum(tall(h.get("pct")) for h in holdere if h.get("insider"))
    forsyning = tall(token.get("supply"))
    dev = tall(r.get("creatorBalance")) / forsyning * 100 if forsyning > 0 else 0.0

    # Lommebok-nettverk: grupper av lommebøker som har sendt tokens til hverandre.
    nettverk_liste = r.get("insiderNetworks") or []
    nettverk = (sum(tall(n.get("currentHolding")) for n in nettverk_liste) / forsyning * 100
                if forsyning > 0 else 0.0)

    d = {"rugcheck": poeng, "storste": storste, "topp10": topp10, "dev": dev,
         "insider": insider, "nettverk": nettverk, "nettverk_antall": len(nettverk_liste),
         "advarsler": [x.get("name", "?") for x in risiko if x.get("level") == "warn"][:3],
         "skatt": tall((r.get("transferFee") or {}).get("pct"))}
    if dev > MAKS_DEV_ANDEL:
        return False, f"utvikler eier {dev:.1f} %", d, []
    if storste > MAKS_STORSTE_HOLDER:
        return False, f"største holder eier {storste:.1f} %", d, []
    if topp10 > MAKS_TOPP10:
        return False, f"topp 10 eier {topp10:.0f} %", d, []
    if insider > MAKS_INSIDER_ANDEL:
        return False, f"innsidere eier {insider:.0f} %", d, []
    if nettverk > MAKS_NETTVERK_ANDEL:
        return False, f"{len(nettverk_liste)} lommebok-nettverk eier {nettverk:.1f} %", d, []
    eiere = [h.get("owner") for h in holdere[:5] if h.get("owner")]
    return True, "ok", d, eiere


def sjekk_bundle(eiere):
    """Bundle-tegnet fra video 1: topp-holderne har nesten lik SOL-saldo.
    Returnerer 'ok', 'bundle' eller 'ukjent' (hvis Solana-noden ikke svarer)."""
    if len(eiere) < 4:
        return "ukjent"
    svar = http_json(SOLANA_RPC_URL, data={
        "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [eiere, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]})
    try:
        saldoer = [(a or {}).get("lamports", 0) / 1e9 for a in svar["result"]["value"]]
    except Exception:
        return "ukjent"
    if len(saldoer) < 4:
        return "ukjent"
    median = statistics.median(saldoer)
    if median <= 0:
        return "ok"
    like = sum(1 for s in saldoer if abs(s - median) <= 0.15 * median)
    return "bundle" if like >= 4 else "ok"


# ------------------------- finne kandidater -------------------------
def hent_kandidater(t):
    stier = ["/networks/solana/trending_pools?page=1&include=base_token",
             "/networks/solana/trending_pools?page=2&include=base_token",
             "/networks/solana/pools?page=1&sort=h24_volume_usd_desc&include=base_token",
             "/networks/solana/pools?page=2&sort=h24_volume_usd_desc&include=base_token"]
    beste, sett_totalt = {}, 0
    for sti in stier:
        svar = gt(sti)
        if not svar:
            continue
        navn = {i.get("id"): i.get("attributes") or {} for i in svar.get("included") or []}
        for p in svar.get("data") or []:
            sett_totalt += 1
            a = p.get("attributes") or {}
            tid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            mint = tid.split("_", 1)[-1]
            if not mint or mint in IKKE_MEME:
                continue
            try:
                opprettet = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            k = {"mint": mint, "pool": a.get("address"),
                 "symbol": (navn.get(tid) or {}).get("symbol") or (a.get("name") or "?").split(" / ")[0],
                 "navn": (navn.get(tid) or {}).get("name") or "",
                 "alder_dager": (time.time() - opprettet) / 86400,
                 "mcap": tall(a.get("market_cap_usd")) or tall(a.get("fdv_usd")),
                 "likviditet": tall(a.get("reserve_in_usd")),
                 "volum24": tall((a.get("volume_usd") or {}).get("h24")),
                 "kjopere": tall(((a.get("transactions") or {}).get("h24") or {}).get("buyers")),
                 "selgere": tall(((a.get("transactions") or {}).get("h24") or {}).get("sellers"))}
            if not k["pool"]:
                continue
            if k["alder_dager"] < MIN_ALDER_DAGER:
                continue
            if not (MIN_MCAP <= k["mcap"] <= MAKS_MCAP):
                continue
            if k["likviditet"] < MIN_LIKVIDITET or k["volum24"] < MIN_VOLUM_24T:
                continue
            if naa() - t["sist_varslet"].get(mint, 0) < PAUSE_SAMME_COIN_TIMER * 3600:
                continue
            if mint not in beste or k["likviditet"] > beste[mint]["likviditet"]:
                beste[mint] = k
    ut = sorted(beste.values(), key=lambda k: -k["volum24"])[:MAKS_KANDIDATER]
    print(f"Fant {sett_totalt} pooler, {len(beste)} besto grovfilteret, analyserer {len(ut)}.")
    return ut, sett_totalt


def hent_lys(pool):
    svar = gt(f"/networks/solana/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000&currency=usd")
    try:
        lys = [[tall(x) for x in rad] for rad in svar["data"]["attributes"]["ohlcv_list"]]
    except Exception:
        return []
    return sorted((c for c in lys if len(c) >= 6), key=lambda c: c[0])


def hent_priser(minter):
    priser, minter = {}, list(dict.fromkeys(minter))
    for i in range(0, len(minter), 30):
        svar = gt("/simple/networks/solana/token_price/" + ",".join(minter[i:i + 30]))
        try:
            for m, p in svar["data"]["attributes"]["token_prices"].items():
                if tall(p) > 0:
                    priser[m] = tall(p)
        except Exception:
            pass
    return priser


def poengsum(k, g, s):
    p = min(g["utvaskinger"], 4) * 8                                   # maks 32
    p += max(0.0, (MAKS_RUGCHECK - s["rugcheck"]) / MAKS_RUGCHECK) * 20  # maks 20
    p += min(k["likviditet"] / k["mcap"] / 0.10, 1.0) * 15              # maks 15
    handlere = k["kjopere"] + k["selgere"]
    if handlere > 0:
        p += max(0.0, min((k["kjopere"] / handlere - 0.4) / 0.2, 1.0)) * 15  # maks 15
    p += min(g["volum_hopp"] / 4.0, 1.0) * 18                           # maks 18
    return round(p)


# ------------------------- varsler -------------------------
def kjopsvarsel(t, k, g, s, bundle):
    vid = t["neste_id"]
    t["neste_id"] += 1
    poeng = poengsum(k, g, s)
    mint, e = k["mint"], html.escape
    linjer = [
        f"🟢 <b>KANDIDAT #{vid}: {e(k['symbol'])}</b> — poeng {poeng}/100",
        e(k["navn"]) if k["navn"] else None,
        f"Markedsverdi {usd(k['mcap'])} · likviditet {usd(k['likviditet'])} · alder {k['alder_dager']:.0f} d",
        f"Fall fra topp: −{g['fall']:.0%} (toppen var for {g['timer_siden_topp']:.0f} t siden)",
        f"Utvaskinger overlevd: {g['utvaskinger']} · volum siste 72 t: {g['volum_hopp']:.1f}x normalt",
        f"Holdere: størst {s['storste']:.1f} %, topp 10 {s['topp10']:.0f} %, dev {s['dev']:.1f} %",
        f"Lommebok-nettverk: {s['nettverk_antall']} stk eier {s['nettverk']:.1f} %",
        f"RugCheck: {s['rugcheck']:.0f}/100 (lavere er tryggere)"
        + (f" · advarsler: {e(', '.join(s['advarsler']))}" if s["advarsler"] else ""),
        "Bundle-sjekk: " + ("OK" if bundle == "ok" else "ikke sjekket (Solana-noden svarte ikke)"),
        f"Skatt på overføring: {s['skatt']:.1f} %" if s["skatt"] > 0 else None,
        f"Maks innsats etter grensen din: {usd(PORTEFOLJE_USD * MAKS_ANDEL_PER_KJOP)}",
        "",
        f"CA (trykk for å kopiere):\n<code>{mint}</code>",
        "",
        f"Sjekk selv før du gjør noe: "
        f"<a href=\"https://x.com/search?q={mint}&f=live\">narrativ på X</a> · "
        f"<a href=\"https://dexscreener.com/solana/{mint}\">graf</a> · "
        f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a>",
        f"Kjøpte du? Send /kjopt {vid}",
        "<i>Filtertreff, ikke kjøpsråd. Kan gå til null.</i>",
    ]
    send("\n".join(x for x in linjer if x is not None))
    t["varsler"].append({"id": vid, "mint": mint, "symbol": k["symbol"], "pris": g["pris"],
                         "tid": naa(), "maks": 1.0, "min": 1.0, "utfall": "åpen"})
    t["sist_varslet"][mint] = naa()
    t["dag"]["antall"] += 1


def folg_opp(t):
    """Oppdaterer testloggen og sender innsats-ut/stopp for coins du eier."""
    grense = naa() - TESTLOGG_DAGER * 86400
    aktive = [v for v in t["varsler"] if v["tid"] >= grense and v["utfall"] == "åpen"]
    minter = [v["mint"] for v in aktive] + list(t["posisjoner"].keys())
    if not minter:
        return {}
    priser = hent_priser(minter)
    for v in aktive:
        p = priser.get(v["mint"])
        if not p or v["pris"] <= 0:
            continue
        x = p / v["pris"]
        v["maks"] = round(max(v["maks"], x), 2)
        v["min"] = round(min(v["min"], x), 2)
        if x >= INNSATS_UT_VED:
            v["utfall"] = "2x først"
        elif x <= 1 - STOPP_VED_FALL:
            v["utfall"] = "stopp først"
    for v in t["varsler"]:
        if v["tid"] < grense and v["utfall"] == "åpen":
            v["utfall"] = "ingen av delene"
    t["varsler"] = t["varsler"][-200:]

    for mint, pos in t["posisjoner"].items():
        p = priser.get(mint)
        if not p or pos["pris"] <= 0:
            continue
        x = p / pos["pris"]
        if x >= INNSATS_UT_VED and not pos.get("varslet_ut"):
            pos["varslet_ut"] = True
            send(f"💰 <b>{html.escape(pos['symbol'])}</b> er {x:.1f}x fra kjøpsprisen din.\n"
                 f"Regelen din: ta ut innsatsen nå og la resten stå som moon bag.\n"
                 f"<code>{mint}</code>")
        elif x <= 1 - STOPP_VED_FALL and not pos.get("varslet_stopp"):
            pos["varslet_stopp"] = True
            send(f"🔴 <b>{html.escape(pos['symbol'])}</b> er {1 - x:.0%} under kjøpsprisen din.\n"
                 f"Regelen din: stopp ved −{STOPP_VED_FALL:.0%}.\n"
                 f"Solgte du? Send /solgt {html.escape(pos['symbol'])}\n<code>{mint}</code>")
    return priser


# ------------------------- kommandoer fra deg i Telegram -------------------------
# ------------------------- /analyse: rapport på én coin -------------------------
def _ordtreff(sok, navn):
    """Hvor mange av søkeordene som finnes i navnet. Tåler små skrivefeil (jeff/jef)."""
    navneord = [n for n in navn.lower().replace("/", " ").split() if n]
    treff = 0
    for w in sok.lower().split():
        if any(n == w or (len(w) >= 3 and len(n) >= 3 and (n.startswith(w) or w.startswith(n)))
               for n in navneord):
            treff += 1
    return treff


def rugcheck_detaljer(mint):
    r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=40)
    if not r or "topHolders" not in r:
        return None
    token = r.get("token") or {}
    pooler = set((r.get("knownAccounts") or {}).keys())
    for m in r.get("markets") or []:
        pooler.update([m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")])
        for felt in ("liquidityAAccount", "liquidityBAccount"):
            pooler.add((m.get(felt) or {}).get("owner"))
    pooler.discard(None)
    alle = r.get("topHolders") or []
    holdere = [h for h in alle if h.get("owner") not in pooler and h.get("address") not in pooler]
    forsyning = tall(token.get("supply"))
    nett = r.get("insiderNetworks") or []
    risiko = r.get("risks") or []
    return {
        "rugged": bool(r.get("rugged")),
        "mint": bool(r.get("mintAuthority") or token.get("mintAuthority")),
        "freeze": bool(r.get("freezeAuthority") or token.get("freezeAuthority")),
        "poeng": tall(r.get("score_normalised"), 100),
        "fare": [x.get("name", "?") for x in risiko if x.get("level") == "danger"],
        "advarsler": [x.get("name", "?") for x in risiko if x.get("level") == "warn"],
        "storste": max((tall(h.get("pct")) for h in holdere), default=0.0),
        "topp10": sum(tall(h.get("pct")) for h in holdere[:10]),
        "pool": sum(tall(h.get("pct")) for h in alle if h not in holdere),
        "dev": tall(r.get("creatorBalance")) / forsyning * 100 if forsyning > 0 else 0.0,
        "nettverk": (sum(tall(n.get("currentHolding")) for n in nett) / forsyning * 100
                     if forsyning > 0 else 0.0),
        "nettverk_antall": len(nett),
        "holdere": int(tall(r.get("totalHolders"))),
        "skatt": tall((r.get("transferFee") or {}).get("pct")),
    }


def analyse_tekst(sok):
    sok, e = sok.strip(), html.escape
    if not sok:
        return "Skriv navnet eller adressen etter kommandoen, f.eks. /analyse holdoween"
    svar = http_json("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(sok))
    if svar is None:
        return "Fikk ikke kontakt med DexScreener. Prøv igjen om litt."
    beste = {}  # én rad per coin: poolen med mest likviditet
    for p in svar.get("pairs") or []:
        b = p.get("baseToken") or {}
        m = b.get("address")
        if p.get("chainId") != "solana" or not m or m in IKKE_MEME:
            continue
        if m not in beste or tall((p.get("liquidity") or {}).get("usd")) > \
                tall((beste[m].get("liquidity") or {}).get("usd")):
            beste[m] = p
    if not beste:
        return f"Fant ingen Solana-coin som matcher «{e(sok)}»."

    def treff(p):
        b = p["baseToken"]
        if b["address"] == sok:
            return 99
        return _ordtreff(sok, f"{b.get('name', '')} {b.get('symbol', '')}")

    def vol(p):
        return tall((p.get("volume") or {}).get("h24"))

    topp = max(treff(p) for p in beste.values())
    like = sorted((p for p in beste.values() if treff(p) == topp), key=vol, reverse=True)
    p, andre = like[0], like[1:]
    b, mint = p["baseToken"], p["baseToken"]["address"]
    mcap = tall(p.get("marketCap")) or tall(p.get("fdv"))
    likv = tall((p.get("liquidity") or {}).get("usd"))
    tx = (p.get("txns") or {}).get("h24") or {}
    endr = p.get("priceChange") or {}
    alder_t = (time.time() - tall(p.get("pairCreatedAt")) / 1000) / 3600 if p.get("pairCreatedAt") else None
    alder = "ukjent" if alder_t is None else (f"{alder_t:.1f} timer" if alder_t < 48 else f"{alder_t / 24:.0f} dager")

    ut = [f"🔎 <b>Analyse: {e(b.get('name', '?'))} ({e(b.get('symbol', '?'))})</b>",
          f"<code>{mint}</code>", "",
          "<b>Marked</b>",
          f"Alder: {alder} · markedsverdi {usd(mcap)} · likviditet {usd(likv)}"
          + (f" ({likv / mcap:.0%} av markedsverdien)" if mcap > 0 else ""),
          f"Volum 24 t: {usd(vol(p))} · kjøp/salg 24 t: {tall(tx.get('buys')):.0f}/{tall(tx.get('sells')):.0f}",
          f"Kurs: {tall(endr.get('h1')):+.0f} % siste time, {tall(endr.get('h24')):+.0f} % siste døgn"]

    graf = analyser_graf(hent_lys(p.get("pairAddress") or ""))[2]
    if graf:
        ut.append(f"Fall fra topp siste 72 t: −{graf['fall']:.0%} · utvaskinger overlevd: {graf['utvaskinger']}")
    else:
        ut.append("Graf: for kort historikk til å telle utvaskinger")

    rc = rugcheck_detaljer(mint)
    brudd = []
    ut += ["", "<b>Rug-risiko</b>"]
    if rc is None:
        ut.append("RugCheck svarte ikke, så dette er ikke sjekket.")
        brudd.append("sikkerhet ikke bekreftet")
    else:
        ut.append(f"RugCheck: {rc['poeng']:.0f}/100 (lavere er tryggere)"
                  + (" · MERKET SOM RUG" if rc["rugged"] else ""))
        ut.append("Kan lage nye tokens: " + ("JA" if rc["mint"] else "nei")
                  + " · kan fryse lommebøker: " + ("JA" if rc["freeze"] else "nei")
                  + (f" · skatt {rc['skatt']:.1f} %" if rc["skatt"] > 0 else ""))
        if rc["fare"]:
            ut.append("Fare: " + e(", ".join(rc["fare"][:4])))
        if rc["advarsler"]:
            ut.append("Advarsler: " + e(", ".join(rc["advarsler"][:4])))
        ut += ["", "<b>Hvem eier</b>",
               f"Holdere: {rc['holdere']} · utvikler eier {rc['dev']:.1f} %",
               f"Største holder: {rc['storste']:.1f} % · topp 10: {rc['topp10']:.0f} % "
               f"(likviditetspool holder {rc['pool']:.0f} % i tillegg)",
               f"Lommebok-nettverk: {rc['nettverk_antall']} stk eier {rc['nettverk']:.1f} %"]
        if rc["rugged"] or rc["mint"] or rc["freeze"] or rc["fare"]:
            brudd.append("alvorlig RugCheck-funn")
        if rc["poeng"] > MAKS_RUGCHECK:
            brudd.append(f"RugCheck over {MAKS_RUGCHECK}")
        if rc["dev"] > MAKS_DEV_ANDEL:
            brudd.append("utvikler eier for mye")
        if rc["storste"] > MAKS_STORSTE_HOLDER:
            brudd.append("for stor enkeltholder")
        if rc["topp10"] > MAKS_TOPP10:
            brudd.append("topp 10 eier for mye")
        if rc["nettverk"] > MAKS_NETTVERK_ANDEL:
            brudd.append("lommebok-nettverk eier for mye")
    if alder_t is not None and alder_t < MIN_ALDER_DAGER * 24:
        brudd.append(f"yngre enn {MIN_ALDER_DAGER} dager")
    if not (MIN_MCAP <= mcap <= MAKS_MCAP):
        brudd.append("markedsverdi utenfor 300k–10M")
    if likv < MIN_LIKVIDITET:
        brudd.append("for lite likviditet")
    if vol(p) < MIN_VOLUM_24T:
        brudd.append("for lite handel")
    if graf and graf["utvaskinger"] < MIN_UTVASKINGER:
        brudd.append(f"under {MIN_UTVASKINGER} utvaskinger")

    info = p.get("info") or {}
    lenker = [f"<a href=\"{e(w.get('url', ''))}\">nettside</a>" for w in (info.get("websites") or [])[:2]
              if w.get("url")]
    lenker += [f"<a href=\"{e(s.get('url', ''))}\">{e(s.get('type') or 'sosial')}</a>"
               for s in (info.get("socials") or [])[:3] if s.get("url")]
    ut += ["", "<b>Narrativ</b>",
           "Boten kan ikke vurdere historien bak coinen. Les selv:",
           (" · ".join(lenker) if lenker else "Prosjektet har ikke oppgitt nettside eller sosiale lenker.")
           + f" · <a href=\"https://x.com/search?q={mint}&f=live\">søk på X</a>"
           + f" · <a href=\"https://dexscreener.com/solana/{mint}\">graf</a>"]
    if andre:
        ut.append(f"OBS: andre Solana-coins med samme navn: {len(andre)}. Sjekk adressen før du handler.")
        for a in andre[:3]:
            ab = a["baseToken"]
            ut.append(f"  {e(ab.get('symbol', '?'))} …{ab['address'][-8:]} · "
                      f"{usd(tall(a.get('marketCap')) or tall(a.get('fdv')))}")

    ut += ["", "<b>Mot botens regler</b>",
           ("Faller på: " + ", ".join(brudd)) if brudd else
           "Består størrelse, alder og sikkerhet. Timing (fall fra topp) avgjør om boten ville varslet.",
           "<i>Rapport fra offentlige data, ikke kjøpsråd. Kan gå til null.</i>"]
    return "\n".join(ut)


HJELP = ("<b>Kommandoer</b>\n"
         "/analyse navn – rapport på én coin (navn eller adresse)\n"
         "/kjopt 12 – jeg kjøpte kandidat #12 (boten følger den videre)\n"
         "/solgt 12 – jeg har solgt, slutt å følge den\n"
         "/status – coins du eier og hvordan de står\n"
         "/logg – testlogg: hvordan varslene ville gått\n"
         "/hjelp – denne lista")


def finn_varsel(t, arg):
    arg = arg.lstrip("#").strip()
    for v in reversed(t["varsler"]):
        if str(v["id"]) == arg or v["mint"] == arg or v["symbol"].lower() == arg.lower():
            return v
    return None


def les_kommandoer(t):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    svar = http_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                     f"?offset={t['tg_offset']}&timeout=0")
    if not svar or not svar.get("ok"):
        return
    for u in svar.get("result", []):
        t["tg_offset"] = max(t["tg_offset"], u.get("update_id", 0) + 1)
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != TELEGRAM_CHAT_ID:
            continue  # boten svarer bare deg
        deler = (m.get("text") or "").strip().split(maxsplit=1)
        if not deler:
            continue
        kmd, arg = deler[0].lower().split("@")[0], (deler[1] if len(deler) > 1 else "")
        if kmd in ("/kjopt", "/kjøpt"):
            v = finn_varsel(t, arg)
            if not v:
                send("Fant ikke den kandidaten. Bruk nummeret fra varselet, f.eks. /kjopt 12")
                continue
            pris = hent_priser([v["mint"]]).get(v["mint"]) or v["pris"]
            t["posisjoner"][v["mint"]] = {"symbol": v["symbol"], "pris": pris, "tid": naa()}
            send(f"Notert: {html.escape(v['symbol'])} kjøpt til ca. ${pris:.3g}. "
                 f"Jeg varsler ved {INNSATS_UT_VED:.0f}x og ved −{STOPP_VED_FALL:.0%}.")
        elif kmd == "/solgt":
            v = finn_varsel(t, arg)
            mint = v["mint"] if v else arg.strip()
            if mint in t["posisjoner"]:
                pos = t["posisjoner"].pop(mint)
                send(f"Notert: {html.escape(pos['symbol'])} er solgt. Jeg følger den ikke lenger.")
            else:
                send("Fant ingen åpen posisjon med det nummeret.")
        elif kmd == "/status":
            if not t["posisjoner"]:
                send("Du har ingen coins merket som kjøpt.")
                continue
            priser = hent_priser(list(t["posisjoner"].keys()))
            rader = []
            for mint, pos in t["posisjoner"].items():
                p = priser.get(mint)
                endring = f"{(p / pos['pris'] - 1):+.0%}" if p and pos["pris"] > 0 else "ukjent"
                rader.append(f"{html.escape(pos['symbol'])}: {endring}")
            send("<b>Dine posisjoner</b>\n" + "\n".join(rader))
        elif kmd == "/logg":
            send(testlogg_tekst(t))
        elif kmd in ("/analyse", "/analyser", "/analyze"):
            try:
                send(analyse_tekst(arg))
            except Exception as feil:
                print("Feil i /analyse:", repr(feil))
                send("Analysen feilet denne gangen. Prøv igjen, eller send adressen i stedet for navnet.")
        elif kmd in ("/hjelp", "/help", "/start"):
            send(HJELP)


def testlogg_tekst(t):
    v = t["varsler"]
    if not v:
        return "Testloggen er tom – boten har ikke sendt noen kjøpsvarsler ennå."
    toX = sum(1 for x in v if x["utfall"] == "2x først")
    stopp = sum(1 for x in v if x["utfall"] == "stopp først")
    ingen = sum(1 for x in v if x["utfall"] == "ingen av delene")
    apne = sum(1 for x in v if x["utfall"] == "åpen")
    rader = [f"#{x['id']} {html.escape(x['symbol'])}: {x['utfall']} "
             f"(høyest {x['maks']:.1f}x, lavest {x['min']:.2f}x)" for x in v[-10:]]
    return (f"<b>Testlogg ({len(v)} varsler)</b>\n"
            f"Nådde {INNSATS_UT_VED:.0f}x før stopp: {toX}\n"
            f"Traff stopp (−{STOPP_VED_FALL:.0%}) først: {stopp}\n"
            f"Ingen av delene på {TESTLOGG_DAGER} dager: {ingen}\n"
            f"Fortsatt åpne: {apne}\n\n" + "\n".join(rader))


# ------------------------- hovedløp -------------------------
def main():
    t = les_tilstand()
    for_tekst = json.dumps(t, ensure_ascii=False, indent=1, sort_keys=True)
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("OBS: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID mangler – varsler skrives bare her.")

    if not t["hilst"]:
        if send("✅ Trygg først-boten er koblet til og kjører.\n\n" + HJELP +
                "\n\nDen er streng, så det kan gå dager mellom varsler."):
            t["hilst"] = True

    les_kommandoer(t)
    folg_opp(t)

    if t["dag"]["dato"] != dagens_dato():
        t["dag"] = {"dato": dagens_dato(), "antall": 0}

    kandidater, sett_totalt = hent_kandidater(t)
    if sett_totalt == 0 and naa() - t["sist_feilmelding"] > 6 * 3600:
        t["sist_feilmelding"] = naa()
        send("⚠️ Boten fikk ikke hentet data fra GeckoTerminal denne runden. "
             "Den prøver igjen automatisk.")

    for k in kandidater:
        if t["dag"]["antall"] >= MAKS_VARSLER_PER_DAG:
            print("Dagsgrensen for varsler er nådd.")
            break
        ok, grunn, g = analyser_graf(hent_lys(k["pool"]))
        if not ok:
            print(f"  {k['symbol']}: forkastet – {grunn}")
            continue
        ok, grunn, s, eiere = sjekk_rugcheck(k["mint"])
        if not ok:
            print(f"  {k['symbol']}: forkastet – {grunn}")
            continue
        bundle = sjekk_bundle(eiere)
        if bundle == "bundle":
            print(f"  {k['symbol']}: forkastet – topp-holderne har nesten lik SOL-saldo (bundle)")
            continue
        print(f"  {k['symbol']}: BESTO ALT – sender varsel")
        kjopsvarsel(t, k, g, s, bundle)

    lagre_tilstand(t, for_tekst)
    print("Ferdig.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # siste sikkerhetsnett: aldri la kjøringen feile stille
        print("Uventet feil:", repr(e))
        sys.exit(1)
