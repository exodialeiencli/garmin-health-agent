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

import json, os, requests, logging, base64, tarfile, io
from datetime import date, timedelta
from garminconnect import Garmin
import anthropic

logging.getLogger("garminconnect").setLevel(logging.ERROR)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GARMIN_TOKEN_BASE64 = os.environ.get("GARMIN_TOKEN_BASE64", "")
GARMIN_EMAIL     = os.environ.get("GARMIN_EMAIL",     "")
GARMIN_PASSWORD  = os.environ.get("GARMIN_PASSWORD",  "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PROFIL_FILE     = "profil_athlete.json"
HISTORIQUE_FILE = "historique.json"
MAX_ACTIVITES   = 14     # activités affichées dans le prompt (les plus récentes)
FETCH_ACTIVITES = 40     # activités récupérées pour alimenter la mémoire persistante
STORE_JOURS     = 120    # fenêtre glissante du stock d'activités dans historique.json
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
def send_telegram(message, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram non configuré (secrets manquants)")
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
        if r.status_code == 200:
            print("✅ Telegram envoyé")
        else:
            # Markdown peut casser ; on retente en texte brut
            print(f"⚠️  Telegram {r.status_code}, retry sans markdown")
            payload.pop("parse_mode", None)
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
                timeout=15,
            )
    except Exception as e:
        print(f"⚠️  Telegram exception: {e}")


# Boutons inline pour la dispo du jour (architecture deux temps)
DISPO_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "🟢 Beaucoup", "callback_data": "dispo:beaucoup"},
        {"text": "🟡 Un peu",   "callback_data": "dispo:court"},
        {"text": "🔴 Pas le temps", "callback_data": "dispo:rien"},
    ]]
}


# ─── GARMIN ───────────────────────────────────────────────────────────────────
TOKENSTORE = os.path.expanduser("~/.garminconnect")

def connect_garmin():
    # 1) AUTH PAR TOKEN (priorité — évite le rate-limit 429 des logins répétés)
    #    GARMIN_TOKEN_BASE64 = archive tar.gz du dossier ~/.garminconnect,
    #    générée une fois en local par generer_token_garmin.py.
    if GARMIN_TOKEN_BASE64:
        try:
            os.makedirs(TOKENSTORE, exist_ok=True)
            raw = base64.b64decode(GARMIN_TOKEN_BASE64)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                tar.extractall(TOKENSTORE)
            client = Garmin()
            client.login(TOKENSTORE)   # reprend la session sans email/mdp
            print("✅ Connecté à Garmin (token)")
            return client
        except Exception as e:
            print(f"⚠️  Auth token échouée ({e}) — fallback email/mot de passe")

    # 2) FALLBACK email/mot de passe (si token absent ou expiré)
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("❌ Aucun moyen d'auth Garmin (ni token valide ni email/mdp)")
        return None
    try:
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        print("✅ Connecté à Garmin (email/mdp)")
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

    # ── Activités : on en récupère plus (FETCH_ACTIVITES) pour alimenter la mémoire
    #    persistante, en capturant l'activityId (clé de déduplication fiable).
    #    Filtre < 8 min pour ignorer les tractions/séries isolées.
    data["recent_activities"] = []
    try:
        raw_acts = client.get_activities(0, FETCH_ACTIVITES) or []
        for a in raw_acts:
            dur_min = round((a.get("duration") or 0) / 60)
            if dur_min < 8:
                continue
            data["recent_activities"].append({
                "id":       a.get("activityId"),
                "date":     (a.get("startTimeLocal") or "")[:10],
                "type":     a.get("activityType", {}).get("typeKey", "unknown"),
                "duration": dur_min,
                "distance": round((a.get("distance") or 0) / 1000, 2),
                "avg_hr":   a.get("averageHR"),
                "max_hr":   a.get("maxHR"),
            })
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
def _cle_activite(a):
    """Clé de déduplication : activityId Garmin si présent, sinon clé composite."""
    if a.get("id"):
        return f"id:{a['id']}"
    return f"{a.get('date')}|{a.get('type')}|{a.get('duration')}|{a.get('distance')}"


