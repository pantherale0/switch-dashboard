#!/bin/bash
set -e

INSTALL_DIR="/opt/switch-dashboard"
DATA_DIR="/var/lib/switch-dashboard"
ENV_FILE="/etc/switch-dashboard.env"

echo "=== Switch Dashboard Installation ==="

if ! id switch-dashboard >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin switch-dashboard
fi
install -d -m 0700 -o switch-dashboard -g switch-dashboard "$DATA_DIR"

if systemctl list-unit-files switch-dashboard.service >/dev/null 2>&1; then
    systemctl stop switch-dashboard.service || true
fi

# Migrate data from releases that stored mutable state in the application tree.
for item in dashboard.db switch_dashboard.db network_scanner.db config.json config.json.bak settings.json notes.json counters.json history_hourly.json history_daily.json last_port_scan.ts mac_vendors.txt oui.txt oui36.txt logs backup; do
    if [ -e "$INSTALL_DIR/$item" ] && [ ! -e "$DATA_DIR/$item" ]; then
        cp -a "$INSTALL_DIR/$item" "$DATA_DIR/$item"
    fi
done
chown -R switch-dashboard:switch-dashboard "$DATA_DIR"

if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
fi

cp -r ./* "$INSTALL_DIR/"
cd "$INSTALL_DIR"

echo "[1/3] Installing Python dependencies..."

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 14))'; then
    echo "Python 3.14 or newer is required."
    exit 1
fi
if ! python3 -c 'import venv' >/dev/null 2>&1; then
    echo "The Python venv module is required."
    exit 1
fi
python3 -m venv "$INSTALL_DIR/.uv-bootstrap"
"$INSTALL_DIR/.uv-bootstrap/bin/pip" install --quiet uv==0.11.16
"$INSTALL_DIR/.uv-bootstrap/bin/uv" sync --project "$INSTALL_DIR" --frozen --no-dev
PYTHON_EXEC="$INSTALL_DIR/.venv/bin/python"

echo "[2/3] Creating systemd service..."
chown -R switch-dashboard:switch-dashboard "$INSTALL_DIR"
if [ ! -f "$ENV_FILE" ]; then
    ENCRYPTION_KEY=$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
    cat > "$ENV_FILE" << EOF
DASHBOARD_DATA_DIR=$DATA_DIR
DASHBOARD_ENCRYPTION_KEY=$ENCRYPTION_KEY
DASHBOARD_ENCRYPTION_KEY_ID=v1
DASHBOARD_PREVIOUS_ENCRYPTION_KEYS=
# Configure these values before starting the service:
OIDC_ISSUER=
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_REDIRECT_URI=
OIDC_ROLES_CLAIM=groups
OIDC_ADMIN_GROUP=switch-dashboard-admin
OIDC_OPERATOR_GROUP=switch-dashboard-operator
OIDC_VIEWER_GROUP=switch-dashboard-viewer
OIDC_TOKEN_AUTH_METHOD=client_secret_basic
MANAGEMENT_NETWORKS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
ALLOW_INSECURE_HTTP=false
TRUSTED_PROXY_COUNT=0
EOF
    chmod 0600 "$ENV_FILE"
fi
cat > /etc/systemd/system/switch-dashboard.service << EOF
[Unit]
Description=Switch Dashboard - Ports and Traffic
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=switch-dashboard
Group=switch-dashboard
WorkingDirectory=/opt/switch-dashboard
EnvironmentFile=/etc/switch-dashboard.env
UMask=0077
ExecStart=$PYTHON_EXEC -m gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 4 --timeout 60 app:app
Restart=always
RestartSec=10
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable switch-dashboard
if grep -Eq '^OIDC_ISSUER=.+$' "$ENV_FILE" && grep -Eq '^OIDC_CLIENT_ID=.+$' "$ENV_FILE"; then
    systemctl restart switch-dashboard
else
    echo "Edit $ENV_FILE with your OIDC settings, then run: systemctl start switch-dashboard"
fi

echo "[3/3] Service status:"
systemctl status switch-dashboard --no-pager || true

echo ""
echo "=== Installation completed ==="
echo "Dashboard listens on: http://127.0.0.1:8080"
echo ""
echo "Recommended nginx location (set TRUSTED_PROXY_COUNT=1 when using it):"
echo "  location / {"
echo "    proxy_pass http://127.0.0.1:8080;"
echo '    proxy_set_header Host $host;'
echo '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;'
echo '    proxy_set_header X-Forwarded-Proto $scheme;'
echo "  }"
