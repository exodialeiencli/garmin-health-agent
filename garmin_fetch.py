"""
garmin_fetch.py — Garmin Health Agent v3.0
===========================================
Agent santé/coaching ESM Saint-Cyr.
Récupère les données Garmin du jour, lit le profil athlète + l'historique
persistant, génère une recommandation calibrée (Claude) et l'envoie sur Telegram.

Modèle d'entraînement : polarisé 80/20 (Seiler), prévention périostite (MTSS),
anti-désentraînement (Mujika & Padilla). Détails dans profil_athlete.json.

INSTALLATION : pip install garminconnect anthropic requests
SECRETS (GitHub Actions) : GARMIN_EMAIL, GARMIN_PASSWORD, ANTHROPIC_KEY,
                           TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Méthodes garminconnect vérifiées (v0.2.x) :
  get_sleep_data(cdate) -> dict
  get_body_battery(startdate, enddate=None) -> list[dict]   # ⚠️ liste par jour
  get_stress_data(cdate) -> dict
  get_training_readiness(cdate) -> list[dict]                # score récup Garmin
  get_max_metrics(cdate) -> dict                             # VO2max
  get_rhr_day(cdate) -> dict                                 # FC repos
  get_activities(start, limit) -> list[dict]
"""

import json, os, requests, logging
from datetime import date, timedelta
from garminconnect import Garmin
import anthropic

logging.getLogger("garminconnect").setLevel(logging.ERROR)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GARMIN_EMAIL     = os.environ.get("GARMIN_EMAIL",     "")
GARMIN_PASSWORD  = os.environ.get("GARMIN_PASSWORD",  "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PROFIL_FILE     = "profil_athlete.json"
HISTORIQUE_FILE = "historique.json"
MAX_ACTIVITES   = 14
MODELE_CLAUDE   = "claude-haiku-4-5-20251001"


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def safe_div(val, div, dec=0):
    if val is None:
        return "N/A"
    try:
        return round(val / div, dec) if dec else round(val / div)
    except (TypeError, ZeroDivisionError):
        return "N/A"

def kmh_to_pace(v):
    """km/h -> 'm:ss/km'."""
    if not v or v <= 0:
        return "N/A"
    p = 60 / v
    return f"{int(p)}:{int(round((p % 1) * 60)):02d}/km"


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram non configuré (secrets manquants)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        if r.status_code == 200:
            print("✅ Telegram envoyé")
        else:
            # Markdown peut casser ; on retente en texte brut
            print(f"⚠️  Telegram {r.status_code}, retry sans markdown")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=15,
            )
    except Exception as e:
        print(f"⚠️  Telegram exception: {e}")


# ─── GARMIN ───────────────────────────────────────────────────────────────────
def connect_garmin():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("❌ Identifiants Garmin manquants")
        return None
    try:
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        print("✅ Connecté à Garmin Connect")
        return client
    except Exception as e:
        print(f"❌ Erreur connexion Garmin: {e}")
        return None


