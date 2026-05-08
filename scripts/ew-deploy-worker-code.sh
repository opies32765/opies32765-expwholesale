#!/bin/bash
# EW worker code deploy script.
#
# Pushes the 4 worker .py files (process_bid, worker_vauto, worker_accutrade,
# worker_ipacket) to every surviving worker VM via Proxmox QGA, decodes them
# in place, and restarts NSSM. Then clears any 'degraded' priority flags so
# the workers re-join the dispatch pool.
#
# Source files: /opt/expwholesale/worker_code/<name>.py (committed in git)
# Run as: sudo bash ew-deploy-worker-code.sh
#
# Customize the VM_LIST below if you change the surviving fleet.

set -u

# --- CONFIG ---------------------------------------------------------------
TOK='PVEAPIToken=root@pam!ewdashboard=678699e1-4b97-4c0c-9ba1-2e9563b2de2b'
PROXY='https://pve.experience-wholesale.net'
SRC_DIR='/opt/expwholesale/worker_code'

# Surviving fleet after the 2-VM trim — edit if you change the layout.
# Format: vmid:node:worker_id
VM_LIST=(
  '9000:pve:vm-worker-1'
  '100:pve:vm-worker-2'
  '102:pve:vm-worker-4'
  '103:pve:vm-worker-5'
  '116:pve115:vm-worker-6'
  '111:pve115:vm-worker-7'
  '112:pve115:vm-worker-8'
  '115:pve115:vm-worker-10'
)

FILES=( process_bid.py worker_vauto.py worker_accutrade.py worker_ipacket.py worker_main.py )

DB_HOST='localhost'
DB_PORT='5433'
DB_USER='expuser'
DB_PASS='ExpWholesale2026!'
DB_NAME='expwholesale'

# --- HELPERS --------------------------------------------------------------
qga_exec() {
  # qga_exec <node> <vmid> <command_arg1> <command_arg2> ...
  # Returns the exec PID.
  local node="$1"; local vmid="$2"; shift 2
  local args=()
  for a in "$@"; do args+=( --data-urlencode "command=$a" ); done
  curl -sk --max-time 25 -X POST -H "Authorization: $TOK" "${args[@]}" \
    "$PROXY/api2/json/nodes/$node/qemu/$vmid/agent/exec" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["pid"])' 2>/dev/null
}

qga_status() {
  # qga_status <node> <vmid> <pid>  -> prints "ec=<exitcode>"
  local node="$1"; local vmid="$2"; local pid="$3"
  curl -sk --max-time 8 -X GET -H "Authorization: $TOK" \
    "$PROXY/api2/json/nodes/$node/qemu/$vmid/agent/exec-status?pid=$pid" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin)["data"];print("ec="+str(d.get("exitcode","?")))' 2>/dev/null
}

push_file() {
  # push_file <node> <vmid> <local_path> <remote_b64_filename>
  local node="$1"; local vmid="$2"; local local_path="$3"; local remote_b64="$4"
  local b64; b64=$(base64 -w0 "$local_path")
  curl -sk --max-time 30 -X POST -H "Authorization: $TOK" \
    --data-urlencode "file=C:\\worker\\$remote_b64" \
    --data-urlencode "content=$b64" \
    "$PROXY/api2/json/nodes/$node/qemu/$vmid/agent/file-write" >/dev/null
}

# --- PRE-FLIGHT -----------------------------------------------------------
echo "==== EW Worker Code Deploy ===="
echo
for f in "${FILES[@]}"; do
  if [[ ! -r "$SRC_DIR/$f" ]]; then
    echo "[!] missing source: $SRC_DIR/$f"
    exit 1
  fi
done
echo "Source files OK ($SRC_DIR):"
for f in "${FILES[@]}"; do
  printf "  %-26s %s bytes\n" "$f" "$(stat -c%s "$SRC_DIR/$f")"
done
echo
echo "Target fleet (${#VM_LIST[@]} VMs):"
for entry in "${VM_LIST[@]}"; do echo "  $entry"; done
echo

# --- MAIN: PER-VM LOOP ----------------------------------------------------
FAILED=()
for entry in "${VM_LIST[@]}"; do
  vmid=${entry%%:*}
  rest=${entry#*:}
  node=${rest%%:*}
  wid=${rest#*:}
  printf "\n=== %s (vmid=%s on %s) ===\n" "$wid" "$vmid" "$node"

  # 1. Push all 4 .b64 files
  for f in "${FILES[@]}"; do
    printf "  push %-26s ... " "$f"
    if push_file "$node" "$vmid" "$SRC_DIR/$f" "${f}.b64"; then
      echo "ok"
    else
      echo "FAIL"
      FAILED+=( "$wid:push-$f" )
    fi
  done

  # 2. Decode all 4 (one combined cmd) + restart NSSM
  printf "  decode + restart NSSM ... "
  pid=$(qga_exec "$node" "$vmid" \
    'cmd.exe' '/c' \
    'del /Q C:\worker\process_bid.py 2>nul & certutil -decode C:\worker\process_bid.py.b64 C:\worker\process_bid.py & del /Q C:\worker\process_bid.py.b64 & del /Q C:\worker\worker_vauto.py 2>nul & certutil -decode C:\worker\worker_vauto.py.b64 C:\worker\worker_vauto.py & del /Q C:\worker\worker_vauto.py.b64 & del /Q C:\worker\worker_accutrade.py 2>nul & certutil -decode C:\worker\worker_accutrade.py.b64 C:\worker\worker_accutrade.py & del /Q C:\worker\worker_accutrade.py.b64 & del /Q C:\worker\worker_ipacket.py 2>nul & certutil -decode C:\worker\worker_ipacket.py.b64 C:\worker\worker_ipacket.py & del /Q C:\worker\worker_ipacket.py.b64 & del /Q C:\worker\worker_main.py 2>nul & certutil -decode C:\worker\worker_main.py.b64 C:\worker\worker_main.py & del /Q C:\worker\worker_main.py.b64 & C:\Tools\nssm.exe restart EWWorker')
  sleep 6
  ec=$(qga_status "$node" "$vmid" "$pid")
  echo "$ec"
  if [[ "$ec" != "ec=0" ]]; then
    FAILED+=( "$wid:decode-restart" )
  fi
done

# --- DB: CLEAR DEGRADED + UN-PAUSE -----------------------------------------
echo
echo "==== Clearing degraded flags + pause flags ===="
PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" <<SQL
UPDATE workers
   SET effective_priority='primary',
       paused=FALSE,
       pause_reason=NULL,
       auto_demoted_at=NULL,
       synthetic_ok_count=0
 WHERE worker_id LIKE 'vm-worker-%';
SELECT worker_id, effective_priority, paused,
       EXTRACT(EPOCH FROM (NOW()-last_heartbeat))::int AS hb_age
  FROM workers
 WHERE worker_id LIKE 'vm-worker-%'
 ORDER BY CAST(REGEXP_REPLACE(worker_id,'[^0-9]','','g') AS INT);
SQL

# --- SUMMARY --------------------------------------------------------------
echo
if (( ${#FAILED[@]} == 0 )); then
  echo "==== ALL ${#VM_LIST[@]} VMs DEPLOYED OK ===="
else
  echo "==== ${#FAILED[@]} FAILURES ===="
  for f in "${FAILED[@]}"; do echo "  - $f"; done
  exit 1
fi
