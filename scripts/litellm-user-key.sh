#!/usr/bin/env bash
# Create (or report on) a per-user LiteLLM virtual key for the in-cluster
# LiteLLM proxy (app `litellm`). The key is model-restricted to
# deepseek-v4-flash-v2 and gets a monthly budget.
#
# Usage: scripts/litellm-user-key.sh <username> [max_budget_usd]
#
# The token is printed exactly once (at creation). LiteLLM stores only a
# hash; a lost key must be rotated from the LiteLLM UI
# (kubectl -n litellm port-forward svc/litellm 4000 -> /ui) or by deleting
# the key there and re-running this script.
set -euo pipefail

user="${1:-}"
budget="${2:-5}"
[ -n "$user" ] || { echo "usage: $0 <username> [max_budget_usd]" >&2; exit 1; }
case "$user" in
  *[!a-zA-Z0-9._-]*) echo "username may only contain [a-zA-Z0-9._-]" >&2; exit 1 ;;
esac

NS=litellm
# Local-only port: the node's own :4000 belongs to the host-level LiteLLM.
PORT=14000

master="$(kubectl -n "$NS" get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)"

kubectl -n "$NS" port-forward svc/litellm "$PORT:4000" >/dev/null 2>&1 &
pf=$!
trap 'kill "$pf" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  if curl -sf --max-time 2 "http://127.0.0.1:$PORT/health/liveliness" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

name="chat-$user"

# /key/list entries are token hashes only; /spend/keys (master) returns
# full key objects including key_name and user_id.
existing="$(curl -s -H "Authorization: Bearer $master" "http://127.0.0.1:$PORT/spend/keys" | python3 -c "
import json, sys
name, user = '$name', '$user'
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
keys = data if isinstance(data, list) else data.get('keys', [])
for k in keys:
    if not isinstance(k, dict):
        continue
    if k.get('key_name') == name or k.get('user_id') == user:
        print('%s / max_budget=%s' % (k.get('key_name'), k.get('max_budget')))
        break
" || true)"

if [ -n "$existing" ]; then
  echo "A key for user '$user' already exists: $existing"
  echo "The token was printed only at creation time. Rotate it via the LiteLLM"
  echo "UI (kubectl -n litellm port-forward svc/litellm 4000 -> /ui), or delete"
  echo "the key there and re-run this script."
  exit 0
fi

resp="$(curl -s -X POST -H "Authorization: Bearer $master" -H 'Content-Type: application/json' \
  "http://127.0.0.1:$PORT/key/generate" \
  -d "{\"key_name\": \"$name\", \"user_id\": \"$user\", \"models\": [\"deepseek-v4-flash-v2\"], \"max_budget\": $budget, \"budget_duration\": \"monthly\"}")"

key="$(printf '%s' "$resp" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("key", ""))
except Exception:
    print("")')"

if [ -z "$key" ]; then
  echo "key/generate failed:" >&2
  printf '%s\n' "$resp" >&2
  exit 1
fi

echo "Virtual key for user '$user' (model: deepseek-v4-flash-v2, budget: \$$budget/month):"
echo
echo "$key"
echo
echo "Shown once — hand it to the user now (LibreChat -> Settings -> API Keys"
echo "-> LiteLLM endpoint). Spend is visible in the LiteLLM UI per key/user."
