#!/bin/bash
# Frigate AI Processor - Quick Install Script for Proxmox VE
# This script creates a new LXC container and sets up the application inside it.

set -e

# --- Configuration ---
CTID=${1:-300} # Default CT ID is 300, or pass as first argument
HOSTNAME="frigate-ai-processor"
TEMPLATE="ubuntu-22.04-standard_22.04-1_amd64.tar.zst" # Using a stable LTS release
STORAGE="local-lvm" # Change if your storage is named differently
DISK_SIZE="8" # FIX: Removed the 'G' suffix. Size is in GB.
MEMORY="1024"
CORES="1"
BRIDGE="vmbr0" # Change if your network bridge is different

# --- Colors for logging ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# --- Helper function for logging ---
log() {
    echo -e "${GREEN}$(date '+%Y-%m-%d %H:%M:%S') - $1${NC}"
}

error() {
    echo -e "${RED}$(date '+%Y-%m-%d %H:%M:%S') - ERROR: $1${NC}"
    exit 1
}

# --- 1. Prerequisites Check ---
log "Checking prerequisites..."
if ! command -v pct &> /dev/null; then
    error "This script must be run on a Proxmox VE host."
fi

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root."
fi

# --- 2. Check for existing container ---
if pct status $CTID &> /dev/null; then
    echo -e "${YELLOW}Container $CTID already exists.${NC}"
    read -p "Do you want to destroy it and create a new one? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log "Stopping and destroying existing container..."
        pct stop $CTID &>/dev/null || true
        pct destroy $CTID
    else
        echo "Aborted."
        exit 1
    fi
fi

# --- 3. Create the LXC Container ---
log "Creating LXC container (ID: $CTID)..."
if ! pveam list local | grep -q $TEMPLATE; then
    log "Downloading Ubuntu 22.04 template..."
    pveam download local $TEMPLATE
fi

# FIX: Corrected the --rootfs parameter syntax.
pct create $CTID local:vztmpl/$TEMPLATE \
    --hostname $HOSTNAME \
    --rootfs $STORAGE:$DISK_SIZE \
    --memory $MEMORY \
    --swap 512 \
    --cores $CORES \
    --net0 name=eth0,bridge=$BRIDGE,ip=dhcp \
    --onboot 1 \
    --start 1

log "Waiting for container to get an IP address..."
sleep 15 # Give container time to boot and get DHCP lease

# --- 4. Install Dependencies inside the Container ---
log "Installing dependencies inside the container..."
pct exec $CTID -- apt-get update
pct exec $CTID -- apt-get upgrade -y
pct exec $CTID -- apt-get install -y python3-pip python3-venv git ffmpeg

# --- 5. Setup Application inside the Container ---
log "Setting up the application..."
APP_DIR="/opt/frigate-ai-processor"
pct exec $CTID -- git clone https://github.com/KiranWelisa/frigate-ai-processor.git $APP_DIR

log "Creating Python virtual environment..."
pct exec $CTID -- python3 -m venv $APP_DIR/venv

log "Installing Python dependencies..."
pct exec $CTID -- bash -c "source $APP_DIR/venv/bin/activate && pip install --upgrade pip && pip install -r $APP_DIR/requirements.txt"

# --- 6. Create and Enable Systemd Service ---
log "Creating systemd service for the application..."
pct exec $CTID -- bash -c "cat > /etc/systemd/system/frigate-ai-processor.service <<'EOF'
[Unit]
Description=Frigate AI Processor
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=$APP_DIR
Environment=\"PATH=$APP_DIR/venv/bin\"
ExecStart=$APP_DIR/venv/bin/python app/main.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF"

log "Enabling and starting the service..."
pct exec $CTID -- systemctl daemon-reload
pct exec $CTID -- systemctl enable --now frigate-ai-processor

# --- 7. Show Completion Message ---
IP=$(pct exec $CTID -- ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
if [ -z "$IP" ]; then
    IP="<could not detect>"
fi

log "Installation Complete!"
echo -e "--------------------------------------------------"
echo -e "  ${GREEN}Frigate AI Processor is now running!${NC}"
echo -e "--------------------------------------------------"
echo -e "  Container ID: ${YELLOW}$CTID${NC}"
echo -e "  IP Address:   ${YELLOW}$IP${NC}"
echo
echo -e "  Access the web interface at:"
echo -e "  Dashboard:     ${YELLOW}http://$IP:5001/${NC}"
echo -e "  Configuration: ${YELLOW}http://$IP:5001/config${NC}"
echo
echo -e "  ${YELLOW}IMPORTANT: Go to the configuration page to set your API keys and MQTT details.${NC}"
echo
echo -e "  To view logs, run: ${YELLOW}pct exec $CTID -- journalctl -u frigate-ai-processor -f${NC}"
echo -e "--------------------------------------------------"