def fusionner_activites(existantes, nouvelles, today=None, jours=STORE_JOURS):
    """Fusionne le stock persistant et les activités fraîches (les fraîches priment),
    déduplique, garde la fenêtre glissante `jours`, trie par date croissante."""
    today = today or date.today()
    par_cle = {}
    for a in (existantes or []):
        if a.get("date"):
            par_cle[_cle_activite(a)] = a
    for a in (nouvelles or []):           # les fraîches écrasent (données plus à jour)
        if a.get("date"):
            par_cle[_cle_activite(a)] = a
    cutoff = (today - timedelta(days=jours)).isoformat()
    fusion = [a for a in par_cle.values() if a.get("date", "") >= cutoff]
    fusion.sort(key=lambda x: (x.get("date", ""), x.get("type", "")))
    return fusion


def load_historique():
    if os.path.exists(HISTORIQUE_FILE):
        try:
            h = json.load(open(HISTORIQUE_FILE))
            h.setdefault("activites", [])      # rétro-compat: stock d'activités
            return h
        except Exception:
            pass
    return {"entrees": [], "activites": [], "stats_30j": {}, "derniere_maj": None}


def save_historique(hist, data):
    today = date.today().isoformat()
    s  = data.get("sleep", {})
    rd = data.get("readiness", "N/A")
    rd_score = rd.get("score") if isinstance(rd, dict) else "N/A"

    # ── Mémoire d'entraînement : fusion des activités dans le stock persistant
    hist["activites"] = fusionner_activites(
        hist.get("activites", []), data.get("recent_activities", []))

    # On préserve dispo + feedbacks éventuellement déjà écrits par le listener aujourd'hui
    existant = next((e for e in hist["entrees"] if e.get("date") == today), {})

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
        # Champs pilotés par le listener Telegram (préservés s'ils existent déjà)
        "dispo":        existant.get("dispo"),
        "feedbacks":    existant.get("feedbacks", []),
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
def _classer_type(typekey):
    """Catégorise une activité Garmin en famille d'entraînement."""
    t = (typekey or "").lower()
    if "run" in t:                                   return "course"
    if any(k in t for k in ("cycl", "bik", "velo")): return "velo"
    if any(k in t for k in ("strength", "training", "gym", "weight", "muscu")): return "renfo"
    if any(k in t for k in ("walk", "hik")):         return "marche"
    if "swim" in t:                                  return "natation"
    return "autre"


def _intensite_course(avg_hr, z2_ceiling, z4_start):
    """Classe l'intensité d'une course via la FC moyenne."""
    if not avg_hr:
        return "?"
    if avg_hr <= z2_ceiling + 2:  return "facile"   # Z1-Z2
    if avg_hr < z4_start:         return "modérée"  # Z3 (zone grise)
    return "dure"                                   # Z4-Z5


