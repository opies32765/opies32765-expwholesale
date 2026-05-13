#!/bin/bash
LOG=/var/log/ew-bouncer-killer.log
while true; do
    KILLED=$(pgrep -f "systemctl restart expwholesale|bash -c.*systemctl restart expwholesale")
    if [ -n "$KILLED" ]; then
        for pid in $KILLED; do
            chain=""
            curp=$pid
            for i in 1 2 3 4 5 6 7 8; do
                if [ -r "/proc/$curp/status" ]; then
                    name=$(grep "^Name:" /proc/$curp/status 2>/dev/null | awk '{print $2}')
                    cmd=$(tr '\0' ' ' < /proc/$curp/cmdline 2>/dev/null | head -c 250)
                    chain="$chain
  pid=$curp $name [$cmd]"
                    parent=$(grep "^PPid:" /proc/$curp/status 2>/dev/null | awk '{print $2}')
                    if [ -z "$parent" ] || [ "$parent" = "0" ] || [ "$parent" = "1" ]; then
                        chain="$chain
  -> reached PID $parent (top)"
                        break
                    fi
                    curp=$parent
                else
                    break
                fi
            done
            kill -9 $pid 2>/dev/null
            echo "[$(date -Iseconds)] killed pid $pid CHAIN:$chain" >> $LOG
        done
    fi
    sleep 0.1
done
