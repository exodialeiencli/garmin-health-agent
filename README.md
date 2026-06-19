# 🏃 Garmin Health Agent v3.0 — Coaching ESM Saint-Cyr

Agent quotidien qui récupère les données Garmin, les croise avec un profil
athlète calibré sur 5 ans de données, et envoie une recommandation
d'entraînement sur Telegram chaque matin (lundi-vendredi).

## Architecture

```
Garmin Connect API → garmin_fetch.py → Claude (Haiku) → Telegram
                          ↓ ↑
              profil_athlete.json + historique.json (mémoire persistante)
                          ↑
                  GitHub Actions (cron 10h30 Shanghai)
```

## Fichiers

| Fichier | Rôle |
|---------|------|
| `garmin_fetch.py` | Script principal (récup données + reco + Telegram) |
| `profil_athlete.json` | Baseline 5 ans + zones + plan 24 mois + science |
| `historique.json` | Mémoire persistante (auto-générée, commitée par le workflow) |
| `.github/workflows/morning_brief.yml` | Automatisation quotidienne |
| `METHODO_SCIENTIFIQUE.docx` | Document de référence scientifique |
| `dashboard.html` | Visualisation locale (optionnel) |

## Secrets GitHub Actions à configurer

`GARMIN_EMAIL`, `GARMIN_PASSWORD`, `ANTHROPIC_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`

## Nouveautés v3.0

- **Appels API Garmin vérifiés** : Training Readiness + VO2max + Body Battery corrigé (lecture de la liste par jour)
- **Modèle scientifique** : polarisé 80/20 (Seiler), prévention périostite (MTSS), anti-désentraînement (Mujika)
- **Mémoire persistante** : historique 120 jours + stats 30 jours glissants, commit auto
- **Logique weekend** : bascule bien-être automatique (samedi-dimanche)
- **Cardio + muscu le même jour** si récupération bonne
- **Filtre activités < 8 min** (ignore les tractions isolées)
- **Cron corrigé** : `30 2 * * 1-5` = 10h30 Shanghai, lundi-vendredi
- **Mode dégradé** : ne crashe pas si Garmin/Claude indisponible

## Horaires (cron UTC)

| Lieu | Heure locale voulue | Cron |
|------|---------------------|------|
| Shanghai (UTC+8) | 10h30 | `30 2 * * 1-5` |
| France été (UTC+2) | 10h30 | `30 8 * * 1-5` |

## Calibration

Après le test VMA (tapis, +0.5 km/h/min depuis 8 km/h, pente 1%), mettre à jour
`vma_testee_kmh` et `vma_test_date` dans `profil_athlete.json`. Toutes les allures
se recalculent automatiquement.

