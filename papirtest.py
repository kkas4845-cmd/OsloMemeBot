#!/usr/bin/env python3
"""
Papirtest: grensetest av boten med fiktive kroner.

Kjører sammenhengende i TIMER timer, sjekker prisene hvert minutt og leter
etter nye kjøp hvert 3. minutt. Alle kjøp og salg er på papir. Ingen ekte
penger flyttes, og boten kan ikke handle i fomo.

Reglene her er bevisst mye løsere enn i bot.py, slik at det faktisk blir
handler på noen få timer. Sikkerhetssjekkene som avgjør om en coin i det hele
tatt kan selges (rug, mint/freeze, likviditet) er beholdt.
"""
import html
import json
import os
import time

import bot

# ============================ INNSTILLINGER ============================
START_KR = 1000        # fiktive kroner
TIMER = 5              # hvor lenge testen varer
ANDEL_PER_KJOP = 0.20  # 20 % av saldoen per kjøp
MAKS_POSISJONER = 5    # maks antall coins samtidig
GEBYR = 0.015          # 1,5 % per handel (gebyr + kursglidning, anslag)

MIN_LIKVIDITET = 20_000   # dollar
MIN_MCAP = 50_000         # dollar
MIN_ALDER_MIN = 30        # coinen må være minst 30 minutter gammel
MIN_VOLUM_1T = 20_000     # handel siste time, dollar
MAKS_STORSTE_HOLDER = 15  # prosent, utenom likviditetspool

FALL_MIN = 0.30        # minst 30 % ned fra toppen siste 6 timer
FALL_MAKS = 0.60       # mer enn 60 % ned regnes som kollaps
SPRETT = 0.05          # må ha løftet seg 5 % fra bunnen siste 30 min

TA_GEVINST = 1.5       # selg halvparten ved +50 %
STOPP = 0.15           # selg alt ved −15 %
SLEP = 0.25            # etter gevinst: selg resten ved −25 % fra toppen

SKANN_HVERT_SEK = 180
INGEN_KJOP_SISTE_MIN = 15   # ikke åpne nye posisjoner helt på slutten
PAUSE_SAMME_COIN_MIN = 90
# =======================================================================

FIL = os.environ.get("PAPIR_FIL", "papir.json")
naa = time.time
sov = time.sleep
e = html.escape


def kr(x):
    return f"{x:,.0f} kr".replace(",", " ")


def les():
    try:
        with open(FIL, encoding="utf-8") as f:
            p = json.load(f)
        if not p.get("ferdig") and p.get("slutt", 0) > naa():
            return p  # fortsetter en test som ble avbrutt
    except Exception:
        pass
    return {"start": naa(), "slutt": naa() + TIMER * 3600, "kontanter": float(START_KR),
            "posisjoner": {}, "handler": [], "pause": {}, "forkastet": {},
            "sist_skann": 0, "sist_status": naa(), "ferdig": False, "ny": True}


def lagre(p):
    with open(FIL, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)


def verdi(p):
    return p["kontanter"] + sum(x["enheter"] * x["sist_pris"] for x in p["posisjoner"].values())


def tell(p, grunn):
    p["forkastet"][grunn] = p["forkastet"].get(grunn, 0) + 1


def selg(p, mint, andel, pris, grunn):
    x = p["posisjoner"][mint]
    enh = x["enheter"] * andel
    inn = enh * pris * (1 - GEBYR)
    kost = x["kost"] * andel
    x["enheter"] -= enh
    x["kost"] -= kost
    p["kontanter"] += inn
    p["handler"].append({"tid": naa(), "type": grunn, "symbol": x["symbol"], "mint": mint,
                         "x": pris / x["pris"], "resultat": inn - kost})
    tegn = "🟢" if inn >= kost else "🔴"
    bot.send(f"{tegn} <b>PAPIR: solgte {'halve ' if andel < 1 else ''}{e(x['symbol'])}</b> ({grunn})\n"
             f"Kurs {pris / x['pris']:.2f}x av kjøp · resultat {inn - kost:+.0f} kr\n"
             f"Saldo nå: {kr(verdi(p))}")
    if x["enheter"] <= 1e-12:
        del p["posisjoner"][mint]
        p["pause"][mint] = naa() + PAUSE_SAMME_COIN_MIN * 60


def folg(p):
    """Sjekker prisene på det vi eier og selger etter reglene."""
    if not p["posisjoner"]:
        return
    priser = bot.hent_priser(list(p["posisjoner"].keys()))
    for mint in list(p["posisjoner"].keys()):
        x = p["posisjoner"][mint]
        pris = priser.get(mint)
        if not pris:
            continue
        x["sist_pris"] = pris
        x["topp"] = max(x["topp"], pris)
        forhold = pris / x["pris"]
        if not x["tatt"]:
            if forhold <= 1 - STOPP:
                selg(p, mint, 1, pris, "stopp")
            elif forhold >= TA_GEVINST:
                x["tatt"] = True
                selg(p, mint, 0.5, pris, "gevinst")
        elif pris <= x["topp"] * (1 - SLEP):
            selg(p, mint, 1, pris, "slepestopp")


