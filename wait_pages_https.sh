#!/usr/bin/env bash
# Sonde l'état du certificat GitHub Pages toutes les 10 min.
# Dès que le certif est "approved", active https_enforced=true, vérifie, et sort.
#
# Usage : ./wait_pages_https.sh [repo] [domaine]
#   defaults : cmehat/dailycmd  blog.oyatrino.com

set -euo pipefail

REPO="${1:-cmehat/dailycmd}"
DOMAIN="${2:-blog.oyatrino.com}"
INTERVAL="${INTERVAL:-600}"   # secondes entre deux checks (défaut 10 min)
MAX_TRIES="${MAX_TRIES:-18}"  # 18 x 10 min = 3 h de patience max

ts() { date '+%H:%M:%S'; }

echo "[$(ts)] Surveillance de $DOMAIN sur $REPO — check toutes $((INTERVAL/60)) min (max $MAX_TRIES essais)."

for ((i=1; i<=MAX_TRIES; i++)); do
  # état courant ; on tolère une erreur transitoire de l'API sans planter
  state="$(gh api "repos/$REPO/pages" --jq '.https_certificate.state // "null"' 2>/dev/null || echo "api_error")"
  status="$(gh api "repos/$REPO/pages" --jq '.status // "unknown"' 2>/dev/null || echo "unknown")"

  echo "[$(ts)] essai $i/$MAX_TRIES — status=$status cert=$state"

  case "$state" in
    approved)
      echo "[$(ts)] Certificat approuvé. Activation du HTTPS forcé…"
      gh api -X PUT "repos/$REPO/pages" -f cname="$DOMAIN" -F https_enforced=true >/dev/null
      echo "[$(ts)] https_enforced=true posé. Vérification finale :"
      # laisse une poignée de secondes puis teste le HTTPS réel
      sleep 5
      code="$(curl -sI "https://$DOMAIN" -o /dev/null -w '%{http_code}' || echo '000')"
      echo "[$(ts)] curl https://$DOMAIN -> HTTP $code"
      if [[ "$code" == "200" || "$code" == "301" || "$code" == "302" ]]; then
        echo "[$(ts)] ✅ En ligne en HTTPS. Terminé."
      else
        echo "[$(ts)] ⚠️  HTTPS forcé activé mais le curl renvoie $code — laisse 1-2 min et re-teste : curl -sI https://$DOMAIN"
      fi
      exit 0
      ;;
    errored)
      echo "[$(ts)] ⚠️  cert.state=errored — GitHub a échoué à émettre le certif."
      echo "        Va dans Settings → Pages : efface le domaine, Save, re-tape $DOMAIN, Save (relance la validation)."
      exit 1
      ;;
  esac

  if (( i < MAX_TRIES )); then
    sleep "$INTERVAL"
  fi
done

echo "[$(ts)] ⏳ Toujours pas 'approved' après $MAX_TRIES essais. Le certif Let's Encrypt peut être long au 1er coup."
echo "         Relance simplement le script, ou sonde à la main : gh api repos/$REPO/pages --jq '{status,cert:.https_certificate.state}'"
exit 2
