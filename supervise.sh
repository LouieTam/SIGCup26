#!/bin/bash
# Keep the quoter running until told to stop.
#
#   start:  nohup caffeinate -ims ./supervise.sh >> data/supervisor.log 2>&1 &
#   stop:   touch data/STOP     (then the running quoter is told to finish its
#                               pass; the supervisor exits instead of relaunching)
#
# If the quoter exits for any other reason (crash, hard stop, API down at
# startup), it is relaunched after 30s, each time into a fresh data/quoterN.log.
# There is no --max-hours: it runs until data/STOP exists.
cd "$(dirname "$0")"
rm -f data/STOP
FLAGS="--live --race-pct 0.85 --budget-pct 135 --max-deploy-pct 1.0 --cash-floor-pct 0.02 \
  --shrink-tol 0.95 --min-fair 0.70 --mirror 0.5 --cheap 0 --unwind --unwind-interval 120 \
  --min-exit-edge 0.005 --reprice-ticks 2 --interval 180 --stop-hours 3 --stop-max-hours 8 \
  --stop-coherence 0.03 --log data/quoter.jsonl"
while [ ! -e data/STOP ]; do
  N=1; while [ -e "data/quoter$N.log" ]; do N=$((N+1)); done
  echo "$(date -u +%FT%TZ) starting quoter -> data/quoter$N.log"
  python3 -u run_quoter.py $FLAGS > "data/quoter$N.log" 2>&1 &
  QPID=$!
  QSTART=$(date +%s)
  # Forward a stop request to the quoter so it finishes its pass cleanly.
  while kill -0 $QPID 2>/dev/null; do
    if [ -e data/STOP ]; then kill -TERM $QPID; wait $QPID; break; fi
    # Watchdog: every pass (ok or failed) appends to data/quoter.jsonl. After the
    # 10-15 outage the main loop hung ~15h in outage recovery and ignored TERM,
    # so if nothing is logged for 90 min, kill it and relaunch.
    AGE=$(( $(date +%s) - $(stat -f %m data/quoter.jsonl 2>/dev/null || date +%s) ))
    STARTED_AGO=$(( $(date +%s) - QSTART ))
    if [ $AGE -gt 5400 ] && [ $STARTED_AGO -gt 5400 ]; then
      echo "$(date -u +%FT%TZ) watchdog: no pass logged for ${AGE}s; killing quoter"
      kill -KILL $QPID
    fi
    sleep 5
  done
  wait $QPID 2>/dev/null
  echo "$(date -u +%FT%TZ) quoter exited (status $?)"
  [ -e data/STOP ] && break
  sleep 30
done
echo "$(date -u +%FT%TZ) supervisor stopped (data/STOP present)"
