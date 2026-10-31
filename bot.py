#!/usr/bin/env python3
"""Bot Telegram personale: orari weekend F1 e MotoGP (ora italiana).

Modalita':
  python3 bot.py poll     -> risponde ai comandi arrivati (da lanciare ogni ~5 min)
  python3 bot.py weekly   -> invia il riepilogo se questa settimana c'e' un GP
  python3 bot.py force    -> come weekly, ma invia anche se non c'e' nessun GP
  python3 bot.py setup    -> registra il menu dei comandi su Telegram
  python3 bot.py debug    -> stampa cosa restituiscono le fonti dati (nessun invio)

Variabili d'ambiente: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (solo il tuo chat id).
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

DAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre"]

COMMANDS = [
    ("f1_prossima", "F1: prossima sessione"),
    ("f1_qualifiche", "F1: prossime qualifiche"),
    ("f1_gara", "F1: prossima gara e sprint"),
    ("f1_weekend", "F1: weekend completo"),
    ("motogp_prossima", "MotoGP: prossima sessione"),
    ("motogp_qualifiche", "MotoGP: prossime qualifiche"),
    ("motogp_gara", "MotoGP: prossima gara e sprint"),
    ("motogp_weekend", "MotoGP: weekend completo"),
    ("help", "Come funziona"),
]

KIND_LABEL = {
    "fp": "prove libere", "quali": "qualifiche", "sprint_quali": "qualifiche sprint",
    "sprint": "sprint", "race": "gara", "warmup": "warm up",
}
QUALI_KINDS = {"quali", "sprint_quali"}
RACE_KINDS = {"race"}


@dataclass
class Session:
    series: str          # "F1" | "MotoGP"
    gp: str              # nome del Gran Premio
    name: str            # nome sessione (FP1, Qualifiche, Gara...)
    kind: str            # fp | quali | sprint_quali | sprint | race | warmup | weekend
    start: datetime      # sempre in UTC
    end: object = None   # fine sessione/evento (UTC), se nota


@dataclass
class Weekend:
    series: str
    gp: str
    sessions: list


# ----------------------------------------------------------------- utilita' HTTP

def get_json(url, tries=3, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "f1-motogp-telegram-bot/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def tg(method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send(chat_id, text):
    for chunk in split_text(text):
        tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML",
           disable_web_page_preview=True)


def split_text(text, limit=3800):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        parts.append(cur)
    return parts


def esc(s):
    return html.escape(str(s), quote=False)


def parse_dt(value, time_part=None):
    """Converte stringhe ISO (con o senza ora separata) in datetime UTC."""
    if not value:
        return None
    s = str(value).strip()
    if time_part:
        s = f"{s}T{str(time_part).strip()}"
    s = s.replace("Z", "+00:00")
    if re.search(r"T.*[+-]\d{4}$", s):          # +0100 -> +01:00
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ----------------------------------------------------------------- fonte F1

F1_SESSIONS = [
    ("FirstPractice", "FP1", "fp"),
    ("SecondPractice", "FP2", "fp"),
    ("ThirdPractice", "FP3", "fp"),
    ("SprintQualifying", "Qualifiche Sprint", "sprint_quali"),
    ("Sprint", "Sprint", "sprint"),
    ("Qualifying", "Qualifiche", "quali"),
]


def f1_from_races(races):
    out = []
    for r in races:
        name = r.get("raceName", "Gran Premio").replace("Grand Prix", "GP")
        loc = (r.get("Circuit", {}).get("Location", {}) or {}).get("locality")
        gp = f"{name} ({loc})" if loc else name
        for key, label, kind in F1_SESSIONS:
            blk = r.get(key)
            if blk:
                dt = parse_dt(blk.get("date"), blk.get("time"))
                if dt:
                    out.append(Session("F1", gp, label, kind, dt))
        dt = parse_dt(r.get("date"), r.get("time"))
        if dt:
            out.append(Session("F1", gp, "Gara", "race", dt))
    return out


def f1_sessions(year):
    data = get_json(f"https://api.jolpi.ca/ergast/f1/{year}.json?limit=100", tries=2, timeout=15)
    return f1_from_races(data["MRData"]["RaceTable"]["Races"])


# ----------------------------------------------------------------- fonte MotoGP
# Fonte: API MotoGP (PulseLive). Le sessioni ("broadcasts") sono dentro l'evento:
#   /motogp/v1/events?seasonYear=ANNO   -> elenco eventi
#   /motogp/v1/events/{id}              -> evento con broadcasts e categorie
# Il formato e' dedotto da documentazione NON ufficiale: se qualcosa non torna,
# lancia "debug" e guarda il log.

MOTOGP_HOSTS = ["https://api.motogp.pulselive.com", "https://api.pulselive.motogp.com"]

SKIP_WORDS = ("press", "conference", "media", "briefing", "walk", "show",
              "ceremony", "interview", "parade")


def pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def classify(kind, shortname, name):
    text = f"{kind or ''} {shortname or ''} {name or ''}".lower()
    sn = (shortname or "").lower()
    if any(w in text for w in SKIP_WORDS):
        return None
    if "sprint" in text and "qualif" in text:
        return "sprint_quali"
    if "sprint" in text or sn == "spr":
        return "sprint"
    if "warm" in text or sn == "wup":
        return "warmup"
    if "qualif" in text or sn in ("q1", "q2"):
        return "quali"
    if "practice" in text or sn.startswith("fp") or sn == "pr":
        return "fp"
    if "race" in text or sn == "rac":
        return "race"
    return None


def session_label(kind, shortname, name):
    sn = (shortname or "").upper()
    if kind == "fp":
        if sn.startswith("FP"):
            return sn
        return "Practice" if sn == "PR" else "Prove libere"
    if kind == "quali":
        return {"Q1": "Qualifiche Q1", "Q2": "Qualifiche Q2"}.get(sn, "Qualifiche")
    return {"sprint": "Sprint", "race": "Gara", "warmup": "Warm up",
            "sprint_quali": "Qualifiche Sprint"}.get(kind, name or "Sessione")


def is_motogp_class(b):
    cat = b.get("category")
    if isinstance(cat, dict):
        acr = (cat.get("acronym") or "").upper()
        nm = (cat.get("name") or "").replace("™", "").strip().lower()
    else:
        acr, nm = "", str(cat or "").replace("™", "").strip().lower()
    if acr:
        return acr == "MGP"
    return nm == "motogp" if nm else True


def event_broadcasts(ev, host, debug=False):
    """Sessioni dell'evento. None = non riesco a leggerle (endpoint non risponde)."""
    items = ev.get("broadcasts")
    if not items:
        try:
            full = get_json(f"{host}/motogp/v1/events/{ev.get('id')}", tries=1, timeout=8)
        except Exception as e:  # noqa: BLE001
            if debug:
                print(f"[motogp] evento {ev.get('id')}: {e}")
            return None
        items = full.get("broadcasts") if isinstance(full, dict) else None
    return items or []