def fetch_data(client):
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    data = {"_date": today}

    if client is None:
        # Mode dégradé : pas de données mais le script ne crashe pas
        data.update({"sleep": {}, "body_battery": "N/A", "stress_avg": "N/A",
                     "readiness": "N/A", "vo2max": "N/A", "recent_activities": []})
        return data

    # ── Sommeil : Garmin range la nuit sous la date du RÉVEIL (today) en priorité
    data["sleep"] = {}
    for try_date in (today, yesterday):
        try:
            raw   = client.get_sleep_data(try_date) or {}
            daily = raw.get("dailySleepDTO", {}) or {}
            score = (daily.get("sleepScores") or {}).get("overall", {}).get("value")
            dur   = daily.get("sleepTimeSeconds")
            if score is not None or dur is not None:
                data["sleep"] = {
                    "score":          score if score is not None else "N/A",
                    "duration_hours": safe_div(dur, 3600, 1),
                    "deep_minutes":   safe_div(daily.get("deepSleepSeconds"), 60),
                    "rem_minutes":    safe_div(daily.get("remSleepSeconds"), 60),
                    "light_minutes":  safe_div(daily.get("lightSleepSeconds"), 60),
                    "awake_minutes":  safe_div(daily.get("awakeSleepSeconds"), 60),
                    "resting_hr":     daily.get("restingHeartRate", "N/A"),
                    "avg_spo2":       daily.get("averageSpO2Value", "N/A"),
                }
                print(f"✅ Sommeil récupéré ({try_date})")
                break
        except Exception as e:
            print(f"⚠️  Sommeil {try_date}: {e}")
    if not data["sleep"]:
        print("⚠️  Pas de données sommeil (montre non portée la nuit ?)")

    # ── FC repos (fallback si absente du sommeil)
    if data["sleep"].get("resting_hr", "N/A") in ("N/A", None):
        try:
            rhr = client.get_rhr_day(today) or {}
            metrics = rhr.get("allMetrics", {}).get("metricsMap", {})
            rhr_list = metrics.get("WELLNESS_RESTING_HEART_RATE", [])
            if rhr_list and rhr_list[0].get("value"):
                data["sleep"]["resting_hr"] = int(rhr_list[0]["value"])
                print(f"✅ FC repos (fallback): {data['sleep']['resting_hr']} bpm")
        except Exception as e:
            print(f"⚠️  FC repos fallback: {e}")

    # ── Body Battery : get_body_battery renvoie une LISTE par jour
    data["body_battery"] = "N/A"
    try:
        bb = client.get_body_battery(today, today)
        if bb and isinstance(bb, list):
            day0 = bb[0] if isinstance(bb[0], dict) else {}
            # La dernière mesure de la journée se trouve dans bodyBatteryValuesArray
            arr = day0.get("bodyBatteryValuesArray") or []
            if arr and isinstance(arr[-1], list) and len(arr[-1]) >= 2:
                data["body_battery"] = arr[-1][1]   # [timestamp, niveau]
            else:
                data["body_battery"] = (day0.get("charged") or day0.get("bodyBatteryLevel") or "N/A")
        print(f"✅ Body Battery: {data['body_battery']}")
    except Exception as e:
        print(f"⚠️  Body Battery: {e}")

    # ── Stress
    data["stress_avg"] = "N/A"
    try:
        stress = client.get_stress_data(today) or {}
        data["stress_avg"] = stress.get("avgStressLevel", "N/A")
        print("✅ Stress récupéré")
    except Exception as e:
        print(f"⚠️  Stress: {e}")

    # ── Training Readiness (score de récup calculé par Garmin — plus fiable que BB seul)
    data["readiness"] = "N/A"
    try:
        tr = client.get_training_readiness(today)
        if tr and isinstance(tr, list) and tr[0].get("score") is not None:
            data["readiness"] = {
                "score": tr[0].get("score"),
                "level": tr[0].get("level", ""),
            }
            print(f"✅ Training Readiness: {data['readiness']['score']}")
    except Exception as e:
        print(f"⚠️  Training Readiness (non dispo sur tous appareils): {e}")

    # ── VO2max
    data["vo2max"] = "N/A"
    try:
        mm = client.get_max_metrics(today)
        if mm and isinstance(mm, list) and mm[0].get("generic"):
            data["vo2max"] = mm[0]["generic"].get("vo2MaxValue", "N/A")
            print(f"✅ VO2max: {data['vo2max']}")
    except Exception as e:
        print(f"⚠️  VO2max: {e}")

    # ── Activités (14 dernières, filtre < 8 min pour ignorer tractions isolées)
    data["recent_activities"] = []
    try:
        raw_acts = client.get_activities(0, MAX_ACTIVITES + 12) or []
        for a in raw_acts:
            dur_min = round((a.get("duration") or 0) / 60)
            if dur_min < 8:
                continue
            data["recent_activities"].append({
                "date":     (a.get("startTimeLocal") or "")[:10],
                "type":     a.get("activityType", {}).get("typeKey", "unknown"),
                "duration": dur_min,
                "distance": round((a.get("distance") or 0) / 1000, 2),
                "avg_hr":   a.get("averageHR"),
                "max_hr":   a.get("maxHR"),
            })
            if len(data["recent_activities"]) >= MAX_ACTIVITES:
                break
        print(f"✅ {len(data['recent_activities'])} activités récupérées")
    except Exception as e:
        print(f"⚠️  Activités: {e}")

    return data


