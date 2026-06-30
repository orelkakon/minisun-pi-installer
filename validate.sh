#!/bin/sh
set -e
PASS=0

echo "── Python syntax ──────────────────────────"
for f in main.py installer.py; do
  python3 -m py_compile "$f" && echo "  ✓ $f" || { echo "  ✗ $f"; PASS=1; }
done

echo "── JavaScript syntax ──────────────────────"
python3 - <<'EOF'
import re, sys
html = open("static/index.html").read()
match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
if not match:
    print("  ✗ No <script> block found in index.html")
    sys.exit(1)
open("/tmp/_minisun_check.js", "w").write(match.group(1))
EOF
node --check /tmp/_minisun_check.js && echo "  ✓ static/index.html" || { echo "  ✗ static/index.html"; PASS=1; }

echo "───────────────────────────────────────────"
if [ $PASS -eq 0 ]; then
  echo "  All checks passed."
else
  echo "  One or more checks failed — fix before committing."
  exit 1
fi
