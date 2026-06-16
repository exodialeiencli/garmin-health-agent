"""
garmin_fetch.py
---------------
Récupère tes données Garmin Connect du jour et génère une recommandation
de séance via Claude API, puis envoie le bilan sur Telegram.

INSTALLATION (une seule fois) :
    pip install garminconnect anthropic requests

VARIABLES D'ENVIRONNEMENT :
    GARMIN_EMAIL      → ton email Garmin Connect
    GARMIN_PASSWORD   → ton mot de passe Garmin Connect
    ANTHROPIC_KEY     → ta clé API Anthropic
    TELEGRAM_TOKEN    → token du bot BotFather
    TELEGRAM_CHAT_ID  → ton Chat ID Telegram
"""

import json
import os
import requests
from datetime import date, timedelta
from garminconnect import Garmin
import anthropic

# ─────────────────────────────────────────────
# CONFIGURATION — lus depuis les variables d'environnement
# Sur GitHub Actions : Settings → Secrets
# En local : renseigne directement ici
# ─────────────────────────────────────────────
GARMIN_EMAIL     = os.environ.get("GARMIN_EMAIL",     "")
GARMIN_PASSWORD  = os.environ.get("GARMIN_PASSWORD",  "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    """Envoie un message Telegram. Silencieux si token absent."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram non configuré — envoi ignoré")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("✅ Message Telegram envoyé")
        else:
            print(f"⚠️  Telegram erreur : {r.status_code} — {r.text}")
    except Exception as e:
        print(f"⚠️  Telegram exception : {e}")

# ─────────────────────────────────────────────
# CONNEXION GARMIN
# ─────────────────────────────────────────────
def connect_garmin():
    """Connexion à Garmin Connect."""
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    try:
        client.login()
        print("✅ Connecté à Garmin Connect")
    except Exception as e:
        print(f"❌ Erreur de connexion : {e}")
    return client

# ─────────────────────────────────────────────
# RÉCUPÉRATION DES DONNÉES
# ─────────────────────────────────────────────
def fetch_data(client):
    """Récupère les métriques clés du jour et de la nuit précédente."""
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    data = {}

    def safe_div(val, divisor, decimals=0):
        if val is None:
            return "N/A"
        return round(val / divisor, decimals) if decimals else round(val / divisor)

    # Sommeil
    try:
        sleep = client.get_sleep_data(yesterday)
        daily = sleep.get("dailySleepDTO", {})
        data["sleep"] = {
            "score":          (daily.get("sleepScores") or {}).get("overall", {}).get("value", "N/A"),
            "duration_hours": safe_div(daily.get("sleepTimeSeconds"), 3600, 1),
            "deep_minutes":   safe_div(daily.get("deepSleepSeconds"), 60),
            "rem_minutes":    safe_div(daily.get("remSleepSeconds"), 60),
            "light_minutes":  safe_div(daily.get("lightSleepSeconds"), 60),
            "awake_minutes":  safe_div(daily.get("awakeSleepSeconds"), 60),
            "hrv_status":     daily.get("avgSleepStress", "N/A"),
            "resting_hr":     daily.get("restingHeartRate", "N/A"),
            "avg_spo2":       daily.get("averageSpO2Value", "N/A"),
        }
        print("✅ Données sommeil récupérées")
    except Exception as e:
        print(f"⚠️  Sommeil : {e}")
        data["sleep"] = {}

    # Body Battery
    try:
        bb = client.get_body_battery(today)
        bb_val = "N/A"
        if bb and len(bb) > 0:
            latest = bb[-1] if isinstance(bb, list) else bb
            if isinstance(latest, dict):
                bb_val = (latest.get("charged") or latest.get("bodyBatteryLevel") or latest.get("value") or "N/A")
            elif isinstance(latest, (int, float)):
                bb_val = int(latest)
        data["body_battery"] = bb_val
        print(f"✅ Body Battery récupéré : {bb_val}")
    except Exception as e:
        print(f"⚠️  Body Battery : {e}")
        data["body_battery"] = "N/A"

    # Stress
    try:
        stress = client.get_stress_data(today)
        data["stress_avg"] = stress.get("avgStressLevel", "N/A")
        print("✅ Stress récupéré")
    except Exception as e:
        print(f"⚠️  Stress : {e}")
        data["stress_avg"] = "N/A"

    # Activités
    try:
        activities = client.get_activities(0, 3)
        data["recent_activities"] = [
            {
                "date":     act.get("startTimeLocal", "")[:10],
                "type":     act.get("activityType", {}).get("typeKey", "unknown"),
                "duration": round(act.get("duration", 0) / 60),
                "distance": round(act.get("distance", 0) / 1000, 2),
                "avg_hr":   act.get("averageHR", "N/A"),
            }
            for act in activities
        ]
        print("✅ Activités récupérées")
    except Exception as e:
        print(f"⚠️  Activités : {e}")
        data["recent_activities"] = []

    return data