# ─── AFFICHAGE TERMINAL ───────────────────────────────────────────────────────
def print_data(data):
    s = data.get("sleep", {})
    rd = data.get("readiness", "N/A")
    rd_str = rd.get("score") if isinstance(rd, dict) else rd
    print("\n" + "=" * 54)
    print("📊 DONNÉES DU JOUR")
    print("=" * 54)
    print(f"🌙 Sommeil   : {s.get('score','N/A')}/100 — {s.get('duration_hours','N/A')}h "
          f"(profond {s.get('deep_minutes','N/A')}min, REM {s.get('rem_minutes','N/A')}min)")
    print(f"❤️  FC repos  : {s.get('resting_hr','N/A')} bpm | SpO2 {s.get('avg_spo2','N/A')}%")
    print(f"⚡ Body Batt : {data.get('body_battery','N/A')}/100")
    print(f"😌 Stress    : {data.get('stress_avg','N/A')}/100")
    print(f"🎯 Readiness : {rd_str}/100")
    print(f"📈 VO2max    : {data.get('vo2max','N/A')}")
    print(f"\n🏃 {len(data.get('recent_activities',[]))} activités récentes:")
    for a in data.get("recent_activities", [])[:5]:
        print(f"   {a['date']} | {a['type']:18} | {a['duration']}min | "
              f"{a['distance']}km | FC {a.get('avg_hr','?')}")
    print("=" * 54)


# ─── HISTORIQUE PERSISTANT ────────────────────────────────────────────────────
def load_historique():
    if os.path.exists(HISTORIQUE_FILE):
        try:
            return json.load(open(HISTORIQUE_FILE))
        except Exception:
            pass
    return {"entrees": [], "stats_30j": {}, "derniere_maj": None}