def motogp_sessions(year, debug=False):
    events, host = None, None
    for h in MOTOGP_HOSTS:
        try:
            events = get_json(f"{h}/motogp/v1/events?seasonYear={year}", tries=2, timeout=10)
            host = h
            break
        except Exception as e:  # noqa: BLE001
            if debug:
                print(f"[motogp] {h} events: {e}")
    if events is None:
        raise RuntimeError("API MotoGP non raggiungibile")
    if isinstance(events, dict):
        events = events.get("events") or events.get("data") or []
    if debug:
        print(f"[motogp] {len(events)} eventi; chiavi esempio: "
              f"{sorted(events[0].keys()) if events else '-'}")

    now = datetime.now(timezone.utc)
    upcoming = []
    for ev in events:
        if str(ev.get("kind") or "").upper() == "TEST" or ev.get("test"):
            continue
        end = parse_dt(ev.get("date_end")) or parse_dt(ev.get("date_start"))
        if end and end + timedelta(days=1) < now:
            continue  # evento già concluso
        upcoming.append(ev)
    upcoming.sort(key=lambda e: parse_dt(e.get("date_start")) or now)

    out, fetch_ok = [], True
    for ev in upcoming[:3]:
        label = (pick(ev, "name", "sponsored_name", "short_name") or "MotoGP")
        label = label.replace("™", "").replace("Grand Prix", "GP").strip()
        circ = ev.get("circuit")
        place = circ.get("place") if isinstance(circ, dict) else None
        gp = f"{label} ({place})" if place and place.lower() not in label.lower() else label

        items = None
        if fetch_ok:
            items = event_broadcasts(ev, host, debug)
            if items is None:
                fetch_ok = False  # non insistere sugli altri eventi
        found = []
        for b in items or []:
            if not is_motogp_class(b):
                continue
            kind = classify(b.get("kind"), b.get("shortname"), b.get("name"))
            dt = parse_dt(pick(b, "date_start", "start", "start_time", "datetime"))
            if debug:
                print(f"[motogp]   {b.get('shortname')!r:8} {b.get('name')!r:28} "
                      f"kind={b.get('kind')!r:12} -> {kind}  {dt}")
            if kind and dt:
                found.append(Session("MotoGP", gp, session_label(kind, b.get("shortname"), b.get("name")),
                                     kind, dt, parse_dt(b.get("date_end"))))
        if found:
            out.extend(found)
        else:  # niente orari: almeno il weekend, valido fino alla fine dell'evento
            start = parse_dt(ev.get("date_start"))
            end = parse_dt(ev.get("date_end")) or start
            if start:
                out.append(Session("MotoGP", gp, "Weekend", "weekend", start,
                                   end + timedelta(days=1)))
    return out


