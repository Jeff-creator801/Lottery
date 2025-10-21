# app.py
import os
import threading
import time
import requests
from datetime import datetime
from urllib.parse import quote_plus

from flask import Flask, request, jsonify
from sqlalchemy import (create_engine, Column, Integer, String, Float,
                        ForeignKey, DateTime)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# -----------------------
# Config (env vars)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# URL that Render exposes, e.g. "earthlife.onrender.com" (without https://)
# used for setting webhook. If absent, user can call set_webhook with full URL.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
DATABASE_URL = os.getenv("DATABASE_URL")  # if not set, fallback to sqlite file

if not TELEGRAM_TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN environment variable")

# DB URL fallback: SQLite file for local testing
if not DATABASE_URL:
    DB_URL = "sqlite:///earth_life.db"
else:
    DB_URL = DATABASE_URL

# SQLAlchemy setup
Base = declarative_base()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# -----------------------
# Models
# -----------------------
class Civilization(Base):
    __tablename__ = "civilizations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    men = Column(Integer, default=50)
    women = Column(Integer, default=50)
    base_birth_rate = Column(Float, default=3.0)  # percent per year
    age = Column(Integer, default=0)
    last_update = Column(DateTime, default=datetime.utcnow)

    factors = relationship("Factor", back_populates="civ", cascade="all, delete-orphan")

class Factor(Base):
    __tablename__ = "factors"
    id = Column(Integer, primary_key=True)
    civ_id = Column(Integer, ForeignKey("civilizations.id"), nullable=False)
    name = Column(String, nullable=False)
    value = Column(Float, default=100.0)  # percent baseline 100.0

    civ = relationship("Civilization", back_populates="factors")

class Link(Base):
    __tablename__ = "links"
    id = Column(Integer, primary_key=True)
    civ_id = Column(Integer, ForeignKey("civilizations.id"), nullable=False)
    source_factor_id = Column(Integer, ForeignKey("factors.id"), nullable=False)
    target_factor_id = Column(Integer, ForeignKey("factors.id"), nullable=False)
    type = Column(String, nullable=False)  # 'inc' or 'dec'
    magnitude = Column(Float, default=5.0)  # percent magnitude

# create tables
Base.metadata.create_all(bind=engine)

# -----------------------
# App & helpers
# -----------------------
app = Flask(__name__)
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(chat_id: int, text: str):
    """Send message to telegram chat_id (simple wrapper)."""
    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})
        return resp.ok
    except Exception:
        return False

# -------- Core game logic (DB operations) --------
def create_civilization(name: str):
    session = SessionLocal()
    try:
        name = name[:64]
        # check exists
        existing = session.query(Civilization).filter_by(name=name).first()
        if existing:
            return False, "A civilization with that name already exists."
        civ = Civilization(name=name, men=50, women=50, base_birth_rate=3.0, age=0)
        session.add(civ)
        session.commit()
        # add Birth factor
        birth = Factor(civ_id=civ.id, name="Birth", value=100.0)
        session.add(birth)
        session.commit()
        return True, f"Civilization '{name}' created: 50 men, 50 women, Birth 100% (base birth rate 3%)."
    finally:
        session.close()

def get_civ_by_name(name: str):
    session = SessionLocal()
    try:
        return session.query(Civilization).filter_by(name=name).first()
    finally:
        session.close()

def add_factor(civ_name: str, factor_name: str):
    session = SessionLocal()
    try:
        civ = session.query(Civilization).filter_by(name=civ_name).first()
        if not civ:
            return False, "Civilization not found."
        if session.query(Factor).filter_by(civ_id=civ.id, name=factor_name).first():
            return False, "Factor already exists."
        f = Factor(civ_id=civ.id, name=factor_name, value=100.0)
        session.add(f)
        session.commit()
        return True, f"Factor '{factor_name}' added to civilization '{civ_name}'."
    finally:
        session.close()

def link_factors(civ_name: str, source: str, typ: str, target: str, magnitude: float = 5.0):
    session = SessionLocal()
    try:
        civ = session.query(Civilization).filter_by(name=civ_name).first()
        if not civ:
            return False, "Civilization not found."
        src = session.query(Factor).filter_by(civ_id=civ.id, name=source).first()
        tgt = session.query(Factor).filter_by(civ_id=civ.id, name=target).first()
        if not src or not tgt:
            return False, "Source or target factor not found."
        if typ not in ("inc", "dec"):
            return False, "type must be 'inc' or 'dec'."
        link = Link(civ_id=civ.id, source_factor_id=src.id, target_factor_id=tgt.id, type=typ, magnitude=float(magnitude))
        session.add(link)
        session.commit()
        return True, f"Linked: {source} ({typ}) -> {target} (mag={magnitude}%)"
    finally:
        session.close()

