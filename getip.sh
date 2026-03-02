wget -O - -q icanhazip.com > /var/downloads/clouddata/nextcloud/work/ip.txt
FILE="/var/downloads/clouddata/nextcloud/work/ip_history.txt"
IP=$(curl -s https://icanhazip.com)
if ! grep -q "$IP" "$FILE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $IP" >> "$FILE"
fi
