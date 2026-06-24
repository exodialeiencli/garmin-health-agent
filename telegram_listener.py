"""
telegram_listener.py — Garmin Health Agent v3.2
================================================
Écoute les messages Telegram entrants (polling getUpdates avec offset) et
orchestre le pilotage de l'agent :

  /go              → déclenche le workflow GitHub (morning_brief) en mode BRIEF
  boutons 🟢🟡🔴   → enregistre la dispo du jour + déclenche le mode SÉANCE
  /seance          → guide Mathurin sur le format de feedback
  /aide            → rappelle les commandes
  texte libre      → enregistré comme FEEDBACK du jour dans historique.json

Conçu pour tourner en POLLING via GitHub Actions (toutes les ~5 min).
L'offset du dernier update traité est persisté dans telegram_state.json
(commité par le workflow) pour ne jamais retraiter ni rater un message.

SECRETS (GitHub Actions) :
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  → bot Telegram
  GH_DISPATCH_TOKEN                  → fine-grained PAT (Actions: write) sur le repo
  GH_REPO                            → "owner/repo" ex: exodialeiencli/garmin-health-agent

Le déclenchement réel de l'agent passe par l'API GitHub workflow_dispatch :
on relance morning_brief.yml en lui passant mode + dispo via les inputs.
"""

import json, os, time, requests
from datetime import date

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN         = os.environ.get("GH_DISPATCH_TOKEN", "")
GH_REPO          = os.environ.get("GH_REPO", "")            # "owner/repo"
WORKFLOW_FILE    = "morning_brief.yml"
BRANCH           = "main"

STATE_FILE       = "telegram_state.json"
HISTORIQUE_FILE  = "historique.json"

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ─── ÉTAT (offset persistant) ──────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"last_update_id": 0}