def analyser_microcycle(acts, today, z2_ceiling=152, z4_start=162):
    """
    Analyse les 7 derniers jours comme le ferait un entraîneur :
    volume course, jours d'impact, intensité, dernière séance, alternance,
    jours depuis dernière course / dernière séance dure / dernière longue.
    Retourne un dict de faits + un résumé textuel.
    """
    j7 = today - timedelta(days=7)
    sem = []
    for a in acts:
        try:
            adate = date.fromisoformat(a.get("date", ""))
        except Exception:
            continue
        if adate >= j7:
            sem.append((adate, a))
    sem.sort(key=lambda x: x[0], reverse=True)   # plus récent d'abord

    run_min = run_km = 0
    jours_course, jours_velo, jours_renfo = set(), set(), set()
    nb_dures = nb_longues = 0
    derniere = None
    j_depuis_course = j_depuis_dure = j_depuis_longue = None
    detail = []

    for adate, a in sem:
        fam = _classer_type(a.get("type"))
        dur = a.get("duration") or 0
        dist = a.get("distance") or 0
        intens = _intensite_course(a.get("avg_hr"), z2_ceiling, z4_start) if fam == "course" else "-"
        jdiff = (today - adate).days
        if fam == "course":
            run_min += dur; run_km += dist; jours_course.add(adate)
            if j_depuis_course is None: j_depuis_course = jdiff
            if intens == "dure":
                nb_dures += 1
                if j_depuis_dure is None: j_depuis_dure = jdiff
            if dur >= 45:
                nb_longues += 1
                if j_depuis_longue is None: j_depuis_longue = jdiff
        elif fam == "velo":
            jours_velo.add(adate)
        elif fam == "renfo":
            jours_renfo.add(adate)
        if derniere is None:
            derniere = {"jours": jdiff, "fam": fam, "dur": dur, "intens": intens}
        detail.append(f"J-{jdiff} {fam} {dur}min"
                      + (f"/{round(dist,1)}km/{intens}" if fam == 'course' else ""))

    # Jours de course consécutifs en terminant à J-1
    streak = 0
    d = today - timedelta(days=1)
    while d in jours_course:
        streak += 1; d -= timedelta(days=1)

    # Volume course de la semaine PRÉCÉDENTE (J-8 à J-14) pour la règle des +10%
    run_min_prev = 0
    j14, j8 = today - timedelta(days=14), today - timedelta(days=8)
    for a in acts:
        try:
            adate = date.fromisoformat(a.get("date", ""))
        except Exception:
            continue
        if j14 <= adate <= j8 and _classer_type(a.get("type")) == "course":
            run_min_prev += a.get("duration") or 0
    if run_min_prev > 0:
        delta = round((run_min - run_min_prev) / run_min_prev * 100)
        evo_txt = (f"Volume course: {round(run_min)}min cette semaine vs {round(run_min_prev)}min "
                   f"la précédente ({'+' if delta >= 0 else ''}{delta}%). "
                   + ("⚠️ Hausse >10% = risque MTSS, plafonne." if delta > 10 else "Progression maîtrisée."))
    else:
        evo_txt = f"Volume course semaine précédente: insuffisant pour comparer (reprise)."

    resume = (
        f"7 derniers jours: {round(run_min)}min course / {round(run_km,1)}km sur {len(jours_course)}j, "
        f"vélo {len(jours_velo)}j, renfo {len(jours_renfo)}j. "
        f"Séances dures: {nb_dures}, longues (≥45min): {nb_longues}. "
        f"Jours course consécutifs (fin J-1): {streak}.\n"
        f"{evo_txt}\n"
        f"Dernière séance: " + (f"J-{derniere['jours']} {derniere['fam']} {derniere['dur']}min "
        f"({derniere['intens']})" if derniere else "aucune cette semaine") + ".\n"
        f"Depuis dernière course: {j_depuis_course if j_depuis_course is not None else '7+'}j | "
        f"dernière séance dure: {j_depuis_dure if j_depuis_dure is not None else '7+'}j | "
        f"dernière longue: {j_depuis_longue if j_depuis_longue is not None else '7+'}j.\n"
        f"Détail: " + (" ; ".join(detail) if detail else "rien")
    )
    return {
        "run_min": run_min, "run_km": run_km,
        "jours_course": len(jours_course), "jours_velo": len(jours_velo),
        "jours_renfo": len(jours_renfo), "nb_dures": nb_dures, "nb_longues": nb_longues,
        "streak_course": streak, "derniere": derniere,
        "j_depuis_course": j_depuis_course, "j_depuis_dure": j_depuis_dure,
        "j_depuis_longue": j_depuis_longue, "resume": resume,
    }


def phase_courante(profil, today):
    """Détermine la phase du plan 24 mois à partir de la date du jour."""
    plan = profil.get("plan_progression_24_mois", {})
    ym = today.strftime("%Y-%m")
    for key, ph in plan.items():
        per = ph.get("periode", "")
        bornes = [b.strip() for b in per.replace("à", "-").split("-") if b.strip()]
        # periode du type "2026-06 à 2026-12"
        if len(bornes) >= 4:
            debut = f"{bornes[0]}-{bornes[1]}"
            fin   = f"{bornes[2]}-{bornes[3]}"
            if debut <= ym <= fin:
                return key, ph
    # défaut: phase 1
    return "phase_1_base", plan.get("phase_1_base", {})