def trygg(mint):
    """Minimumssjekk: kan coinen i det hele tatt selges igjen?"""
    r = bot.http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=40)
    if not r or "topHolders" not in r:
        return False, "RugCheck svarte ikke"
    token = r.get("token") or {}
    if r.get("rugged"):
        return False, "merket som rug"
    if r.get("mintAuthority") or token.get("mintAuthority"):
        return False, "mint authority"
    if r.get("freezeAuthority") or token.get("freezeAuthority"):
        return False, "freeze authority"
    if any(x.get("level") == "danger" for x in r.get("risks") or []):
        return False, "RugCheck fare"
    pooler = set((r.get("knownAccounts") or {}).keys())
    for m in r.get("markets") or []:
        pooler.update([m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")])
    holdere = [h for h in r.get("topHolders") or []
               if h.get("owner") not in pooler and h.get("address") not in pooler]
    if holdere and max(bot.tall(h.get("pct")) for h in holdere) > MAKS_STORSTE_HOLDER:
        return False, "for stor enkeltholder"
    return True, "ok"


def kandidater(p):
    funnet = {}
    for sti in ("/networks/solana/trending_pools?page=1&duration=1h&include=base_token",
                "/networks/solana/trending_pools?page=1&include=base_token",
                "/networks/solana/trending_pools?page=2&include=base_token"):
        svar = bot.gt(sti)
        if not svar:
            continue
        navn = {i.get("id"): i.get("attributes") or {} for i in svar.get("included") or []}
        for pool in svar.get("data") or []:
            a = pool.get("attributes") or {}
            tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            mint = tid.split("_", 1)[-1]
            if not mint or mint in bot.IKKE_MEME or mint in funnet or not a.get("address"):
                continue
            try:
                from datetime import datetime
                alder = (naa() - datetime.fromisoformat(
                    a["pool_created_at"].replace("Z", "+00:00")).timestamp()) / 60
            except Exception:
                continue
            h1 = (a.get("transactions") or {}).get("h1") or {}
            k = {"mint": mint, "pool": a["address"],
                 "symbol": (navn.get(tid) or {}).get("symbol") or (a.get("name") or "?").split(" / ")[0],
                 "mcap": bot.tall(a.get("market_cap_usd")) or bot.tall(a.get("fdv_usd")),
                 "likv": bot.tall(a.get("reserve_in_usd")),
                 "vol1": bot.tall((a.get("volume_usd") or {}).get("h1")),
                 "kjop": bot.tall(h1.get("buys")), "salg": bot.tall(h1.get("sells"))}
            if mint in p["posisjoner"] or p["pause"].get(mint, 0) > naa():
                continue
            if alder < MIN_ALDER_MIN:
                tell(p, "for ny"); continue
            if k["likv"] < MIN_LIKVIDITET or k["mcap"] < MIN_MCAP:
                tell(p, "for liten"); continue
            if k["vol1"] < MIN_VOLUM_1T:
                tell(p, "for lite handel"); continue
            funnet[mint] = k
    return sorted(funnet.values(), key=lambda k: -k["vol1"])[:6]


def skann(p):
    p["sist_skann"] = naa()
    for k in kandidater(p):
        if len(p["posisjoner"]) >= MAKS_POSISJONER:
            return
        svar = bot.gt(f"/networks/solana/pools/{k['pool']}/ohlcv/minute?aggregate=5&limit=72&currency=usd")
        try:
            lys = sorted(([bot.tall(v) for v in rad] for rad in svar["data"]["attributes"]["ohlcv_list"]),
                         key=lambda c: c[0])
        except Exception:
            tell(p, "ingen graf"); continue
        if len(lys) < 6 or lys[-1][4] <= 0:
            tell(p, "ingen graf"); continue
        pris = lys[-1][4]
        fall = 1 - pris / max(c[2] for c in lys)
        bunn = min(c[3] for c in lys[-6:])
        if fall < FALL_MIN:
            tell(p, "ikke falt nok"); continue
        if fall > FALL_MAKS:
            tell(p, "falt for mye"); continue
        if bunn <= 0 or pris / bunn - 1 < SPRETT:
            tell(p, "faller fortsatt"); continue
        ok, grunn = trygg(k["mint"])
        if not ok:
            tell(p, grunn); continue
        innsats = min(p["kontanter"], verdi(p) * ANDEL_PER_KJOP)
        if innsats < 20:
            tell(p, "ingen kroner igjen"); return
        pris = bot.hent_priser([k["mint"]]).get(k["mint"]) or pris
        p["kontanter"] -= innsats
        p["posisjoner"][k["mint"]] = {"symbol": k["symbol"], "pris": pris, "sist_pris": pris, "topp": pris,
                                      "enheter": innsats * (1 - GEBYR) / pris, "kost": innsats,
                                      "tatt": False, "tid": naa()}
        p["handler"].append({"tid": naa(), "type": "kjøp", "symbol": k["symbol"], "mint": k["mint"],
                             "x": 1.0, "resultat": 0.0})
        bot.send(f"🛒 <b>PAPIR: kjøpte {e(k['symbol'])}</b> for {kr(innsats)}\n"
                 f"−{fall:.0%} fra topp siste 6 t · markedsverdi {bot.usd(k['mcap'])} · "
                 f"likviditet {bot.usd(k['likv'])}\n<code>{k['mint']}</code>")


def toppgrunner(p, n=4):
    par = sorted(p["forkastet"].items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{g} ({a})" for g, a in par) or "ingen"


def status(p):
    p["sist_status"] = naa()
    igjen = max(0, p["slutt"] - naa()) / 3600
    apne = ", ".join(f"{e(x['symbol'])} {x['sist_pris'] / x['pris']:.2f}x" for x in p["posisjoner"].values())
    bot.send(f"⏱ <b>Papirtest: {igjen:.1f} t igjen</b>\nSaldo {kr(verdi(p))} "
             f"({verdi(p) / START_KR - 1:+.1%})\nÅpne: {apne or 'ingen'}\n"
             f"Forkastet mest for: {toppgrunner(p)}")


def avslutt(p):
    folg(p)
    priser = bot.hent_priser(list(p["posisjoner"].keys())) if p["posisjoner"] else {}
    for mint in list(p["posisjoner"].keys()):
        selg(p, mint, 1, priser.get(mint) or p["posisjoner"][mint]["sist_pris"], "tiden er ute")
    salg = [h for h in p["handler"] if h["type"] != "kjøp"]
    kjop = [h for h in p["handler"] if h["type"] == "kjøp"]
    vinn = sum(1 for h in salg if h["resultat"] > 0)
    slutt = p["kontanter"]
    linjer = [f"🏁 <b>Papirtesten er ferdig</b>",
              f"Start {kr(START_KR)} → slutt {kr(slutt)} ({slutt / START_KR - 1:+.1%})",
              f"Kjøp: {len(kjop)} · salg: {len(salg)} · salg med gevinst: {vinn}"]
    if salg:
        beste, verst = max(salg, key=lambda h: h["resultat"]), min(salg, key=lambda h: h["resultat"])
        linjer.append(f"Beste: {e(beste['symbol'])} {beste['resultat']:+.0f} kr · "
                      f"dårligste: {e(verst['symbol'])} {verst['resultat']:+.0f} kr")
    linjer.append(f"Forkastet mest for: {toppgrunner(p, 5)}")
    linjer.append("<i>Fiktive kroner og antatt 1,5 % kostnad per handel. Ekte kjøp i fomo ville "
                  "fått dårligere kurser. Fem timer sier lite om strategien.</i>")
    bot.send("\n".join(linjer))
    p["ferdig"] = True


def main():
    p = les()
    if p.pop("ny", False):
        bot.send(f"🚦 <b>Papirtest startet</b>\n{kr(START_KR)} i fiktive penger, {TIMER} timer.\n"
                 f"Kjøper for {ANDEL_PER_KJOP:.0%} av saldoen per coin, maks {MAKS_POSISJONER} samtidig. "
                 f"Selger halvparten ved +{TA_GEVINST - 1:.0%}, alt ved −{STOPP:.0%}.\n"
                 f"Du får melding ved hver handel og status hver time.")
    while naa() < p["slutt"]:
        t0 = naa()
        try:
            folg(p)
            if (naa() - p["sist_skann"] >= SKANN_HVERT_SEK
                    and p["slutt"] - naa() > INGEN_KJOP_SISTE_MIN * 60):
                skann(p)
            if naa() - p["sist_status"] >= 3600:
                status(p)
        except Exception as feil:  # én dårlig runde skal ikke stoppe testen
            print("Feil i runden:", repr(feil))
        lagre(p)
        print(f"{time.strftime('%H:%M:%S')} saldo {verdi(p):.0f} kr, {len(p['posisjoner'])} åpne", flush=True)
        sov(max(5, 60 - (naa() - t0)))
    avslutt(p)
    lagre(p)


if __name__ == "__main__":
    main()
