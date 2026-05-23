#!/usr/bin/env python3
"""
VW ID.4 150kW (77/82 kWh) sledovač inzerátov – auto.bazos.sk
Spustite denne: python3 vw_id4_tracker.py
"""

import re
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

# ── konfigurácia ──────────────────────────────────────────────────────────────
SEARCH_BASE = (
    "https://auto.bazos.sk/?hledat=volkswagen+id4&rubriky=auto"
    "&hlokalita=&humkreis=25&cenaod=21000&cenado=27000"
    "&Submit=H%C4%BEada%C5%A5&order=&kitx=ano&crp={crp}"
)
BASE_URL    = "https://auto.bazos.sk"
DB_PATH     = Path.home() / "vw_id4_tracker.db"
PAGE_SIZE   = 20                        # počet inzerátov na stránku
HEADERS     = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9",
    "Cookie": "bid=29098534; rek=ano",
}
DELAY_BETWEEN_REQUESTS = 1.5   # sekundy medzi požiadavkami
# ─────────────────────────────────────────────────────────────────────────────


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().upper()


# ── databáza ─────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inzeraty (
            id          TEXT PRIMARY KEY,
            url         TEXT,
            nazov       TEXT,
            cena        INTEGER,
            km          INTEGER,
            rocnik      TEXT,
            lokalita    TEXT,
            bateria     TEXT,
            tahac       INTEGER,
            park_asist  INTEGER,
            popis       TEXT,
            prvy_zaznam TEXT,
            posledny    TEXT
        )
    """)
    # Migrácia: pridaj rocnik ak chýba (staršia DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(inzeraty)").fetchall()]
    if "rocnik" not in cols:
        conn.execute("ALTER TABLE inzeraty ADD COLUMN rocnik TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zmeny_cien (
            id          TEXT,
            datum       TEXT,
            cena_stara  INTEGER,
            cena_nova   INTEGER
        )
    """)
    conn.commit()
    return conn


def count_total(html: str) -> int:
    """Zistí celkový počet inzerátov zo stránky výsledkov."""
    m = re.search(r"Vyberajte z (\d+) inzerátov", html)
    return int(m.group(1)) if m else PAGE_SIZE


# ── parsovanie zoznamu ────────────────────────────────────────────────────────
def parse_listing_page(html: str) -> list[dict]:
    """Vráti základné info (id, url, nazov, cena, lokalita) zo stránky výsledkov."""
    pattern = re.compile(
        r'<div class="inzeraty inzeratyflex">(.*?)(?=<div class="inzeraty inzeratyflex"|'
        r'<div class="strankovani"|<div class="listainzerat")',
        re.DOTALL,
    )
    items = []
    for m in pattern.finditer(html):
        block = m.group(1)

        url_m   = re.search(r'href="(/inzerat/(\d+)/[^"]+)"', block)
        nazov_m = re.search(r'class=nadpis><a[^>]+>([^<]+)</a>', block)
        cena_m  = re.search(r'translate="no">\s*([\d\s]+)\s*€', block)
        lok_m   = re.search(r'<div class="inzeratylok">([^<]+)', block)

        if not url_m:
            continue

        ad_id = url_m.group(2)
        url   = BASE_URL + url_m.group(1)
        nazov = nazov_m.group(1).strip() if nazov_m else ""
        cena  = int(re.sub(r"\s", "", cena_m.group(1))) if cena_m else 0
        lok   = lok_m.group(1).strip() if lok_m else ""

        items.append({"id": ad_id, "url": url, "nazov": nazov,
                      "cena": cena, "lokalita": lok})
    return items