def list_factors(civ_name: str):
    session = SessionLocal()
    try:
        civ = session.query(Civilization).filter_by(name=civ_name).first()
        if not civ:
            return None
        rows = session.query(Factor).filter_by(civ_id=civ.id).all()
        return [(f.name, f.value) for f in rows]
    finally:
        session.close()

def list_links(civ_name: str):
    session = SessionLocal()
    try:
        civ = session.query(Civilization).filter_by(name=civ_name).first()
        if not civ:
            return None
        rows = session.query(Link).filter_by(civ_id=civ.id).all()
        res = []
        for l in rows:
            sess2 = SessionLocal()
            src = sess2.query(Factor).filter_by(id=l.source_factor_id).first()
            tgt = sess2.query(Factor).filter_by(id=l.target_factor_id).first()
            sess2.close()
            res.append((src.name if src else "?", l.type, tgt.name if tgt else "?", l.magnitude))
        return res
    finally:
        session.close()

# Influence algorithm
def apply_influences_to_civ(session, civ: Civilization):
    # load factor dict
    facts = {f.id: {"name": f.name, "value": f.value} for f in civ.factors}
    deltas = {fid: 0.0 for fid in facts.keys()}
    links = session.query(Link).filter_by(civ_id=civ.id).all()
    for l in links:
        if l.source_factor_id not in facts or l.target_factor_id not in facts:
            continue
        src_val = facts[l.source_factor_id]["value"]
        # influence proportional to deviation from baseline (100)
        delta = (src_val - 100.0) * (l.magnitude / 100.0)
        if l.type == "dec":
            delta = -delta
        deltas[l.target_factor_id] += delta
    # apply deltas
    for fid, delta in deltas.items():
        new_val = facts[fid]["value"] + delta
        if new_val < 0.0: new_val = 0.0
        if new_val > 1000.0: new_val = 1000.0
        session.query(Factor).filter_by(id=fid).update({"value": new_val})
    session.commit()

def apply_tick_to_civ(civ_name: str):
    session = SessionLocal()
    try:
        civ = session.query(Civilization).filter_by(name=civ_name).first()
        if not civ:
            return False, "Civilization not found."
        # 1) apply influences which can modify factor values
        apply_influences_to_civ(session, civ)
        # load Birth factor
        birth = session.query(Factor).filter_by(civ_id=civ.id, name="Birth").first()
        birth_val = birth.value if birth else 100.0
        effective_birth_rate = civ.base_birth_rate * (birth_val / 100.0)
        total = civ.men + civ.women
        new_people = total * (effective_birth_rate / 100.0)
        # mortality after 30 years
        if civ.age >= 30:
            mortality_rate = (civ.age - 30) * 0.1  # percent
            new_people -= total * (mortality_rate / 100.0)
        new_people_int = max(int(round(new_people)), 0)
        new_men = new_people_int // 2
        new_women = new_people_int - new_men
        civ.men += new_men
        civ.women += new_women
        civ.age += 1
        civ.last_update = datetime.utcnow()
        session.commit()
        return True, {
            "name": civ.name,
            "age": civ.age,
            "men": civ.men,
            "women": civ.women,
            "effective_birth_rate": round(effective_birth_rate, 6),
            "new_people": new_people_int
        }
    finally:
        session.close()

def tick_all():
    session = SessionLocal()
    try:
        civs = session.query(Civilization).all()
        results = {}
        for civ in civs:
            ok, res = apply_tick_to_civ(civ.name)
            results[civ.name] = res if ok else None
        return results
    finally:
        session.close()

# ---------- Background ticker ----------
def background_ticker_loop():
    # 3 hours = 10800 seconds
    wait_seconds = 3 * 60 * 60
    while True:
        try:
            print(f"[Ticker] Applying tick to all civilizations at {datetime.utcnow().isoformat()} ...")
            res = tick_all()
            print("[Ticker] Tick completed:", res)
        except Exception as e:
            print("[Ticker] Error:", e)
        time.sleep(wait_seconds)

# Start background thread when running
def start_background_thread():
    t = threading.Thread(target=background_ticker_loop, daemon=True)
    t.start()