# ─────────────────────────────────────────────
# AFFICHAGE TERMINAL
# ─────────────────────────────────────────────
def print_data(data):
    print("\n" + "="*50)
    print("📊 TES DONNÉES DU JOUR")
    print("="*50)
    sleep = data.get("sleep", {})
    print(f"\n🌙 SOMMEIL")
    print(f"   Score         : {sleep.get('score', 'N/A')}/100")
    print(f"   Durée totale  : {sleep.get('duration_hours', 'N/A')}h")
    print(f"   Sommeil prof. : {sleep.get('deep_minutes', 'N/A')} min")
    print(f"   REM           : {sleep.get('rem_minutes', 'N/A')} min")
    print(f"   FC repos      : {sleep.get('resting_hr', 'N/A')} bpm")
    print(f"   SpO2 moy.     : {sleep.get('avg_spo2', 'N/A')} %")
    print(f"\n⚡ RÉCUPÉRATION")
    print(f"   Body Battery  : {data.get('body_battery', 'N/A')}/100")
    print(f"   Stress moyen  : {data.get('stress_avg', 'N/A')}/100")
    print(f"\n🏃 DERNIÈRES ACTIVITÉS")
    for act in data.get("recent_activities", []):
        print(f"   {act['date']} | {act['type']:15} | {act['duration']} min | {act['distance']} km | FC moy {act['avg_hr']} bpm")
    print("="*50)

# ─────────────────────────────────────────────
# RECOMMANDATION CLAUDE + ENVOI TELEGRAM
# ─────────────────────────────────────────────
def get_recommendation(data):
    """Appelle Claude, affiche la recommandation et l'envoie sur Telegram."""
    if not ANTHROPIC_KEY:
        print("\n⚠️  Clé Anthropic absente — recommandation IA désactivée")
        return None

    sleep   = data.get("sleep", {})
    battery = data.get("body_battery", "N/A")
    stress  = data.get("stress_avg", "N/A")
    acts    = data.get("recent_activities", [])

    prompt = f"""Tu es mon coach personnel. Mon objectif est de construire une base cardio solide pour les standards militaires (Cooper 3000m+, endurance longue distance). Je fais du rugby et du taekwondo en parallèle.

Voici mes données physiologiques du jour :

SOMMEIL (nuit dernière) :
- Score sommeil : {sleep.get('score', 'N/A')}/100
- Durée : {sleep.get('duration_hours', 'N/A')}h
- Sommeil profond : {sleep.get('deep_minutes', 'N/A')} min
- REM : {sleep.get('rem_minutes', 'N/A')} min
- FC repos : {sleep.get('resting_hr', 'N/A')} bpm
- SpO2 : {sleep.get('avg_spo2', 'N/A')} %

RÉCUPÉRATION :
- Body Battery : {battery}/100
- Stress moyen hier : {stress}/100

ACTIVITÉS RÉCENTES :
{json.dumps(acts, indent=2, ensure_ascii=False)}

Sur la base de ces données, dis-moi :
1. Mon niveau de récupération aujourd'hui (bon / moyen / insuffisant) en 2-3 phrases
2. Le bloc de séance recommandé parmi :
   - BLOC Z2 : 40-60 min course FC basse (< 75% FCmax)
   - BLOC TEMPO : 25-35 min FC modérée-haute (80-87% FCmax)
   - BLOC FRACTIONNÉ : 20-25 min efforts intenses (ex: 8x400m)
   - REPOS ACTIF : marche, mobilité uniquement
3. Les paramètres précis (durée, allure, zones FC)
4. Un signal d'alerte si une donnée est préoccupante

Sois direct et concis. Format texte simple sans markdown complexe, compatible Telegram."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    recommendation = response.content[0].text

    print("\n" + "="*50)
    print("🤖 RECOMMANDATION COACH IA")
    print("="*50)
    print(recommendation)
    print("="*50)

    # Formatage du message Telegram
    today_str = date.today().strftime("%d/%m/%Y")
    telegram_msg = (
        f"🏃 *Bilan santé — {today_str}*\n\n"
        f"🌙 Sommeil : {sleep.get('score', 'N/A')}/100 "
        f"({sleep.get('duration_hours', 'N/A')}h)\n"
        f"⚡ Body Battery : {battery}/100\n"
        f"😌 Stress : {stress}/100\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{recommendation}"
    )
    send_telegram(telegram_msg)
    return recommendation

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("🏃 Garmin Health Agent — démarrage\n")
    client = connect_garmin()
    data   = fetch_data(client)
    print_data(data)
    get_recommendation(data)