def parse_km(text: str) -> int | None:
    """
    Extrahuje najazdené km z textu inzerátu. Zvláda tieto formáty:
      116.000KM   135.000 KM   36 000 KM   KM: 36 000
      NAJAZDENÉ KILOMETRE - 102 000   NÁJAZDOM 116.000KM
      NAJAZDENÝCH MA 125.331 KILOMETROV
    Vylučuje: DOJAZD X-YKKM, záručné limity, batériové referencie.
    """
    def číslica(raw: str) -> int:
        return int(re.sub(r"[\s.,]", "", raw))

    def platné(val: int) -> bool:
        return 100 <= val <= 300_000

    # Normalizácia: nahraď emoji medzerou
    čistý = re.sub(r"[^\w\s.,:/\-]", " ", text)

    # ── Prioritné vzory (explicitné označenie km) ─────────────────────────────
    prioritné = [
        # "NAJAZDENÝCH MA 125.331 KILOMETROV" / "NAJAZDENÝCH 125 331 KM"
        r"NAJAZDENÝ?CH\s+(?:MÁ\s+|MA\s+)?(\d[\d\s.]{1,9})\s*KILOMETROV",
        r"NAJAZDENÝ?CH\s+(?:MÁ\s+|MA\s+)?(\d[\d\s.]{1,9})\s*KM\b",
        # "KM: 36 000" / "KM - 36 000"
        r"\bKM\s*[:\-–]\s*(\d[\d\s.]{1,9})\b",
        # "NÁJAZDOM 116.000KM" / "NÁJAZD: 125.000 KM"
        r"NÁJAZDM?[A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ]*\s*[:\-–]?\s*(\d[\d\s.]{1,9})\s*KM",
        # "NAJAZDENÉ KILOMETRE - 102 000"
        r"NAJAZDENÉ\s+KILOMETRE?\s*[-–:]\s*(\d[\d\s.]{1,9})",
        # "NAJAZDENÉ - 102 000 KM"
        r"NAJAZDENÉ\s*[-–:]\s*(\d[\d\s.]{1,9})\s*KM",
    ]

    for vzor in prioritné:
        m = re.search(vzor, čistý)
        if m:
            try:
                val = číslica(m.group(1))
                if platné(val):
                    return val
            except ValueError:
                pass

    # ── Záložný vzor: číslo pred KM, ale nie v kontexte dojazdu/záruky ───────
    # Vylúčiť: DOJAZD X-YKKM, ZÁRUKA/GARANCIA X.000 KM, BATÉRIA X.000 KM
    vylúčiť = re.compile(
        r"(?:DOJAZD|ZÁRUK|ZARUK|GARANCI|BATÉRI|NABÍJ|NABIT|KAPACIT|WLTP|SPOTREB)"
        r".{0,30}(\d[\d\s.]{2,9})\s*KM",
        re.DOTALL,
    )
    vylúčené_pozície = set()
    for m in vylúčiť.finditer(čistý):
        # Označ pozíciu čísla ako vylúčenú
        vylúčené_pozície.update(range(m.start(1), m.end(1) + 3))

    for m in re.finditer(r"(\d[\d\s.]{2,9})\s*KM\b", čistý):
        if m.start(1) in vylúčené_pozície:
            continue
        try:
            val = číslica(m.group(1))
            if platné(val):
                return val
        except ValueError:
            pass

    return None


def parse_rocnik(text: str) -> str:
    """
    Extrahuje rok výroby / prvé prihlásenie. Vracia napr. '10/2022' alebo '2022'.
    Vzory:
      R.V.: 10/2022    R.V: 04/2021    ROK 12/2021
      PRVÉ PRIHLÁSENIE - 4/2022     3/2021 (voľne v texte)
    """
    # MM/RRRR za kľúčovým slovom (R.V., ROK, PRIHLÁSENIE)
    m = re.search(
        r"(?:R\.?\s*V\.?\s*[:\-–]?\s*|ROK\s+|PRVÉ\s+PRIHLÁSENIE\s*[-–:]\s*)"
        r"(\d{1,2}/20\d{2})",
        text,
    )
    if m:
        return m.group(1)

    # Samotný rok za R.V.
    m = re.search(
        r"(?:R\.?\s*V\.?\s*[:\-–]?\s*)(20\d{2})\b",
        text,
    )
    if m:
        return m.group(1)

    # Dátum vo formáte "M/RRRR" alebo "MM/RRRR" voľne v texte (napr. "3/2021")
    # Berieme prvý výskyt, ktorý dáva zmysel (2018–2024)
    for m in re.finditer(r"\b(\d{1,2}/20(?:1[89]|2[0-4]))\b", text):
        # Vylúčiť ak je to súčasť väčšieho čísla (napr. SOH 94/100)
        return m.group(1)

    return ""


