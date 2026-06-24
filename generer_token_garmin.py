"""
generer_token_garmin.py — À LANCER UNE SEULE FOIS EN LOCAL
===========================================================
Génère le token Garmin et l'encode en base64 pour GitHub Actions.
Évite le rate-limit (erreur 429) causé par les logins répétés.

USAGE :
  pip install garminconnect
  python generer_token_garmin.py

Puis copie la longue chaîne affichée dans un secret GitHub nommé
GARMIN_TOKEN_BASE64 (Settings > Secrets and variables > Actions > New secret).

⚠️ Le token expire après quelques mois : relancer ce script si l'agent
   signale une erreur d'authentification.
"""
import os, base64, tarfile, io
from getpass import getpass
from garminconnect import Garmin

TOKENSTORE = os.path.expanduser("~/.garminconnect")

email = input("Email Garmin : ").strip()
password = getpass("Mot de passe Garmin : ")

# MFA géré interactivement si le compte en a un
def mfa():
    return input("Code MFA (vérification en 2 étapes) : ").strip()

print("\nConnexion à Garmin...")
client = Garmin(email=email, password=password, prompt_mfa=mfa)
client.login(TOKENSTORE)
print(f"✅ Connecté. Token enregistré dans {TOKENSTORE}")

# Archive le dossier token en tar.gz puis base64
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    for fname in os.listdir(TOKENSTORE):
        tar.add(os.path.join(TOKENSTORE, fname), arcname=fname)
token_b64 = base64.b64encode(buf.getvalue()).decode()

print("\n" + "="*60)
print("COPIE TOUT CE QUI SUIT dans le secret GitHub GARMIN_TOKEN_BASE64 :")
print("="*60)
print(token_b64)
print("="*60)
print(f"\nLongueur : {len(token_b64)} caractères")
