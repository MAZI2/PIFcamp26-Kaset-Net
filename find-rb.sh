#!/usr/bin/env bash
set -Eeuo pipefail

# Find a likely Raspberry Pi on the local IPv4 network, SSH into it,
# and save a non-destructive diagnostic report locally.
#
# Usage:
#   ./find-rb.sh [-u USER] [-i SSH_KEY] [-H HOST_OR_IP] [-I INTERFACE] [-o REPORT]
#
# Examples:
#   ./find-rb.sh -u jakob
#   ./find-rb.sh -u pi -i ~/.ssh/id_ed25519
#   ./find-rb.sh -u pi -H raspberrypi.local

USER_NAME="${RB_USER:-}"
IDENTITY_FILE=""
TARGET_HOST=""
INTERFACE=""
REPORT_FILE=""

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

while getopts ":u:i:H:I:o:h" opt; do
  case "$opt" in
    u) USER_NAME="$OPTARG" ;;
    i) IDENTITY_FILE="$OPTARG" ;;
    H) TARGET_HOST="$OPTARG" ;;
    I) INTERFACE="$OPTARG" ;;
    o) REPORT_FILE="$OPTARG" ;;
    h) usage; exit 0 ;;
    :) echo "Missing argument for -$OPTARG" >&2; exit 2 ;;
    \?) echo "Unknown option: -$OPTARG" >&2; usage; exit 2 ;;
  esac
done

for cmd in ip ssh awk sed grep sort timeout getent tee; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Required command not found: $cmd" >&2
    exit 1
  }
done

if [[ -z "$USER_NAME" ]]; then
  read -r -p "SSH username on the Raspberry Pi: " USER_NAME
fi
[[ -n "$USER_NAME" ]] || { echo "SSH username cannot be empty." >&2; exit 2; }

if [[ -n "$IDENTITY_FILE" && ! -r "$IDENTITY_FILE" ]]; then
  echo "SSH key is not readable: $IDENTITY_FILE" >&2
  exit 2
fi

SSH_OPTS=(
  -o ConnectTimeout=6
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
  -o StrictHostKeyChecking=accept-new
)
[[ -n "$IDENTITY_FILE" ]] && SSH_OPTS+=( -i "$IDENTITY_FILE" )

resolve_ipv4() {
  local host="$1"
  getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1 {print $1}'
}

port_22_open() {
  local ip="$1"
  timeout 1 bash -c "</dev/tcp/$ip/22" >/dev/null 2>&1
}