# ----------------------------------------------------------------- caricamento

def load_all(debug=False):
    now = datetime.now(timezone.utc)
    years = [now.year] + ([now.year + 1] if now.month >= 10 else [])
    sessions, errors = [], []
    for series, fn in (("F1", f1_sessions),
                       ("MotoGP", lambda y: motogp_sessions(y, debug))):
        got, err = [], None
        for y in years:
            if y != years[0] and any(s.start >= now for s in got):
                break  # l'anno dopo serve solo a fine stagione
            try:
                got += fn(y)
            except Exception as e:  # noqa: BLE001
                if y == years[0]:
                    err = e
        if not got and err:
            errors.append(f"{series}: dati non disponibili ({err})")
        sessions += got
    sessions.sort(key=lambda s: s.start)
    return sessions, errors


def weekends(sessions, series=None):
    groups = {}
    for s in sessions:
        if series and s.series != series:
            continue
        groups.setdefault((s.series, s.gp), []).append(s)
    out = [Weekend(k[0], k[1], sorted(v, key=lambda x: x.start)) for k, v in groups.items()]
    out.sort(key=lambda w: w.sessions[0].start)
    return out


def w_end(w):
    return max((s.end or s.start) for s in w.sessions)


def current_or_next(sessions, series, now):
    for w in weekends(sessions, series):
        if w_end(w) >= now - timedelta(hours=3):
            return w
    return None


# ----------------------------------------------------------------- formattazione

