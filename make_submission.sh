#!/bin/sh
# Usage: ./make_submission.sh ENTRY1 ENTRY2   (e.g. 2024CS10001 2024CS10002)
# Builds A3_<E1>_<E2>.zip with the layout required by the handout (section 7).
set -e
[ $# -eq 2 ] || { echo "usage: $0 ENTRY1 ENTRY2" >&2; exit 1; }
cd "$(dirname "$0")"
E=$(printf '%s\n%s\n' "$(echo "$1" | tr a-z A-Z)" "$(echo "$2" | tr a-z A-Z)" | sort | paste -sd_)
NAME="A3_$E"
REPORT=report/report.pdf
[ -f "$REPORT" ] || { echo "report/report.pdf missing" >&2; exit 1; }
STAGE=$(mktemp -d)
mkdir -p "$STAGE/$NAME/src"
cp -r netcfg client server "$STAGE/$NAME/"
cp src/packet.py src/tcp.py src/client.py src/server.py "$STAGE/$NAME/src/"
cp README.md "$STAGE/$NAME/"
cp "$REPORT" "$STAGE/$NAME/report.pdf"
chmod +x "$STAGE/$NAME"/netcfg/*.sh "$STAGE/$NAME"/client/run-client "$STAGE/$NAME"/server/run-server
rm -f "$NAME.zip"
python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2], sys.argv[3])" "$PWD/$NAME" "$STAGE/$NAME" .
rm -rf "$STAGE"
echo "wrote $NAME.zip"; python3 -m zipfile -l "$NAME.zip"