# ── parsovanie detailu ────────────────────────────────────────────────────────
def parse_detail(html: str, meta: dict) -> dict | None:
    """
    Načíta detail inzerátu a vráti obohatený slovník alebo None, ak nespĺňa
    kritériá (150kW + batéria 77/82 kWh).
    """
    # Vyober len hlavný obsah inzerátu (pred sekciou podobných inzerátov)
    mc_start = html.find('class="maincontent"')
    if mc_start >= 0:
        main_html = html[mc_start:]
        similar = main_html.find('class="inzeraty inzeratyflex"')
        if similar > 0:
            main_html = main_html[:similar]
    else:
        main_html = html
    text = normalize(strip_tags(main_html))

    # ── filter: musí ísť o VW ID.4 / ID4 ────────────────────────────────────
    is_id4 = bool(re.search(r"\bID[\s.]?4\b", text[:300]))
    # aj URL môže byť indikátorom
    is_id4 = is_id4 or "id4" in meta["url"].lower() or "id-4" in meta["url"].lower()
    if not is_id4:
        return None

    # ── filter: 150 kW ───────────────────────────────────────────────────────
    has_150kw = bool(re.search(r"150\s*KW", text))
    if not has_150kw:
        return None

    # ── filter: batéria 77/82 kWh (nie menšia Pure) ──────────────────────────
    # Exclude "52 KWH" alebo "PURE" s nižšou kapacitou
    has_small = bool(re.search(r"(?:52|45|58)\s*KWH?", text))
    # Ak sa 77/82 explicitne spomína – bereme
    has_7782  = bool(re.search(r"(?:77|82|80)\s*KWH?", text))
    # "PRO PERFORMANCE" alebo "PRO S" je indikátor veľkej batérie
    has_pro   = bool(re.search(r"PRO\s+PERFORMANCE|PRO\s+S\b", text))

    if has_small:
        return None
    if not has_7782 and not has_pro:
        return None

    bateria = "77/82 kWh (Pro Performance)"

    # ── najazdené kilometre ───────────────────────────────────────────────────
    km = parse_km(text)

    # ── rok výroby ───────────────────────────────────────────────────────────
    rocnik = parse_rocnik(text)

    # ── ťažné zariadenie ─────────────────────────────────────────────────────
    # Slovenský text: "ŤAŽNÉ ZARIADENIE" (Ť nie T) – hľadáme viacero variantov
    tahac = bool(re.search(
        r"T[AÁ]ŽN[EÉ]\s+ZARIADEN"       # ŤAŽNÉ ZARIADENIE (niekedy bez háčka)
        r"|ŤAŽNÉ\s+ZARIADEN"             # so správnym Ť
        r"|ŤAHAČ|TAŽNÉ|TAZNE\b"          # skrátené/bez diakritiky
        r"|TOW\s*BAR|TOWBAR"             # anglické označenie
        r"|ANHÄNGERKUPPLUNG",            # nemecké
        text,
    ))

    # ── parkovací asistent ────────────────────────────────────────────────────
    park_asist = bool(re.search(
        r"PARKOVAC[IÍ]\s+ASISTENT"       # PARKOVACÍ ASISTENT
        r"|PARK\s*ASIST"                 # skrátene
        r"|ASISTENT\s+PARKOVANIA"
        r"|PARKOVAC[IÍ]\s+SYSTÉM"
        r"|PARK\s*PILOT",                # VW Park Pilot
        text,
    ))

    return {
        **meta,
        "bateria":    bateria,
        "km":         km,
        "rocnik":     rocnik,
        "tahac":      int(tahac),
        "park_asist": int(park_asist),
        "popis":      text[:800],
    }