def until(dt, now):
    m = max(int((dt - now).total_seconds() // 60), 0)
    days, rem = divmod(m, 1440)
    h, mi = divmod(rem, 60)
    if days:
        return f"tra {days}g {h}h"
    if h:
        return f"tra {h}h {mi}m"
    return f"tra {mi}m"


def icon(series):
    return "🏎️" if series == "F1" else "🏍️"


def day_label(loc):
    return f"{DAYS[loc.weekday()].capitalize()} {loc.day} {MONTHS[loc.month - 1]}"


def fmt_weekend(w, now):
    lines = [f"{icon(w.series)} <b>{esc(w.series)} – {esc(w.gp)}</b>"]
    real = [s for s in w.sessions if s.kind != "weekend"]
    if not real:
        loc = w.sessions[0].start.astimezone(TZ)
        lines.append(f"\nWeekend dal {loc.day} {MONTHS[loc.month - 1]} "
                     "(orari delle sessioni non ancora disponibili)")
        return "\n".join(lines)
    last_day = None
    for s in real:
        loc = s.start.astimezone(TZ)
        if loc.date() != last_day:
            lines.append(f"\n<b>{day_label(loc)}</b>")
            last_day = loc.date()
        mark = "✅" if s.start < now else "🕒"
        lines.append(f"{mark} {loc:%H:%M} – {esc(s.name)}")
    return "\n".join(lines)


def fmt_next(s, now):
    loc = s.start.astimezone(TZ)
    return (f"{icon(s.series)} <b>{esc(s.series)} – {esc(s.name)}</b>\n"
            f"{esc(s.gp)}\n{day_label(loc)}, ore {loc:%H:%M} ({until(s.start, now)})")


def errors_block(errors):
    return ("\n\n⚠️ " + "\n⚠️ ".join(esc(e) for e in errors)) if errors else ""


# ----------------------------------------------------------------- comandi

def next_matching(sessions, now, kinds, series):
    for s in sessions:
        if s.series == series and s.start >= now and s.kind != "weekend" \
                and (kinds is None or s.kind in kinds):
            return [s]
    return []


HELP_TEXT = (
    "Ciao! Ti mando gli orari dei weekend di F1 e MotoGP (ora italiana).\n\n"
    "Usa il pulsante <b>Menu</b> oppure scrivi:\n\n"
    "🏎️ <b>F1</b>\n"
    "/f1_prossima – prossima sessione\n"
    "/f1_qualifiche – prossime qualifiche\n"
    "/f1_gara – prossima gara (e sprint, se c'è)\n"
    "/f1_weekend – weekend completo\n\n"
    "🏍️ <b>MotoGP</b>\n"
    "/motogp_prossima – prossima sessione\n"
    "/motogp_qualifiche – prossime qualifiche\n"
    "/motogp_gara – prossima gara (e sprint, se c'è)\n"
    "/motogp_weekend – weekend completo"
)


def reply_for(cmd, sessions, errors):
    now = datetime.now(timezone.utc)
    if cmd in ("start", "help"):
        return HELP_TEXT
    prefix, _, action = cmd.partition("_")
    series = {"f1": "F1", "motogp": "MotoGP"}.get(prefix)
    if not series or action not in ("prossima", "qualifiche", "gara", "weekend"):
        return None
    errs = [e for e in errors if e.startswith(series)]
    if action == "weekend":
        w = current_or_next(sessions, series, now)
        text = fmt_weekend(w, now) if w else "Nessun weekend in programma."
    else:
        if action == "gara":
            # prossima gara + sprint dello stesso weekend (se c'è e non è già passata)
            found = next_matching(sessions, now, {"race"}, series)
            sprint = next_matching(sessions, now, {"sprint"}, series)
            if found and sprint and sprint[0].gp == found[0].gp:
                found = sprint + found
        else:
            kinds = QUALI_KINDS if action == "qualifiche" else None
            found = next_matching(sessions, now, kinds, series)
        text = "\n\n".join(fmt_next(s, now) for s in found)
        if not text:
            w = current_or_next(sessions, series, now)
            if w and all(s.kind == "weekend" for s in w.sessions):
                text = f"Orari delle sessioni di {esc(w.gp)} non disponibili al momento."
            else:
                text = "Nessuna sessione in programma."
    return text + errors_block(errs)


def poll():
    res = tg("getUpdates", timeout=0, allowed_updates=["message"])
    updates = res.get("result", [])
    if not updates:
        print("Nessun messaggio.")
        return
    cache = {}

    def data():
        if "d" not in cache:
            cache["d"] = load_all()
        return cache["d"]

    for u in updates:
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text.startswith("/"):
            continue
        cmd = text[1:].split()[0].split("@")[0].lower()
        try:
            if not CHAT_ID:
                if cmd == "start":
                    set_commands()
                    send(chat, f"Il tuo chat id è <code>{chat}</code>.\n"
                               "Aggiungilo nei Secrets di GitHub come TELEGRAM_CHAT_ID.")
                continue
            if str(chat) != CHAT_ID:
                continue  # bot personale: ignora gli altri
            if cmd == "start":
                set_commands()
            sessions, errors = (data() if cmd not in ("start", "help") else ([], []))
            out = reply_for(cmd, sessions, errors) or HELP_TEXT
            send(chat, out)
        except Exception as e:  # noqa: BLE001
            print(f"Errore su /{cmd}: {e}")
    tg("getUpdates", offset=max(u["update_id"] for u in updates) + 1, timeout=0)


def set_commands():
    tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])


def weekly(force=False):
    if not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID mancante")
    sessions, errors = load_all()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=5)
    blocks = []
    for w in weekends(sessions):
        if w_end(w) >= now and w.sessions[0].start <= horizon:
            blocks.append(fmt_weekend(w, now))
    if not blocks and not force and not errors:
        print("Nessun GP questo weekend: nessun messaggio.")
        return
    if not blocks:
        blocks = ["Nessun GP di F1 o MotoGP in programma questo weekend."]
    header = "📅 <b>Weekend di gara</b>\n\n"
    send(CHAT_ID, header + "\n\n".join(blocks) + errors_block(errors))
    set_commands()


def debug():
    sessions, errors = load_all(debug=True)
    print(f"Sessioni caricate: {len(sessions)}; errori: {errors}")
    now = datetime.now(timezone.utc)
    for series in ("F1", "MotoGP"):
        w = current_or_next(sessions, series, now)
        print(re_plain(fmt_weekend(w, now)) if w else f"{series}: nessun weekend")


def re_plain(t):
    return t.replace("<b>", "").replace("</b>", "")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    if mode not in ("debug",) and not TOKEN:
        sys.exit("TELEGRAM_TOKEN mancante")
    {"poll": poll, "weekly": weekly, "force": lambda: weekly(True),
     "setup": set_commands, "debug": debug}[mode]()
