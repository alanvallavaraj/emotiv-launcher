#!/bin/bash
# Double-click to open the headset launcher menu.
# No EMOTIV Launcher, no login, no cloud.
cd "$(dirname "$0")" || exit 1
PY=/usr/bin/python3
[ -x "$PY" ] || PY=$(command -v python3) || { echo "No python3 found."; read -r; exit 1; }

while true; do
  clear
  echo "EMOTIV headset launcher"
  echo "======================="
  echo
  echo "  1  Status            what the service and headset are doing"
  echo "  2  Connect           connect the headset, show battery and contact"
  echo "  3  Start background  keep it running and self-healing"
  echo "  4  Stop background"
  echo "  5  Restart service   use when things are stuck"
  echo "  6  Show log"
  echo "  q  Quit"
  echo
  printf "Choose: "
  read -r choice
  echo
  case "$choice" in
    1) "$PY" launcherd.py status ;;
    2) "$PY" launcherd.py connect ;;
    3) "$PY" launcherd.py start ;;
    4) "$PY" launcherd.py stop ;;
    5) "$PY" launcherd.py restart-service ;;
    6) tail -40 "$HOME/.emotiv-launcher/launcherd.log" 2>/dev/null || echo "No log yet." ;;
    q|Q) exit 0 ;;
    *) echo "Pick 1-6 or q." ;;
  esac
  echo
  printf "Press return to continue... "
  read -r _
done