# ---------- Webhook endpoint ----------
@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    # Telegram will POST updates here
    data = request.get_json(force=True)
    # handle message simple parser
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return jsonify({"ok": True})
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text", "")
    if not text:
        send_message(chat_id, "Я принимаю только текстовые команды.")
        return jsonify({"ok": True})
    # parse command
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    try:
        if cmd == "/start":
            send_message(chat_id, "Привет! Earth Life — создайте цивилизацию: /create <имя>")
        elif cmd == "/create":
            if not args:
                send_message(chat_id, "Использование: /create <имя_цивилизации>")
            else:
                name = " ".join(args)[:64]
                ok, msg = create_civilization(name)
                send_message(chat_id, msg)
        elif cmd == "/status":
            if not args:
                send_message(chat_id, "Использование: /status <имя_цивилизации>")
            else:
                name = " ".join(args)[:64]
                session = SessionLocal()
                civ = session.query(Civilization).filter_by(name=name).first()
                if not civ:
                    send_message(chat_id, "Цивилизация не найдена.")
                else:
                    factors = session.query(Factor).filter_by(civ_id=civ.id).all()
                    lines = [
                        f"🌍 {civ.name}",
                        f"Возраст: {civ.age} лет",
                        f"Мужчин: {civ.men}",
                        f"Женщин: {civ.women}",
                        f"Базовая рождаемость: {civ.base_birth_rate}%",
                        "Факторы:"
                    ]
                    for f in factors:
                        lines.append(f" - {f.name}: {round(f.value,3)}%")
                    links = list_links(name)
                    if links:
                        lines.append("Связи:")
                        for l in links:
                            arrow = "↑" if l[1] == "inc" else "↓"
                            lines.append(f" - {l[0]} {arrow} {l[2]} (mag={l[3]}%)")
                    send_message(chat_id, "\n".join(lines))
                session.close()
        elif cmd == "/addfactor":
            if len(args) < 2:
                send_message(chat_id, "Использование: /addfactor <имя_цивилизации> <название_фактора>")
            else:
                civ_name = args[0]
                factor_name = " ".join(args[1:])[:64]
                ok, msg = add_factor(civ_name, factor_name)
                send_message(chat_id, msg)
        elif cmd == "/link":
            # /link <civ> <source> <inc|dec> <target> [magnitude]
            if len(args) < 4:
                send_message(chat_id, "Использование: /link <civ> <source> <inc|dec> <target> [magnitude]")
            else:
                civ_name = args[0]
                source = args[1]
                typ = args[2]
                target = args[3]
                mag = float(args[4]) if len(args) >= 5 else 5.0
                ok, msg = link_factors(civ_name, source, typ, target, mag)
                send_message(chat_id, msg)
        elif cmd == "/tick":
            if not args:
                send_message(chat_id, "Использование: /tick <имя_цивилизации>")
            else:
                name = " ".join(args)[:64]
                ok, res = apply_tick_to_civ(name)
                if not ok:
                    send_message(chat_id, res)
                else:
                    send_message(chat_id, f"Прошёл 1 год для '{res['name']}'. Возраст: {res['age']} лет. Мужчин: {res['men']}. Женщин: {res['women']}. Новых людей: {res['new_people']}. Эфф. рождаемость: {res['effective_birth_rate']}%")
        elif cmd == "/help":
            send_message(chat_id, "/create <name>\n/status <name>\n/addfactor <civ> <factor>\n/link <civ> <src> <inc|dec> <tgt> [mag]\n/tick <name>\n/help")
        else:
            send_message(chat_id, "Неизвестная команда. /help")
    except Exception as e:
        send_message(chat_id, f"Ошибка: {e}")
    return jsonify({"ok": True})

# ---------- set_webhook route ----------
@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    # You can call this once after deploy (or use Render dashboard cron)
    if RENDER_EXTERNAL_URL:
        url = f"https://{RENDER_EXTERNAL_URL}/webhook/{TELEGRAM_TOKEN}"
    else:
        # user must provide full URL as ?url=...
        q = request.args.get("url")
        if not q:
            return "Provide ?url=https://yourdomain/webhook/<TOKEN> or set RENDER_EXTERNAL_URL env var", 400
        url = q
    resp = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": url})
    if resp.ok:
        return f"Webhook set to {url}: {resp.text}"
    else:
        return f"Failed to set webhook: {resp.status_code} {resp.text}", 500

# ---------- health check ----------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# ---------- start background thread on startup ----------
start_background_thread()

# ---------- Run (gunicorn recommended in production) ----------
if __name__ == "__main__":
    # For local debugging only
    start_background_thread()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