if [[ -z "$TARGET_HOST" ]]; then
  if [[ -z "$INTERFACE" ]]; then
    INTERFACE="$(ip -4 route show default 2>/dev/null | awk 'NR==1 {print $5}')"
  fi
  [[ -n "$INTERFACE" ]] || {
    echo "Could not determine the active network interface. Use -I INTERFACE." >&2
    exit 1
  }

  CIDR="$(ip -o -4 addr show dev "$INTERFACE" scope global | awk 'NR==1 {print $4}')"
  [[ -n "$CIDR" ]] || {
    echo "No global IPv4 address found on interface $INTERFACE." >&2
    exit 1
  }

  echo "Interface: $INTERFACE"
  echo "Local subnet: $CIDR"

  declare -A SCORE=()
  declare -A REASON=()
  declare -A HOSTNAME=()

  add_candidate() {
    local ip="$1" score="$2" reason="$3" hostname="${4:-}"
    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 0
    if (( score > ${SCORE[$ip]:-0} )); then
      SCORE[$ip]="$score"
    fi
    if [[ -n "${REASON[$ip]:-}" ]]; then
      REASON[$ip]+="; $reason"
    else
      REASON[$ip]="$reason"
    fi
    [[ -n "$hostname" ]] && HOSTNAME[$ip]="$hostname"
  }

  # Common mDNS hostname.
  if ip="$(resolve_ipv4 raspberrypi.local || true)"; [[ -n "$ip" ]]; then
    add_candidate "$ip" 120 "resolved raspberrypi.local" "raspberrypi.local"
  fi

  # Use nmap when available. It both discovers hosts and often identifies Raspberry Pi MAC vendors.
  if command -v nmap >/dev/null 2>&1; then
    echo "Scanning live hosts with nmap..."
    NMAP_OUT="$(mktemp)"
    trap 'rm -f "${NMAP_OUT:-}"' EXIT

    # sudo improves MAC/vendor detection on a local Ethernet/Wi-Fi segment, but is optional.
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      sudo nmap -sn "$CIDR" >"$NMAP_OUT"
    else
      nmap -sn "$CIDR" >"$NMAP_OUT"
    fi

    awk '
      /^Nmap scan report for / {
        line=$0
        sub(/^Nmap scan report for /, "", line)
        host=line
        ip=line
        if (line ~ /\([0-9.]+\)$/) {
          ip=line
          sub(/^.*\(/, "", ip)
          sub(/\)$/, "", ip)
          sub(/ \([0-9.]+\)$/, "", host)
        }
      }
      /^MAC Address:/ {
        vendor=$0
        sub(/^MAC Address: [^ ]+ /, "", vendor)
        gsub(/^\(|\)$/, "", vendor)
        print ip "\t" host "\t" vendor
      }
      /^Host is up/ && ip != "" {
        print ip "\t" host "\t"
      }
    ' "$NMAP_OUT" | while IFS=$'\t' read -r ip host vendor; do
      score=10
      reason="live host"
      shopt -s nocasematch
      if [[ "$host" =~ raspberry|raspberrypi|(^|[-_.])pi([-_.]|$) ]]; then
        score=100
        reason="Pi-like hostname: $host"
      fi
      if [[ "$vendor" =~ Raspberry[[:space:]]Pi ]]; then
        score=110
        reason="Raspberry Pi MAC vendor: $vendor"
      fi
      shopt -u nocasematch
      printf '%s\t%s\t%s\t%s\n' "$ip" "$score" "$reason" "$host"
    done >"${NMAP_OUT}.candidates"

    while IFS=$'\t' read -r ip score reason host; do
      add_candidate "$ip" "$score" "$reason" "$host"
    done <"${NMAP_OUT}.candidates"
    rm -f "${NMAP_OUT}.candidates"
  else
    echo "nmap is not installed; using neighbour discovery only."
    echo "For a more reliable scan: sudo apt install nmap"
    ip neigh show dev "$INTERFACE" | awk '$1 ~ /^[0-9]+\./ && $NF != "FAILED" {print $1}' | while read -r ip; do
      printf '%s\n' "$ip"
    done > /tmp/find-rb-neighbours.$$
    while read -r ip; do
      add_candidate "$ip" 10 "network neighbour"
    done < /tmp/find-rb-neighbours.$$
    rm -f /tmp/find-rb-neighbours.$$
  fi

  # Keep only hosts that currently expose SSH; add a small score bonus.
  declare -a OPEN_IPS=()
  for ip in "${!SCORE[@]}"; do
    if port_22_open "$ip"; then
      SCORE[$ip]=$(( SCORE[$ip] + 20 ))
      REASON[$ip]+="; SSH port 22 open"
      OPEN_IPS+=("$ip")
    fi
  done

  if (( ${#OPEN_IPS[@]} == 0 )); then
    echo "No discovered host has TCP port 22 open." >&2
    echo "Check that the Pi is powered on, on the same non-guest Wi-Fi, and has SSH enabled." >&2
    exit 1
  fi

  mapfile -t SORTED_IPS < <(
    for ip in "${OPEN_IPS[@]}"; do
      printf '%05d\t%s\n' "${SCORE[$ip]}" "$ip"
    done | sort -rn | awk -F '\t' '{print $2}'
  )

  echo
  echo "SSH candidates:"
  for idx in "${!SORTED_IPS[@]}"; do
    ip="${SORTED_IPS[$idx]}"
    printf '  [%d] %-15s score=%-3s %s\n' "$((idx + 1))" "$ip" "${SCORE[$ip]}" "${REASON[$ip]}"
  done

  if (( ${#SORTED_IPS[@]} == 1 )); then
    TARGET_HOST="${SORTED_IPS[0]}"
  else
    read -r -p "Choose host [1-${#SORTED_IPS[@]}] (default 1): " choice
    choice="${choice:-1}"
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#SORTED_IPS[@]} )) || {
      echo "Invalid selection." >&2
      exit 2
    }
    TARGET_HOST="${SORTED_IPS[$((choice - 1))]}"
  fi
fi

TARGET_IP="$(resolve_ipv4 "$TARGET_HOST" || true)"
TARGET_IP="${TARGET_IP:-$TARGET_HOST}"

if [[ -z "$REPORT_FILE" ]]; then
  safe_host="${TARGET_HOST//[^A-Za-z0-9_.-]/_}"
  REPORT_FILE="rb-diagnostics-${safe_host}-$(date +%Y%m%d-%H%M%S).txt"
fi

echo
echo "Connecting to ${USER_NAME}@${TARGET_HOST} ..."
echo "Diagnostic output will also be saved to: $REPORT_FILE"

ssh "${SSH_OPTS[@]}" "${USER_NAME}@${TARGET_HOST}" 'bash -s' 2>&1 <<'REMOTE_DIAGNOSTICS' | tee "$REPORT_FILE"
set +e

section() {
  printf '\n========== %s ==========\n' "$1"
}

run() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1
}

section "IDENTITY"
printf 'Collected: '; date --iso-8601=seconds 2>/dev/null || date
run hostnamectl
run uname -a
run cat /etc/os-release

section "UPTIME AND LOAD"
run uptime
run who -b
run free -h

section "RASPBERRY PI HARDWARE"
if command -v vcgencmd >/dev/null 2>&1; then
  run vcgencmd get_throttled
  run vcgencmd measure_temp
  run vcgencmd get_mem arm
  run vcgencmd get_mem gpu
else
  echo "vcgencmd not installed or not in PATH"
fi
if [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
  awk '{printf "CPU temperature: %.1f °C\n", $1/1000}' /sys/class/thermal/thermal_zone0/temp
fi
[[ -r /proc/device-tree/model ]] && { printf 'Model: '; tr -d '\0' </proc/device-tree/model; echo; }
[[ -r /proc/cpuinfo ]] && grep -E '^(Hardware|Revision|Serial|Model)' /proc/cpuinfo || true

section "NETWORK"
run ip -brief address
run ip route
run ip neigh
if command -v iw >/dev/null 2>&1; then
  for dev in $(iw dev 2>/dev/null | awk '$1=="Interface" {print $2}'); do
    run iw dev "$dev" link
  done
fi
if command -v nmcli >/dev/null 2>&1; then
  run nmcli -f DEVICE,TYPE,STATE,CONNECTION device status
fi
if command -v rfkill >/dev/null 2>&1; then
  run rfkill list
fi

section "STORAGE"
run df -hT
run lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS,ROTA,MODEL

section "FAILED SERVICES"
run systemctl --failed --no-pager --plain
run systemctl status ssh --no-pager --lines=20

section "LISTENING PORTS"
run ss -lntup

section "RECENT HIGH-PRIORITY LOGS"
if command -v journalctl >/dev/null 2>&1; then
  run journalctl -b -p warning..alert --no-pager -n 120
else
  echo "journalctl unavailable"
fi

section "KERNEL WARNINGS"
if command -v dmesg >/dev/null 2>&1; then
  dmesg --level=warn,err 2>&1 | tail -n 120
fi

section "END"
echo "Diagnostics complete. No Wi-Fi passwords, SSH keys, or configuration secrets were requested."
REMOTE_DIAGNOSTICS

status=${PIPESTATUS[0]}
if (( status != 0 )); then
  echo >&2
  echo "SSH or the remote diagnostic command failed with status $status." >&2
  echo "Partial output, if any, is in $REPORT_FILE." >&2
  exit "$status"
fi

echo
echo "Done. Review the report for anything private, then paste it here for analysis."