def save_historique(hist, data):
    today = date.today().isoformat()
    s  = data.get("sleep", {})
    rd = data.get("readiness", "N/A")
    rd_score = rd.get("score") if isinstance(rd, dict) else "N/A"

    entree = {
        "date":         today,
        "sleep_score":  s.get("score", "N/A"),
        "sleep_hours":  s.get("duration_hours", "N/A"),
        "body_battery": data.get("body_battery", "N/A"),
        "stress":       data.get("stress_avg", "N/A"),
        "resting_hr":   s.get("resting_hr", "N/A"),
        "readiness":    rd_score,
        "vo2max":       data.get("vo2max", "N/A"),
        "nb_activites": len(data.get("recent_activities", [])),
    }

    hist["entrees"] = [e for e in hist["entrees"] if e.get("date") != today]
    hist["entrees"].append(entree)
    hist["entrees"] = sorted(hist["entrees"], key=lambda x: x["date"])[-120:]  # 120j max

    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent = [e for e in hist["entrees"] if e.get("date", "") >= cutoff]

    def avg(lst, key):
        vals = [e[key] for e in lst if isinstance(e.get(key), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else "N/A"

    hist["stats_30j"] = {
        "fc_repos_moy":     avg(recent, "resting_hr"),
        "sleep_score_moy":  avg(recent, "sleep_score"),
        "body_battery_moy": avg(recent, "body_battery"),
        "stress_moy":       avg(recent, "stress"),
        "readiness_moy":    avg(recent, "readiness"),
        "nb_jours_actifs":  sum(1 for e in recent if isinstance(e.get("nb_activites"), int) and e["nb_activites"] > 0),
        "nb_jours_suivis":  len(recent),
    }
    hist["derniere_maj"] = today

    try:
        json.dump(hist, open(HISTORIQUE_FILE, "w"), indent=2, ensure_ascii=False)
        print("✅ Historique mis à jour")
    except Exception as e:
        print(f"⚠️  Sauvegarde historique: {e}")
    return hist


# ─── RECOMMANDATION CLAUDE ────────────────────────────────────────────────────
def build_prompt(data, profil, hist):
    today       = date.today()
    jour_sem    = today.strftime("%A")
    est_weekend = jour_sem in ("Saturday", "Sunday")

    phys = profil.get("physiologie", {})
    vma  = phys.get("vma_testee_kmh") or phys.get("vma_estimee_kmh", 13.7)
    vma_status = "TESTÉE ✅" if phys.get("vma_testee_kmh") else "estimée ⚠️"

    z2_lo = kmh_to_pace(vma * 0.65)
    z2_hi = kmh_to_pace(vma * 0.72)
    tempo = kmh_to_pace(vma * 0.85)
    vmax  = kmh_to_pace(vma)

    s       = data.get("sleep", {})
    rd      = data.get("readiness", "N/A")
    rd_str  = f"{rd['score']}/100 ({rd.get('level','')})" if isinstance(rd, dict) else "N/A"
    stats   = hist.get("stats_30j", {})
    acts    = data.get("recent_activities", [])

    profil_resume = (
        f"Mathurin, 22 ans, objectif ESM Saint-Cyr 2028 — épreuve critique 3000m (cible 11:30).\n"
        f"VO2max {phys.get('vo2max_actuel')} (montant), VMA {vma} km/h ({vma_status}), "
        f"FCmax {phys.get('fcmax_bpm')} bpm.\n"
        f"Antécédent: périostite tibiale bilatérale — PRÉVENTION PRIORITAIRE.\n"
        f"Forts: sprint, tractions. Faibles: vitesse spécifique 3000m, base aérobie, abdos ESM."
    )

    consignes_jour = (
        "WEEKEND — Mathurin ne s'entraîne quasi jamais le weekend (4% sur 5 ans). "
        "Propose UNIQUEMENT une routine bien-être de 15-20 min : mobilité hanches/chevilles, "
        "étirements mollets (prévention périostite), gainage léger, respiration. Pas de séance structurée."
        if est_weekend else
        "Jour d'entraînement (lun-ven). Phase 1 = construction base aérobie POLARISÉE: "
        "~90% du travail en Z2 facile (allures ci-dessous), accélérations courtes possibles. "
        "PAS de fractionné intense (base pas encore prête). Renforcement mollets/tibias = prévention périostite obligatoire."
    )

    prompt = f"""Tu es le coach sportif personnel de Mathurin. Modèle d'entraînement: POLARISÉ 80/20 (Seiler), prévention périostite (MTSS), anti-désentraînement (Mujika).

== PROFIL ==
{profil_resume}

== ZONES (VMA {vma} km/h) ==
- Z2 endurance (le pain quotidien): {z2_hi} à {z2_lo}, FC ~135-150 bpm. C'est ICI que se construit la base.
- Tempo/seuil: {tempo}, FC ~160-170 (Phase 2, pas maintenant)
- VMA: {vmax} (Phase 2+)
⚠️ PIÈGE À ÉVITER: la "zone grise" (courir le facile trop dur). Les jours Z2 doivent être VRAIMENT faciles.

== DONNÉES DU JOUR ({today.strftime('%A %d/%m/%Y')}) ==
Sommeil: {s.get('score','N/A')}/100 — {s.get('duration_hours','N/A')}h (profond {s.get('deep_minutes','N/A')}min, REM {s.get('rem_minutes','N/A')}min)
FC repos: {s.get('resting_hr','N/A')} bpm (réf forme: 54) | Body Battery: {data.get('body_battery','N/A')}/100 | Stress: {data.get('stress_avg','N/A')}/100
Training Readiness Garmin: {rd_str}
VO2max actuel: {data.get('vo2max','N/A')}

Tendances 30j: FC repos moy {stats.get('fc_repos_moy','N/A')} | Readiness moy {stats.get('readiness_moy','N/A')} | {stats.get('nb_jours_actifs','N/A')} jours actifs/{stats.get('nb_jours_suivis','N/A')}

14 dernières activités:
{json.dumps(acts, indent=1, ensure_ascii=False) if acts else "Aucune activité récente — attention reprise progressive (anti-périostite)."}

== CONTEXTE JOUR ==
{consignes_jour}

== FORMAT DE RÉPONSE (Telegram, texte simple, concis) ==
1. RÉCUP: niveau (EXCELLENT/BON/MOYEN/FAIBLE) en t'appuyant en priorité sur le Training Readiness s'il existe, sinon Body Battery + sommeil + FC repos. 1-2 phrases.
2. SÉANCE DU JOUR: {'routine bien-être détaillée' if est_weekend else 'le bloc principal recommandé avec paramètres précis (durée, allure en min/km RÉALISTE, FC cible)'}.
{'' if est_weekend else '3. Si récup BON/EXCELLENT: tu peux proposer 1 bloc cardio + 1 bloc muscu le même jour (haut du corps = tractions/abdos/pompes, ou bas = squats/fentes/MOLLETS pour prévention).'}
{'' if est_weekend else '4. RAPPEL PÉRIOSTITE: si une activité récente montre une grosse charge de course, privilégie vélo/repos aujourd_hui. Toute douleur tibia = stop course.'}
{'3. Un mot encourageant bref.' if est_weekend else '5. ALERTE si: FC repos > 68, Readiness < 35, ou gap d_activité > 6 jours.'}

Allures réalistes seulement (ce niveau court actuellement ~5:30-6:45/km selon l'intensité — JAMAIS 4:30/km en Z2). Pas de markdown lourd."""
    return prompt, vma, vma_status, est_weekend, jour_sem


def get_recommendation(data, profil, hist):
    if not ANTHROPIC_KEY:
        print("⚠️  Clé Anthropic absente — pas de reco générée")
        return None

    prompt, vma, vma_status, est_weekend, jour_sem = build_prompt(data, profil, hist)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=MODELE_CLAUDE, max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        reco = resp.content[0].text
    except Exception as e:
        print(f"❌ Erreur API Claude: {e}")
        return None

    print("\n" + "=" * 54)
    print("🤖 RECOMMANDATION COACH IA")
    print("=" * 54)
    print(reco)
    print("=" * 54)

    s   = data.get("sleep", {})
    rd  = data.get("readiness", "N/A")
    rd_str = f"{rd['score']}/100" if isinstance(rd, dict) else "N/A"
    phys = profil.get("physiologie", {})

    msg = (
        f"🏃 *Bilan {date.today().strftime('%d/%m/%Y')}* — {jour_sem}\n\n"
        f"🌙 Sommeil : {s.get('score','N/A')}/100 ({s.get('duration_hours','N/A')}h)\n"
        f"❤️ FC repos : {s.get('resting_hr','N/A')} bpm\n"
        f"⚡ Body Battery : {data.get('body_battery','N/A')}/100\n"
        f"🎯 Readiness : {rd_str}\n"
        f"📈 VO2max : {data.get('vo2max', phys.get('vo2max_actuel','?'))} | VMA : {vma} km/h ({vma_status})\n\n"
        f"━━━━━━━━━━━━━━━\n\n{reco}"
    )
    send_telegram(msg)
    return reco


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🏃 Garmin Health Agent v3.0\n")

    profil = {}
    if os.path.exists(PROFIL_FILE):
        try:
            profil = json.load(open(PROFIL_FILE))
            print(f"✅ Profil chargé (VO2max {profil.get('physiologie',{}).get('vo2max_actuel','?')}, "
                  f"VMA {profil.get('physiologie',{}).get('vma_testee_kmh') or profil.get('physiologie',{}).get('vma_estimee_kmh','?')})")
        except Exception as e:
            print(f"⚠️  Profil illisible: {e}")
    else:
        print("⚠️  profil_athlete.json introuvable")

    hist = load_historique()
    print(f"✅ Historique: {len(hist.get('entrees',[]))} jours enregistrés")

    client = connect_garmin()
    data   = fetch_data(client)
    print_data(data)

    hist = save_historique(hist, data)
    get_recommendation(data, profil, hist)
