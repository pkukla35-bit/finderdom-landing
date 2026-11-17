"""Wysyła szablon z /szablony na podany adres przez skrzynkę SMTP zapisaną w panelu admina (settings._id='email').

Użycie (GitHub Actions: workflow "Admin – wyślij szablon maila"):
  MONGODB_URI=... python scripts/send_template.py --template oferta-dla-biur --to adres@firma.pl [--subject "..."]
Temat domyślnie z pierwszej linii pliku .txt ("Temat: ...").
"""
import argparse, os, re, smtplib, ssl, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", default="")
    a = ap.parse_args()

    key = re.sub(r"[^a-z0-9-]", "", a.template.lower())
    html_path = ROOT / "szablony" / f"{key}.html"
    if key == "index" or not html_path.exists():
        sys.exit(f"Nie ma szablonu: {key}")
    html = html_path.read_text(encoding="utf-8")
    txt_path = html_path.with_suffix(".txt")
    text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else re.sub(r"<[^>]+>", " ", html)
    subject = a.subject.strip()
    if not subject and txt_path.exists():
        first = text.strip().splitlines()[0]
        if first.lower().startswith("temat:"):
            subject = first.split(":", 1)[1].strip()
    if not subject:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        subject = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else "FinderDom.pl"

    to = a.to.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        sys.exit(f"Zły adres: {to}")

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    cfg = ((db.settings.find_one({"_id": "email"}) or {}).get("smtp") or {})
    if not (cfg.get("host") and cfg.get("user") and cfg.get("password")):
        sys.exit("Brak konfiguracji SMTP w settings.email (panel admina → Mailing → skrzynka nadawcza)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"FinderDom.pl <{cfg.get('from_email') or cfg['user']}>"
    msg["To"] = to
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    port = int(cfg.get("port") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(cfg["host"], port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(cfg["user"], cfg["password"]); s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(cfg["user"], cfg["password"]); s.send_message(msg)
    print(f"wysłano szablon {key} → {to} (temat: {subject})")


if __name__ == "__main__":
    main()
