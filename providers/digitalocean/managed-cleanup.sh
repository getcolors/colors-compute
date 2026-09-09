#!/usr/bin/env bash
# Library-owned verification of Kubernetes-created provider resources.
set -euo pipefail
log() { echo "managed-compute-cleanup: $*" >&2; }
[[ -n ${COLORS_PAR_DO_TOKEN:-} ]] || { log "compute credential missing"; exit 1; }
if [[ ${1:-} == check-credentials ]]; then exit 0; fi
snapshot=${1:?cleanup snapshot path required}
[[ -f $snapshot && ! -L $snapshot ]] || { log "cleanup snapshot unavailable"; exit 1; }
jq -e 'type == "object" and (.volumes | type) == "array" and all(.volumes[]; type == "string") and (.lb_ip | type) == "string"' "$snapshot" >/dev/null
volume_ids=$(jq -r '.volumes[]' "$snapshot")
lb_ip=$(jq -r '.lb_ip' "$snapshot")
# Confirm provider cleanup before the control plane can be destroyed.
# An API failure is NOT absence: only a successful listing that lacks the
# resource counts as gone; anything else is surfaced as unverified.
do_api() { printf '%s\n' "Authorization: Bearer ${COLORS_PAR_DO_TOKEN:-}" | curl --connect-timeout 10 --max-time 30 -fsS -H @- "https://api.digitalocean.com/v2$1"; }
if [[ -n ${COLORS_PAR_DO_TOKEN:-} ]]; then
  if [[ -n ${volume_ids:-} ]]; then
    verdict="unverified"
    for _ in $(seq 1 30); do
      if live=$(do_api "/volumes?per_page=200" 2>/dev/null | jq -er 'if (.volumes | type) == "array" and ((.links.pages.next // .meta.links.next // "") == "") then [.volumes[].id] | join("\n") else error("incomplete listing") end' 2>/dev/null); then
        leftover=$(comm -12 <(sort <<<"$volume_ids") <(sort <<<"$live") | grep . || true)
        if [[ -z $leftover ]]; then verdict="absent"; break; else verdict="present"; fi
      fi
      sleep 10
    done
    case $verdict in
      absent) log "block volumes confirmed absent at the provider" ;;
      present) log "FATAL: block volumes still in the account: $leftover — delete them manually"; exit 1 ;;
      *) log "FATAL: could not verify volume deletion against the DigitalOcean API — check manually"; exit 1 ;;
    esac
  fi
  if [[ -n ${lb_ip:-} ]]; then
    verdict="unverified"
    for _ in $(seq 1 30); do
      if lbs=$(do_api "/load_balancers?per_page=200" 2>/dev/null); then
        jq -e '(.load_balancers | type) == "array" and ((.links.pages.next // .meta.links.next // "") == "")' <<<"$lbs" >/dev/null || { log "invalid or incomplete provider listing"; exit 1; }
        if jq -e --arg ip "$lb_ip" '.load_balancers[] | select(.ip==$ip)' <<<"$lbs" >/dev/null 2>&1
        then verdict="present"
        else verdict="absent"; break
        fi
      fi
      sleep 10
    done
    case $verdict in
      absent) log "load balancer confirmed absent at the provider" ;;
      present) log "FATAL: the load balancer at $lb_ip is still in the account — delete it manually"; exit 1 ;;
      *) log "FATAL: could not verify load-balancer deletion against the DigitalOcean API — check manually"; exit 1 ;;
    esac
  fi

fi

