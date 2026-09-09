#!/usr/bin/env bash
# Verify the managed provider's client-source ingress policy.
set -euo pipefail
log() { echo "managed-compute-ingress: $*" >&2; }
LB_IP=${1:?load balancer IP required}
shift
[[ $# -gt 0 ]] || { log "client source ranges are required"; exit 1; }
log "asserting the load balancer firewall matches desired http-sources"
desired_sources=$(printf '%s\n' "$@" | jq -R . | jq -cs .)
lb_json=$(printf '%s\n' "Authorization: Bearer ${COLORS_PAR_DO_TOKEN:?}" | curl --connect-timeout 10 --max-time 30 -fsS -H @- \
  "https://api.digitalocean.com/v2/load_balancers?per_page=200" 2>/dev/null \
  | jq -c --arg ip "$LB_IP" '.load_balancers[] | select(.ip==$ip)' || true)
if [[ -z $lb_json ]]; then
  log "FAIL: no load balancer with address $LB_IP is visible through the DigitalOcean API"
  exit 1
fi
allow=$(jq -c '[.firewall.allow[]? | select(startswith("cidr:")) | ltrimstr("cidr:")] | sort' <<<"$lb_json")
want=$(jq -c 'sort' <<<"$desired_sources")
if [[ $want == '["0.0.0.0/0"]' ]]; then
  # Open desired state: an absent/empty firewall and an explicit 0.0.0.0/0
  # allow are both the open configuration.
  if [[ $allow != "[]" && $allow != '["0.0.0.0/0"]' ]]; then
    log "FAIL: desired http-sources is open but the LB firewall restricts to $allow"
    exit 1
  fi
else
  if [[ $allow != "$want" ]]; then
    log "FAIL: the LB firewall client-source allow set is $allow, desired $want"
    exit 1
  fi
fi