# ── uloženie / detekcia zmien ─────────────────────────────────────────────────
def uložiť(conn: sqlite3.Connection, auto: dict) -> str:
    """
    Vráti: 'nové' | 'zmena_ceny' | 'nezmenené'
    """
    dnes = str(date.today())
    existujuce = conn.execute(
        "SELECT cena FROM inzeraty WHERE id=?", (auto["id"],)
    ).fetchone()

    if existujuce is None:
        conn.execute("""
            INSERT INTO inzeraty
              (id, url, nazov, cena, km, rocnik, lokalita, bateria, tahac, park_asist, popis, prvy_zaznam, posledny)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            auto["id"], auto["url"], auto["nazov"], auto["cena"],
            auto["km"], auto.get("rocnik", ""), auto["lokalita"], auto["bateria"],
            auto["tahac"], auto["park_asist"], auto["popis"],
            dnes, dnes,
        ))
        conn.commit()
        return "nové"

    stara_cena = existujuce[0]
    conn.execute(
        "UPDATE inzeraty SET posledny=?, cena=?, km=?, rocnik=?, park_asist=?, tahac=? WHERE id=?",
        (dnes, auto["cena"], auto["km"], auto.get("rocnik", ""), auto["park_asist"], auto["tahac"], auto["id"]),
    )

    if stara_cena != auto["cena"]:
        conn.execute(
            "INSERT INTO zmeny_cien VALUES (?,?,?,?)",
            (auto["id"], dnes, stara_cena, auto["cena"]),
        )
        conn.commit()
        return "zmena_ceny"

    conn.commit()
    return "nezmenené"


# ── výstup ────────────────────────────────────────────────────────────────────
SEP   = "─" * 112
DSEP  = "═" * 112

def format_km(km):
    if km is None:
        return "  –   "
    if km == 0:
        return "  0?  "   # pravdepodobne neuvedené
    return f"{km:>6,}".replace(",", " ")

def format_cena(c):
    return f"{c:>7,}".replace(",", " ") + " €"

def štítok(tahac: int, park: int) -> str:
    t = "✔ Ťažné" if tahac else "✘ Ťažné"
    p = "✔ Park.asist." if park else "✘ Park.asist."
    return f"{t}  {p}"

def tlač_tabuľka(skupina: list[dict], nadpis: str, zmeny: dict):
    if not skupina:
        return
    print(f"\n{DSEP}")
    print(f"  {nadpis}")
    print(DSEP)
    hlavička = (
        f"{'#':>3}  {'Ročník':<8}  {'Km':>7}  {'Cena':>10}  {'Lokalita':<14}"
        f"  {'Výbava':<30}  {'Stav':^12}  Odkaz"
    )
    print(hlavička)
    print(SEP)
    for i, a in enumerate(skupina, 1):
        stav = zmeny.get(a["id"], "")
        stav_str = (
            "🆕 NOVÉ"   if stav == "nové"       else
            "💰 CENA↓"  if stav == "zmena_ceny" and a["cena"] < zmeny.get(f"{a['id']}_stara", a["cena"]) else
            "💰 CENA↑"  if stav == "zmena_ceny" else
            ""
        )
        rocnik = a.get("rocnik") or "–"
        print(
            f"{i:>3}  {rocnik:<8}  {format_km(a['km'])}  {format_cena(a['cena'])}"
            f"  {a['lokalita']:<14}  {štítok(a['tahac'], a['park_asist']):<30}"
            f"  {stav_str:^12}  {a['url']}"
        )
    print(SEP)



# ── HTML export ───────────────────────────────────────────────────────────────
HTML_PATH = Path(__file__).resolve().with_name("index.html")

def html_km(km):
    if km is None: return '<span class="neznáme">–</span>'
    if km == 0:    return '<span class="neznáme">?</span>'
    return f"{km:,}".replace(",", "\u202f") + " km"

def html_cena(c):
    return f"{c:,}".replace(",", "\u202f") + " €"

def html_stav(stav, stara_cena, nova_cena):
    if stav == "nové":
        return '<span class="badge new">🆕 Nové</span>'
    if stav == "zmena_ceny":
        diff = nova_cena - stara_cena
        cls  = "price-down" if diff < 0 else "price-up"
        sym  = "▼" if diff < 0 else "▲"
        return f'<span class="badge {cls}">{sym} {abs(diff):,}€</span>'.replace(",", "\u202f")
    return ""

def html_nove_inzeraty(nove_db):
        if not nove_db:
                return ""

        riadky = ""
        for ad_id, nazov, datum, cena, km, lokalita, url in nove_db:
                riadky += f"""
                <tr>
                    <td>{datum}</td>
                    <td class="cena">{html_cena(cena)}</td>
                    <td>{html_km(km)}</td>
                    <td>{lokalita}</td>
                    <td><a href="{url}" target="_blank" rel="noopener">🔗 otvoriť</a></td>
                    <td class="nazov"><a href="{url}" target="_blank" rel="noopener">{nazov}</a></td>
                </tr>"""

        return f"""
        <section class="cat-newhistory">
            <h2>🆕 História nových inzerátov <span class="cnt">{len(nove_db)}</span></h2>
            <table>
                <thead><tr><th>Dátum pridania</th><th>Cena</th><th>Km</th><th>Lokalita</th><th>Odkaz</th><th>Názov inzerátu</th></tr></thead>
                <tbody>{riadky}</tbody>
            </table>
        </section>"""

def html_sekcia(skupina, nadpis, farba_triedy, zmeny):
    if not skupina:
        return ""
    riadky = ""
    for a in skupina:
        stav      = zmeny.get(a["id"], "")
        stara     = zmeny.get(f"{a['id']}_stara", a["cena"])
        stav_html = html_stav(stav, stara, a["cena"])
        t = "✔" if a["tahac"]     else "✘"
        p = "✔" if a["park_asist"] else "✘"
        tc = "ok" if a["tahac"]     else "no"
        pc = "ok" if a["park_asist"] else "no"
        rocnik = a.get("rocnik") or "–"
        riadky += f"""
        <tr>
          <td class="rocnik">{rocnik}</td>
          <td>{html_km(a['km'])}</td>
          <td class="cena">{html_cena(a['cena'])}</td>
          <td>{a['lokalita']}</td>
          <td class="{tc}">{t} Ťažné</td>
          <td class="{pc}">{p} Park.</td>
          <td>{stav_html}</td>
          <td><a href="{a['url']}" target="_blank" rel="noopener">🔗 otvoriť</a></td>
          <td class="nazov"><a href="{a['url']}" target="_blank" rel="noopener">{a['nazov']}</a></td>
        </tr>"""
    return f"""
    <section class="{farba_triedy}">
      <h2>{nadpis} <span class="cnt">{len(skupina)}</span></h2>
      <table>
        <thead><tr>
          <th onclick="sortTable(this)">Ročník ↕</th>
          <th onclick="sortTable(this)">Km ↕</th>
          <th onclick="sortTable(this)">Cena ↕</th>
          <th>Lokalita</th>
          <th>Ťažné</th>
          <th>Park.</th>
          <th>Zmena</th>
          <th>Odkaz</th>
          <th>Názov inzerátu</th>
        </tr></thead>
        <tbody>{riadky}</tbody>
      </table>
    </section>"""

def vygeneruj_html(autá, zmeny, zmeny_db, nove_db, dnes_str):
    nové_ct = sum(1 for v in zmeny.values() if v == "nové")
    cena_ct = sum(1 for v in zmeny.values() if v == "zmena_ceny")

    s_tahac_park = sorted([a for a in autá if a["tahac"] and a["park_asist"]],
                          key=lambda x: (x["km"] is None or x["km"] == 0, x["km"] or 999999))
    iba_tahac    = sorted([a for a in autá if a["tahac"] and not a["park_asist"]],
                          key=lambda x: (x["km"] is None or x["km"] == 0, x["km"] or 999999))
    iba_park     = sorted([a for a in autá if not a["tahac"] and a["park_asist"]],
                          key=lambda x: (x["km"] is None or x["km"] == 0, x["km"] or 999999))
    bez_oboch    = sorted([a for a in autá if not a["tahac"] and not a["park_asist"]],
                          key=lambda x: (x["km"] is None or x["km"] == 0, x["km"] or 999999))

    sekcie = (
        html_sekcia(s_tahac_park, "✔ Ťažné zariadenie + Parkovací asistent", "cat-best", zmeny)
      + html_sekcia(iba_tahac,    "⚠ Iba ťažné zariadenie",                  "cat-tahac", zmeny)
      + html_sekcia(iba_park,     "⚠ Iba parkovací asistent",                "cat-park",  zmeny)
      + html_sekcia(bez_oboch,    "✘ Bez ťažného aj parkovacieho asistenta", "cat-none",  zmeny)
    )

    historia = ""
    historia_novych = html_nove_inzeraty(nove_db)
    if zmeny_db:
        riadky = ""
        for ad_id, nazov, datum, stara, nova in zmeny_db:
            diff = nova - stara
            cls  = "price-down" if diff < 0 else "price-up"
            sym  = "▼" if diff < 0 else "▲"
            riadky += f"""
            <tr>
              <td>{datum}</td>
              <td class="{cls}">{sym} {abs(diff):,}€</td>
              <td>{html_cena(stara)}</td>
              <td>{html_cena(nova)}</td>
              <td>{nazov}</td>
            </tr>""".replace(",", "\u202f")
        historia = f"""
    <section class="cat-history">
      <h2>📈 História zmien cien</h2>
      <table>
        <thead><tr><th>Dátum</th><th>Zmena</th><th>Pôvodná cena</th><th>Nová cena</th><th>Inzerát</th></tr></thead>
        <tbody>{riadky}</tbody>
      </table>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VW ID.4 150kW – prehľad {dnes_str}</title>
<style>
  :root {{
    --green:#1a7a4a; --green-bg:#eafaf1;
    --yellow:#7a5c00; --yellow-bg:#fffbea;
    --blue:#1a4a7a; --blue-bg:#eaf3fa;
    --gray:#444; --gray-bg:#f5f5f5;
    --red:#c0392b; --teal:#16796f;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:#f0f2f5; color:#222; padding:1rem; }}
  header {{ background:#1b2a4a; color:#fff; padding:1.2rem 1.5rem;
            border-radius:10px; margin-bottom:1.2rem; }}
  header h1 {{ font-size:1.3rem; }}
  .meta {{ font-size:.85rem; opacity:.75; margin-top:.3rem; }}
  .stats {{ display:flex; gap:1rem; margin-top:.8rem; flex-wrap:wrap; }}
  .stat {{ background:rgba(255,255,255,.15); border-radius:6px;
           padding:.3rem .8rem; font-size:.9rem; }}
  section {{ background:#fff; border-radius:10px; margin-bottom:1.2rem;
             box-shadow:0 1px 4px rgba(0,0,0,.08); overflow:hidden; }}
  section h2 {{ padding:.8rem 1.2rem; font-size:1rem; display:flex;
                align-items:center; gap:.5rem; }}
  .cnt {{ background:rgba(0,0,0,.12); border-radius:10px;
          padding:.1rem .5rem; font-size:.8rem; }}
  .cat-best  h2 {{ background:var(--green-bg); color:var(--green); border-left:4px solid var(--green); }}
  .cat-tahac h2 {{ background:var(--yellow-bg); color:var(--yellow); border-left:4px solid #e6b800; }}
  .cat-park  h2 {{ background:var(--blue-bg); color:var(--blue); border-left:4px solid #2980b9; }}
  .cat-none  h2 {{ background:var(--gray-bg); color:var(--gray); border-left:4px solid #aaa; }}
  .cat-history h2 {{ background:#fdf0f0; color:#7a1a1a; border-left:4px solid #c0392b; }}
    .cat-newhistory h2 {{ background:#eef7ff; color:#184c7c; border-left:4px solid #2563eb; }}
  table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
  th {{ background:#f8f9fa; padding:.55rem .8rem; text-align:left;
        border-bottom:2px solid #e0e0e0; white-space:nowrap; }}
  th[onclick] {{ cursor:pointer; user-select:none; }}
  th[onclick]:hover {{ background:#e8eaf0; }}
  td {{ padding:.5rem .8rem; border-bottom:1px solid #f0f0f0; vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#fafbff; }}
  td.ok {{ color:var(--green); font-weight:600; }}
  td.no {{ color:#bbb; }}
  td.cena {{ font-weight:700; white-space:nowrap; }}
  td.rocnik {{ white-space:nowrap; font-variant-numeric: tabular-nums; }}
  td.nazov {{ font-size:.82rem; max-width:260px; }}
  td.nazov a {{ color:#444; text-decoration:none; }}
  td.nazov a:hover {{ text-decoration:underline; }}
  a {{ color:#2563eb; }}
  .badge {{ display:inline-block; border-radius:4px; padding:.15rem .45rem;
            font-size:.78rem; font-weight:700; white-space:nowrap; }}
  .new {{ background:#d4edda; color:#155724; }}
  .price-down {{ background:#d4edda; color:#155724; }}
  .price-up   {{ background:#fde8e8; color:#7a1a1a; }}
  .neznáme {{ color:#bbb; }}
  @media(max-width:700px) {{ table {{ display:block; overflow-x:auto; }} }}
</style>
</head>
<body>
<header>
  <h1>🚗 VW ID.4 150kW (77/82 kWh) – prehľad inzerátov</h1>
  <div class="meta">Zdroj: auto.bazos.sk &nbsp;|&nbsp; Aktualizované: {dnes_str}</div>
  <div class="stats">
    <div class="stat">📋 Celkom: {len(autá)}</div>
    <div class="stat">🆕 Nové: {nové_ct}</div>
    <div class="stat">💰 Zmena ceny: {cena_ct}</div>
    <div class="stat">✔ Ťažné+Park: {len(s_tahac_park)}</div>
  </div>
</header>
{sekcie}
{historia_novych}
{historia}
<script>
function sortTable(th) {{
  const table = th.closest('table');
  const tbody = table.querySelector('tbody');
  const col   = Array.from(th.parentNode.children).indexOf(th);
  const asc   = th.dataset.asc !== '1';
  th.dataset.asc = asc ? '1' : '';
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {{
    const av = a.cells[col].innerText.replace(/[^\\d.,]/g,'').replace(',','.');
    const bv = b.cells[col].innerText.replace(/[^\\d.,]/g,'').replace(',','.');
    const an = parseFloat(av) || 0, bn = parseFloat(bv) || 0;
    return asc ? an - bn : bn - an;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body></html>"""


def main():
    conn     = get_db()
    dnes_str = str(date.today())

    print(f"\n{'═'*60}")
    print(f"  VW ID.4 150kW (77/82 kWh) – prehľad inzerátov")
    print(f"  Dátum: {dnes_str}   Zdroj: auto.bazos.sk")
    print(f"{'═'*60}\n")
    print("⏳ Načítavam výsledky vyhľadávania …")

    # ── stránkovanie ─────────────────────────────────────────────────────────
    kandidáti  = []
    seen_ids   = set()
    crp        = 0

    while True:
        url = SEARCH_BASE.format(crp=crp if crp > 0 else "")
        try:
            html_zoznam = fetch(url)
        except urllib.error.URLError as e:
            print(f"❌ Chyba pri načítaní stránky (crp={crp}): {e}")
            break

        if crp == 0:
            total = count_total(html_zoznam)
            print(f"   Celkom inzerátov: {total}")

        page_items = parse_listing_page(html_zoznam)
        new_items  = [x for x in page_items if x["id"] not in seen_ids]
        if not new_items:
            break
        for x in new_items:
            seen_ids.add(x["id"])
        kandidáti.extend(new_items)

        crp += PAGE_SIZE
        if crp >= total:
            break
        time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"   Načítaných inzerátov zo všetkých stránok: {len(kandidáti)}")
    print("   Načítavam detaily …\n")

    autá     = []
    zmeny    = {}

    for meta in kandidáti:
        time.sleep(DELAY_BETWEEN_REQUESTS)
        try:
            html_detail = fetch(meta["url"])
        except urllib.error.URLError:
            continue

        auto = parse_detail(html_detail, meta)
        if auto is None:
            continue

        stav = uložiť(conn, auto)
        zmeny[auto["id"]] = stav

        # pre zobrazenie smeru zmeny ceny si zapamätaj starú cenu
        if stav == "zmena_ceny":
            row = conn.execute(
                "SELECT cena_stara FROM zmeny_cien WHERE id=? ORDER BY rowid DESC LIMIT 1",
                (auto["id"],),
            ).fetchone()
            if row:
                zmeny[f"{auto['id']}_stara"] = row[0]

        symbol = {"nové": "🆕", "zmena_ceny": "💰", "nezmenené": "  "}.get(stav, "  ")
        km_str = format_km(auto["km"])
        print(f"  {symbol}  {auto['nazov'][:55]:<55}  {km_str} km  {format_cena(auto['cena'])}")
        autá.append(auto)

    if not autá:
        print("\n⚠️  Žiadne inzeráty nesplňajú kritériá (150kW, 77/82 kWh).")
        conn.close()
        return

    # ── roztriedenie ──────────────────────────────────────────────────────────
    s_tahac_park  = sorted(
        [a for a in autá if a["tahac"] and a["park_asist"]],
        key=lambda x: (x["km"] is None, x["km"] or 0),
    )
    len_ok = len(s_tahac_park)

    iba_tahac     = sorted(
        [a for a in autá if a["tahac"] and not a["park_asist"]],
        key=lambda x: (x["km"] is None, x["km"] or 0),
    )
    iba_park      = sorted(
        [a for a in autá if not a["tahac"] and a["park_asist"]],
        key=lambda x: (x["km"] is None, x["km"] or 0),
    )
    bez_oboch     = sorted(
        [a for a in autá if not a["tahac"] and not a["park_asist"]],
        key=lambda x: (x["km"] is None, x["km"] or 0),
    )

    # ── výpis ─────────────────────────────────────────────────────────────────
    tlač_tabuľka(
        s_tahac_park, 
        f"✔ MÁ ŤAŽNÉ ZARIADENIE  +  PARKOVACÍ ASISTENT  ({len_ok} vozidiel)",
        zmeny,
    )
    tlač_tabuľka(
        iba_tahac,
        f"⚠  Má ŤAŽNÉ ZARIADENIE – bez parkovacieho asistenta  ({len(iba_tahac)} vozidiel)",
        zmeny,
    )
    tlač_tabuľka(
        iba_park,
        f"⚠  Má PARKOVACÍ ASISTENT – bez ťažného zariadenia  ({len(iba_park)} vozidiel)",
        zmeny,
    )
    tlač_tabuľka(
        bez_oboch,
        f"✘ Bez ťažného zariadenia  &  bez parkovacieho asistenta  ({len(bez_oboch)} vozidiel)",
        zmeny,
    )

    # ── súhrn zmien ───────────────────────────────────────────────────────────
    nové_ct    = sum(1 for v in zmeny.values() if v == "nové")
    cena_ct    = sum(1 for v in zmeny.values() if v == "zmena_ceny")
    spolu      = len(autá)
    print(f"\n{'═'*60}")
    print(f"  SÚHRN: {spolu} vozidiel  |  🆕 Nové: {nové_ct}  |  💰 Zmenená cena: {cena_ct}")
    print(f"  Databáza uložená: {DB_PATH}")
    print(f"{'═'*60}\n")

    # ── história zmien cien ───────────────────────────────────────────────────
    zmeny_db = conn.execute("""
        SELECT z.id, i.nazov, z.datum, z.cena_stara, z.cena_nova
        FROM zmeny_cien z JOIN inzeraty i ON z.id = i.id
        ORDER BY z.rowid DESC LIMIT 20
    """).fetchall()

    nove_db = conn.execute("""
        SELECT id, nazov, prvy_zaznam, cena, km, lokalita, url
        FROM inzeraty
        ORDER BY prvy_zaznam DESC, rowid DESC LIMIT 50
    """).fetchall()

    if zmeny_db:
        print("  HISTÓRIA ZMIEN CEN (posledných 20):")
        print(f"  {'ID':<12} {'Dátum':<12} {'Stará cena':>12} {'Nová cena':>12}  Názov")
        print("  " + "─" * 80)
        for row in zmeny_db:
            ad_id, nazov, datum, stara, nova = row
            smer = "↓" if nova < stara else "↑"
            diff = abs(nova - stara)
            print(f"  {ad_id:<12} {datum:<12} {format_cena(stara):>12} {format_cena(nova):>12}  {smer}{diff}€  {nazov[:40]}")
        print()

    if nove_db:
        print("  HISTÓRIA NOVÝCH INZERÁTOV (posledných 50):")
        print(f"  {'Dátum':<12} {'Cena':>12} {'Km':>10}  {'Lokalita':<14}  Názov")
        print("  " + "─" * 80)
        for row in nove_db:
            ad_id, nazov, datum, cena, km, lokalita, url = row
            print(f"  {datum:<12} {format_cena(cena):>12} {format_km(km):>10}  {lokalita:<14}  {nazov[:40]}")
        print()

    # ── HTML export ───────────────────────────────────────────────────────────
    html = vygeneruj_html(autá, zmeny, zmeny_db, nove_db, dnes_str)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"  🌐 HTML report: {HTML_PATH}")
    print(f"     Otvorte v prehliadači: open \"{HTML_PATH}\"")
    print()

    conn.close()


if __name__ == "__main__":
    main()
