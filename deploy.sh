#!/bin/bash
set -e
EC2_IP="16.164.124.189"
KEY="$HOME/.ssh/minisun-pi-key.pem"
SSH="ssh -i $KEY -o StrictHostKeyChecking=no ubuntu@$EC2_IP"

cd "$(dirname "$0")"

echo "Uploading files..."
scp -i "$KEY" -o StrictHostKeyChecking=no \
  main.py installer.py requirements.txt \
  ubuntu@$EC2_IP:~/app/

echo "Stopping old server..."
$SSH "pkill -f 'uvicorn main:app' 2>/dev/null; exit 0" || true

echo "Starting server..."
$SSH "cd ~/app && nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 >> ~/app.log 2>&1 & sudo systemctl restart nginx"

sleep 4
STATUS=$($SSH "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000")
if [ "$STATUS" = "200" ]; then
  echo "Done. Live at http://$EC2_IP:8000"
else
  echo "Warning: server returned $STATUS"
fi
