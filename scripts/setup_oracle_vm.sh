#!/usr/bin/env bash
# Darslik Studiyasi (Tarjima) serverini Oracle Cloud'ning BEPUL (Always Free)
# virtual serverida (Ubuntu) doimiy ishlaydigan qilib sozlaydi.
#
# Bu skript serverning O'ZIDA (Oracle Cloud Shell orqali SSH ulanib) bir marta
# ishga tushiriladi. U:
#   1. Kerakli dasturlarni (Python, ffmpeg, git) o'rnatadi.
#   2. Loyihani /opt/tarjima papkasiga joylashtiradi.
#   3. Video/natijalarni /opt/tarjima-storage papkasida saqlaydigan qilib sozlaydi.
#   4. Serverni "systemd xizmati" sifatida ro'natadi - bu shuni anglatadiki,
#      server doim orqa fonda ishlab turadi, kompyuter/planshet o'chsa ham,
#      hatto Oracle serveri qayta ishga tushsa ham (masalan quvvat uzilishi
#      yoki texnik ishlar tufayli) AVTOMATIK o'zi qayta ishga tushadi.
#   5. 80-portni (oddiy http://IP - qo'shimcha raqamsiz) ochadi.
#
# Ishlatish (Oracle Cloud Shell'da yoki SSH orqali serverga ulanib):
#   curl -fsSL https://raw.githubusercontent.com/baxtishodibrohimov-rgb/Tarjima/main/scripts/setup_oracle_vm.sh | bash
set -euo pipefail

REPO_URL="https://github.com/baxtishodibrohimov-rgb/Tarjima.git"
APP_DIR="/opt/tarjima"
STORAGE_DIR="/opt/tarjima-storage"
SERVICE_NAME="tarjima"
PORT=80

echo "== 1/6: Tizim paketlari o'rnatilmoqda (biroz vaqt oladi)... =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git

echo "== 2/6: Loyiha kodi olinmoqda... =="
if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" pull
else
  sudo git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

echo "== 3/6: Python muhiti va kutubxonalar o'rnatilmoqda... =="
sudo python3 -m venv "$APP_DIR/.venv"
sudo "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "== 4/6: Saqlash papkasi tayyorlanmoqda... =="
sudo mkdir -p "$STORAGE_DIR"

echo "== 5/6: Doimiy xizmat (systemd) sozlanmoqda... =="
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Darslik Studiyasi (Tarjima) server
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=STORAGE_DIR=${STORAGE_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "== 6/6: Xavfsizlik devorida ${PORT}-port ochilmoqda... =="
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow "${PORT}/tcp" || true
fi
# Oracle'ning Ubuntu tasviri odatda iptables orqali portlarni cheklaydi (ufw'dan tashqari) -
# shuning uchun iptables'ga ham to'g'ridan-to'g'ri qo'shamiz va saqlaymiz.
sudo iptables -C INPUT -p tcp --dport "${PORT}" -j ACCEPT 2>/dev/null || \
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "${PORT}" -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

PUBLIC_IP=$(curl -s -4 ifconfig.me || echo "<server-ning-tashqi-IP-manzili>")

echo ""
echo "======================================================================"
echo " TAYYOR! Server ishga tushdi va doimiy ishlaydigan qilib sozlandi."
echo ""
echo " Planshetda brauzerda oching:"
echo ""
echo "     http://${PUBLIC_IP}"
echo ""
echo " DIQQAT: Bu manzil ishlashi uchun Oracle Cloud Console'da ham"
echo " ${PORT}-portni ochish kerak (Virtual Cloud Network -> Security List/"
echo " Network Security Group -> Ingress Rule qo'shish) - qo'llanmadagi"
echo " tegishli bo'limga qarang."
echo ""
echo " Foydali buyruqlar (keyinchalik kerak bo'lsa):"
echo "   sudo systemctl status ${SERVICE_NAME}     - server holatini ko'rish"
echo "   sudo systemctl restart ${SERVICE_NAME}    - qayta ishga tushirish"
echo "   sudo journalctl -u ${SERVICE_NAME} -f     - jonli loglarni ko'rish"
echo "======================================================================"