def save_state(state):
    try:
        json.dump(state, open(STATE_FILE, "w"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️  Sauvegarde state: {e}")


# ─── HISTORIQUE (feedback + dispo) ─────────────────────────────────────────────
def load_historique():
    if os.path.exists(HISTORIQUE_FILE):
        try:
            return json.load(open(HISTORIQUE_FILE))
        except Exception:
            pass
    return {"entrees": [], "stats_30j": {}, "derniere_maj": None}


def save_historique(hist):
    try:
        json.dump(hist, open(HISTORIQUE_FILE, "w"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️  Sauvegarde historique: {e}")


def get_or_create_today(hist):
    """Retourne l'entrée du jour, en la créant si besoin."""
    today = date.today().isoformat()
    entree = next((e for e in hist["entrees"] if e.get("date") == today), None)
    if entree is None:
        entree = {"date": today, "feedbacks": [], "dispo": None}
        hist["entrees"].append(entree)
    entree.setdefault("feedbacks", [])
    return entree


def enregistrer_feedback(texte):
    hist = load_historique()
    entree = get_or_create_today(hist)
    entree["feedbacks"].append(texte)
    save_historique(hist)
    print(f"📝 Feedback enregistré: {texte[:60]}")


def enregistrer_dispo(dispo):
    hist = load_historique()
    entree = get_or_create_today(hist)
    entree["dispo"] = dispo
    save_historique(hist)
    print(f"📝 Dispo enregistrée: {dispo}")


# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def envoyer(texte, reply_markup=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texte, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=15)
        if r.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(f"{API}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print(f"⚠️  envoyer: {e}")


def repondre_callback(callback_id, texte=""):
    """Acquitte un clic sur bouton inline (enlève le 'chargement')."""
    try:
        requests.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": texte},
                      timeout=15)
    except Exception as e:
        print(f"⚠️  answerCallback: {e}")


def get_updates(offset):
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": offset, "timeout": 0},
                         timeout=20)
        return r.json().get("result", []) if r.status_code == 200 else []
    except Exception as e:
        print(f"⚠️  getUpdates: {e}")
        return []


# ─── DÉCLENCHEMENT GITHUB (workflow_dispatch) ──────────────────────────────────
def declencher_agent(mode="brief", dispo=None):
    """Relance le workflow morning_brief via l'API GitHub, en passant mode + dispo."""
    if not GH_TOKEN or not GH_REPO:
        print("⚠️  GH_DISPATCH_TOKEN ou GH_REPO manquant — impossible de déclencher")
        envoyer("⚠️ Déclenchement impossible (config GitHub manquante côté serveur).")
        return False
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    inputs = {"mode": mode}
    if dispo:
        inputs["dispo"] = dispo
    try:
        r = requests.post(url,
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": BRANCH, "inputs": inputs},
            timeout=15)
        if r.status_code == 204:
            print(f"✅ Workflow déclenché (mode={mode}, dispo={dispo})")
            return True
        print(f"❌ Dispatch échoué {r.status_code}: {r.text[:200]}")
        envoyer(f"⚠️ Déclenchement refusé par GitHub ({r.status_code}).")
        return False
    except Exception as e:
        print(f"❌ Dispatch exception: {e}")
        return False


# ─── TRAITEMENT DES MESSAGES ───────────────────────────────────────────────────
AIDE = (
    "🤖 *Commandes du coach*\n\n"
    "`/go` — lance le bilan santé du matin\n"
    "🟢🟡🔴 — réponds à la dispo pour recevoir ta séance\n"
    "`/seance` — loguer un retour sur ta séance\n"
    "`/aide` — ce message\n\n"
    "Tu peux aussi m'écrire librement après une séance (ressenti, douleurs, "
    "exécution) : je l'enregistre pour adapter tes prochaines recos."
)

GUIDE_SEANCE = (
    "📝 *Logue ta séance* — écris-moi un message libre, par exemple :\n\n"
    "_« course 40min, z2 tenu mais marché 3x pour rester sous 150, "
    "tibias ok, jambes un peu lourdes, ressenti 6/10 »_\n\n"
    "Mets ce qui compte : allure tenue ou non, douleurs (surtout tibias !), "
    "ressenti, et si tu as alterné course/marche. Je m'en sers pour la suite."
)


def traiter_message(msg):
    texte = (msg.get("text") or "").strip()
    if not texte:
        return
    bas = texte.lower()

    if bas in ("/go", "/start", "go"):
        envoyer("🚀 Je lance ton bilan du matin, deux minutes...")
        declencher_agent(mode="brief")

    elif bas in ("/aide", "/help", "aide"):
        envoyer(AIDE)

    elif bas in ("/seance", "/séance", "seance"):
        envoyer(GUIDE_SEANCE)

    else:
        # Texte libre → feedback du jour
        enregistrer_feedback(texte)
        envoyer("✅ Noté, j'intègre ça à tes prochaines recommandations. 💪")


def traiter_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    if data.startswith("dispo:"):
        dispo = data.split(":", 1)[1]
        labels = {"beaucoup": "🟢 Beaucoup de temps",
                  "court": "🟡 Un peu de temps",
                  "rien": "🔴 Pas le temps"}
        repondre_callback(cb_id, "Dispo enregistrée !")
        enregistrer_dispo(dispo)
        if dispo == "rien":
            envoyer(f"{labels.get(dispo)} — ok, je te prépare une routine bien-être courte.")
        else:
            envoyer(f"{labels.get(dispo)} — je te génère la séance adaptée, deux minutes...")
        # Déclenche l'agent en mode SÉANCE avec la dispo
        declencher_agent(mode="seance", dispo=dispo)
    else:
        repondre_callback(cb_id)


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Secrets Telegram manquants")
        return

    state  = load_state()
    offset = state.get("last_update_id", 0) + 1
    updates = get_updates(offset)
    print(f"📨 {len(updates)} update(s) à traiter (offset={offset})")

    max_id = state.get("last_update_id", 0)
    for up in updates:
        up_id = up.get("update_id", 0)
        max_id = max(max_id, up_id)

        # Sécurité : on n'écoute QUE le chat autorisé (anti-spam/intrusion)
        chat_id = None
        if "message" in up:
            chat_id = str(up["message"].get("chat", {}).get("id", ""))
        elif "callback_query" in up:
            chat_id = str(up["callback_query"].get("message", {}).get("chat", {}).get("id", ""))
        if chat_id and TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            print(f"⛔ Message ignoré (chat {chat_id} non autorisé)")
            continue

        try:
            if "message" in up:
                traiter_message(up["message"])
            elif "callback_query" in up:
                traiter_callback(up["callback_query"])
        except Exception as e:
            print(f"⚠️  Traitement update {up_id}: {e}")

    state["last_update_id"] = max_id
    save_state(state)
    print(f"✅ Offset mis à jour: {max_id}")


if __name__ == "__main__":
    main()