def build_prompt(data, profil, hist, mode="brief", dispo=None):
    """
    mode='brief'  : message du matin = bilan santé + invitation à donner sa dispo.
                    PAS de séance imposée (architecture deux temps).
    mode='seance' : génération de LA séance adaptée à la dispo reçue (dispo='beaucoup'|'court'|'rien').
    """
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

    # Allure Z2 RÉELLE pilotée par la FC (vérité empirique du test VMA), pas la
    # valeur théorique optimiste. À 6:40/km Mathurin est déjà en Z3 → on prescrit la FC.
    z2_reel = profil.get("zones_allures", {}).get(
        "z2_endurance_reel_par_fc", f"{z2_hi}–{z2_lo} (FC plafond ~150)")
    z2_court = z2_reel.split(" — ")[0].strip()   # ex: "7:00-7:30/km (FC plafond ~150)"

    s       = data.get("sleep", {})
    rd      = data.get("readiness", "N/A")
    rd_str  = f"{rd['score']}/100 ({rd.get('level','')})" if isinstance(rd, dict) else "N/A"
    stats   = hist.get("stats_30j", {})
    # Mémoire longue : union du stock persistant + activités fraîches (dédupliqué).
    # Fonctionne quel que soit l'ordre save/reco. acts = liste complète (jusqu'à 120j).
    acts    = fusionner_activites(hist.get("activites", []),
                                  data.get("recent_activities", []), today)
    acts_recentes = sorted(acts, key=lambda x: x.get("date", ""), reverse=True)[:MAX_ACTIVITES]

    # ── ANALYSE DU MICROCYCLE (la semaine) + phase du plan 24 mois
    zfc = profil.get("zones_fc_officielles", {})
    # Plafond Z2 FONCTIONNEL (~152) : le profil note que son facile/talk-test va jusqu'à
    # ~152 et qu'il ne faut pas le forcer sous 140. On classe le "facile" là-dessus,
    # sinon ses footings Z2 réels seraient comptés à tort comme du Z3.
    z2_ceiling = 152
    z4_start   = (zfc.get("z4_seuil", [162, 183]) or [162, 183])[0]
    analyse = analyser_microcycle(acts, today, z2_ceiling, z4_start)
    phase_key, phase = phase_courante(profil, today)

    # Feedbacks récents (3 derniers jours) pour contexte
    cutoff_fb = (today - timedelta(days=3)).isoformat()
    feedbacks_recents = []
    for e in hist.get("entrees", []):
        if e.get("date", "") >= cutoff_fb and e.get("feedbacks"):
            for fb in e["feedbacks"]:
                feedbacks_recents.append(f"{e['date']}: {fb}")

    profil_resume = (
        f"Mathurin, 22 ans, objectif ESM Saint-Cyr 2028 — épreuve critique 3000m (cible 11:30).\n"
        f"VO2max {phys.get('vo2max_actuel')} (montant), VMA {vma} km/h ({vma_status}), "
        f"FCmax {phys.get('fcmax_bpm')} bpm.\n"
        f"Antécédent: périostite tibiale bilatérale — PRÉVENTION PRIORITAIRE.\n"
        f"Forts: sprint, tractions. Faibles: vitesse spécifique 3000m, base aérobie, abdos ESM."
    )

    # ── Consignes selon mode + dispo ──────────────────────────────────────────
    dispo_txt = {
        "beaucoup": "Mathurin a BEAUCOUP de temps → séance complète possible, MAIS choisis dans le MENU selon l'ANALYSE DU MICROCYCLE et les RÈGLES DE RÉCUPÉRATION. Si la charge d'impact récente l'interdit, ce temps va en vélo + renfo + mobilité, PAS en 2e longue course.",
        "court":    "Mathurin a PEU de temps aujourd'hui → format court et efficace (séance minimale de maintien, anti-désentraînement Mujika: réduire volume mais garder qualité).",
        "rien":     "Mathurin n'a PAS le temps de s'entraîner aujourd'hui → propose UNIQUEMENT 10-15 min bien-être/mobilité/étirements (focus mollets-tibias). Aucune séance structurée.",
    }

    if est_weekend:
        consignes_jour = (
            "WEEKEND — Mathurin ne s'entraîne quasi jamais le weekend (4% sur 5 ans). "
            "Propose UNIQUEMENT une routine bien-être de 15-20 min : mobilité hanches/chevilles, "
            "étirements mollets (prévention périostite), gainage léger, respiration. Pas de séance structurée."
        )
    elif mode == "brief":
        consignes_jour = (
            "MODE BRIEF (message du matin, architecture deux temps) : tu fais UNIQUEMENT le bilan santé "
            "et tu INVITES Mathurin à indiquer sa dispo du jour (beaucoup / court / pas le temps) via les boutons. "
            "NE PROPOSE PAS encore de séance détaillée — elle sera générée après sa réponse. "
            "Tu peux juste teaser l'orientation du jour en une phrase (ex: 'récup bonne, on pourra pousser un peu')."
        )
    else:  # mode == 'seance'
        consignes_jour = (
            "MODE SÉANCE (Mathurin a donné sa dispo) : Phase 1 = base aérobie POLARISÉE, ~90% Z2 facile, "
            "PAS de fractionné intense. Renforcement mollets/tibias = prévention périostite obligatoire.\n"
            + dispo_txt.get(dispo, "Dispo non précisée → propose une séance Z2 standard modérée.")
        )

    prompt = f"""Tu es le coach sportif personnel de Mathurin. Modèle d'entraînement: POLARISÉ 80/20 (Seiler), prévention périostite (MTSS), anti-désentraînement (Mujika).

== PROFIL ==
{profil_resume}

== ZONES (VMA {vma} km/h) ==
- Z2 endurance (le pain quotidien): {z2_reel}. PILOTÉ PAR LA FC (plafond ~150 bpm), PAS par le chrono — le chrono suivra avec la base. C'est ICI que tout se construit. (Repère théorique VMA: {z2_hi}–{z2_lo}, mais à l'usage Mathurin part trop vite, on tient la FC.)
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
{json.dumps(acts_recentes, indent=1, ensure_ascii=False) if acts_recentes else "Aucune activité récente — attention reprise progressive (anti-périostite)."}

== ANALYSE DU MICROCYCLE (raisonne comme un entraîneur AVANT de décider) ==
{analyse['resume']}

== PHASE ACTUELLE DU PLAN 24 MOIS : {phase_key} ==
Objectif: {phase.get('objectif','base aérobie')}
Distribution visée: {phase.get('distribution','~90% Z2, pas de fractionné intense')}

== MENU DE SÉANCES (choisis LA plus pertinente selon l'analyse ci-dessus + phase + récup + dispo) ==
A. COURSE FACILE Z2 — 30-50min, {z2_court}. Le pain quotidien. NE PAS enchaîner 2 jours d'impact course si streak élevé ou tibias sensibles.
B. SORTIE LONGUE Z2 — 50-75min facile. MAX 1×/semaine en Phase 1. Jamais 2 longues à <72h d'écart.
C. VÉLO (home-trainer) — 40-75min Z2. Zéro impact tibial: l'outil de choix pour garder du volume aérobie SANS charger le tibia (lendemain de course, streak course ≥2, ou moindre gêne).
D. RENFO BAS DU CORPS / ANTI-PÉRIOSTITE — excentrique mollets (gastro+soléaire), tibial postérieur, proprioception, gainage. 2×/sem, non négociable.
E. RENFO HAUT DU CORPS ESM — tractions, pompes, abdos protocole (cible 42+). Compatible un lendemain de course (pas d'impact jambes).
F. MOBILITÉ / RÉCUP ACTIVE — 15-20min, jour de faible dispo ou récup MOYENNE/FAIBLE.
G. LIGNES DROITES / STRIDES — 4-6×80m en fin de footing facile. SEUL travail "vif" autorisé en Phase 1 (neuromusculaire, faible risque).
{'H. TEMPO/SEUIL et I. FRACTIONNÉ VMA — INTERDITS en Phase 1 (base seulement). Ne PAS proposer.' if phase_key == 'phase_1_base' else 'H. TEMPO/SEUIL — bloc à allure seuil. I. FRACTIONNÉ VMA — type 30-30 ou 5×1000m. Autorisés selon la phase, max 2 séances dures/semaine, jamais 2 dures consécutives.'}

== RÈGLES DE RÉCUPÉRATION (un bon entraîneur ne se trompe pas — priorité ANTI-BLESSURE) ==
1. ALTERNANCE DUR/FACILE: jamais 2 séances dures (ou 2 longues) consécutives. Après une longue/dure → facile, vélo, renfo ou repos.
2. IMPACT TIBIAL: si {analyse['streak_course']} jours de course consécutifs ≥2, ou course ≥45min sur J-1 → aujourd'hui SANS impact (vélo/renfo/mobilité) ou course très courte ≤25min. Antécédent MTSS = on protège le tibia en priorité.
3. POLARISÉ 80/20: garde ~80% du volume facile. Si la semaine a déjà du dur/modéré, aujourd'hui = facile.
4. PROGRESSION: volume course hebdo +10% MAX vs semaine précédente. Pas de pic isolé.
5. RÉCUP DU JOUR: si readiness/sommeil/FC repos dégradés → rabote (vélo/renfo/mobilité plutôt que course longue), même si dispo "beaucoup".
6. "Beaucoup de temps" se traduit en QUALITÉ/COMPLÉMENTS (renfo + mobilité + vélo), pas en volume d'impact supplémentaire.
7. Au moindre signal tibia/cheville → STOP course, bascule vélo/repos, signale-le.

Feedbacks récents de Mathurin (3 derniers jours, ressenti/douleurs/exécution — PRENDS-EN COMPTE):
{chr(10).join(feedbacks_recents) if feedbacks_recents else "Aucun feedback récent."}

== CONTEXTE JOUR ==
{consignes_jour}

== ÉTIREMENTS & MOBILITÉ (structure à 3 temps — module selon la séance faite/prévue) ==
PRINCIPE: on couvre TOUJOURS tout le corps, mais on RENFORCE le focus sur les muscles sollicités par la séance du jour. Mollets + tibial = PRIORITAIRES dans tous les cas (anti-périostite, non négociable).

• AVANT séance → MOBILITÉ DYNAMIQUE seulement (jamais de statique long qui réduit la force):
  cercles chevilles, montées de genoux, talons-fesses, balancements de jambes, rotations hanches/épaules. 5 min.

• APRÈS séance → STATIQUE tenu 30-45s, focus MODULÉ selon ce qui a été fait:
  - Socle systématique (tout le corps, 1 étirement court/groupe): mollets+tibial (PRIORITÉ), ischios, quadriceps, fléchisseurs hanche, épaules/dos.
  - Si COURSE (Z2): focus chaîne postérieure → mollets ++, ischios ++, fléchisseurs hanche, bandelette IT (tenus plus longtemps).
  - Si HAUT DU CORPS (tractions/pompes): focus dorsaux, biceps, avant-bras, pecs, épaules — MAIS garder le socle jambes.
  - Si RENFO JAMBES/MOLLETS: focus quadriceps, fessiers, mollets en excentrique léger. Surveiller récup tibiale.

• JOUR OFF / BIEN-ÊTRE → routine étirements COMPLÈTE tout le corps + mobilité approfondie (10-15 min).

RAPPEL anti-périostite: pour les mollets, le RENFORCEMENT EXCENTRIQUE (déjà au programme) prime sur l'étirement statique. Statique = après séance ou en journée séparée, jamais en pré-séance.

== FORMAT DE RÉPONSE (Telegram, texte simple, concis) ==
{'''MODE BRIEF — réponds en 2 parties SEULEMENT:
1. RÉCUP: niveau (EXCELLENT/BON/MOYEN/FAIBLE) à partir du Training Readiness si dispo, sinon Body Battery + sommeil + FC repos. 1-2 phrases.
2. ORIENTATION: une phrase qui teste l'orientation du jour selon la récup (ex: "récup bonne, on pourra pousser" / "récup moyenne, on restera léger"), puis invite explicitement à choisir la dispo du jour via les boutons ci-dessous. NE DÉTAILLE PAS de séance.
5. ALERTE si: FC repos > 68, Readiness < 35, ou gap d_activité > 6 jours.''' if (mode == "brief" and not est_weekend) else '''
1. RÉCUP: niveau (EXCELLENT/BON/MOYEN/FAIBLE) en t'appuyant en priorité sur le Training Readiness s'il existe, sinon Body Battery + sommeil + FC repos. 1-2 phrases.
2. SÉANCE DU JOUR: ''' + ('routine bien-être détaillée' if est_weekend else 'le bloc principal adapté à la DISPO indiquée, avec paramètres précis (durée, allure en min/km RÉALISTE, FC cible)') + '''.
''' + ('' if est_weekend else '3. Si récup BON/EXCELLENT et dispo le permet: tu peux proposer 1 bloc cardio + 1 bloc muscu (haut du corps = tractions/abdos/pompes, ou bas = squats/fentes/MOLLETS pour prévention).') + '''
''' + ('' if est_weekend else '4. RAPPEL PÉRIOSTITE: si une activité récente montre une grosse charge de course, privilégie vélo/repos. Toute douleur tibia = stop course.') + '''
ÉTIREMENTS: ''' + ('routine étirements COMPLÈTE tout le corps (jour off) + mobilité' if est_weekend else 'termine TOUJOURS par un bloc étirements modulé selon la séance (socle tout le corps + focus muscles sollicités, mollets/tibial prioritaires). Précise avant=dynamique / après=statique.') + '''
''' + ('Un mot encourageant bref.' if est_weekend else '5. ALERTE si: FC repos > 68, Readiness < 35, ou gap d_activité > 6 jours.')}

Allures réalistes seulement: le Z2 facile de Mathurin est ~7:00-7:30/km piloté FC (à 6:40/km il est déjà en Z3). Ne JAMAIS prescrire plus rapide que 7:00/km en Z2, ni un 4:30/km. Pas de markdown lourd."""
    return prompt, vma, vma_status, est_weekend, jour_sem


def get_recommendation(data, profil, hist, mode="brief", dispo=None):
    if not ANTHROPIC_KEY:
        print("⚠️  Clé Anthropic absente — pas de reco générée")
        return None

    prompt, vma, vma_status, est_weekend, jour_sem = build_prompt(data, profil, hist, mode, dispo)

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
    print(f"🤖 RECOMMANDATION COACH IA (mode={mode}, dispo={dispo})")
    print("=" * 54)
    print(reco)
    print("=" * 54)

    s   = data.get("sleep", {})
    rd  = data.get("readiness", "N/A")
    rd_str = f"{rd['score']}/100" if isinstance(rd, dict) else "N/A"
    phys = profil.get("physiologie", {})

    # En mode BRIEF (jour de semaine), on affiche le bilan + boutons dispo.
    # En mode SÉANCE ou weekend, on envoie directement la séance/routine, sans boutons.
    if mode == "brief" and not est_weekend:
        msg = (
            f"🏃 *Bilan {date.today().strftime('%d/%m/%Y')}* — {jour_sem}\n\n"
            f"🌙 Sommeil : {s.get('score','N/A')}/100 ({s.get('duration_hours','N/A')}h)\n"
            f"❤️ FC repos : {s.get('resting_hr','N/A')} bpm\n"
            f"⚡ Body Battery : {data.get('body_battery','N/A')}/100\n"
            f"🎯 Readiness : {rd_str}\n"
            f"📈 VO2max : {data.get('vo2max', phys.get('vo2max_actuel','?'))} | VMA : {vma} km/h ({vma_status})\n\n"
            f"━━━━━━━━━━━━━━━\n\n{reco}\n\n"
            f"👉 *Tu as combien de temps aujourd'hui ?*"
        )
        send_telegram(msg, reply_markup=DISPO_KEYBOARD)
    else:
        titre = "Séance du jour" if not est_weekend else "Routine bien-être"
        msg = (
            f"🏃 *{titre} {date.today().strftime('%d/%m/%Y')}* — {jour_sem}\n\n{reco}"
        )
        send_telegram(msg)
    return reco


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # MODE et DISPO peuvent être passés par le listener Telegram via variables d'env.
    # mode='brief' (défaut, message matin) ou 'seance' (après réponse dispo).
    # ⚠️ `or` et non `.get(..., "brief")` : sur un déclenchement CRON, GitHub injecte
    # AGENT_MODE="" (chaîne vide présente), et le défaut d'un .get ne s'applique PAS.
    # Sans ce `or`, le brief automatique du matin partait en mode SÉANCE imposée.
    MODE  = os.environ.get("AGENT_MODE") or "brief"
    DISPO = os.environ.get("AGENT_DISPO") or None

    print(f"🏃 Garmin Health Agent v3.2  (mode={MODE}, dispo={DISPO})\n")

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
    get_recommendation(data, profil, hist, mode=MODE, dispo=DISPO)
