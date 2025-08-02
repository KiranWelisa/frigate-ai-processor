#!/bin/bash
# Frigate AI Processor - Quick Install Script
# Downloads files from GitHub and sets up the LXC container

set -e

# Configuration - UPDATE WITH YOUR GITHUB USERNAME
GITHUB_USER="KiranWelisa"  # <-- CHANGE THIS!
GITHUB_REPO="frigate-ai-processor"
GITHUB_BRANCH="main"

# LXC Configuration
CTID=${1:-300}
HOSTNAME="frigate-ai"
TEMPLATE="ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
STORAGE="local-lvm"
DISK_SIZE="16"  # GB
MEMORY="2048"   # MB
CORES="2"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Frigate AI Processor Installation    ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
echo

# Check if GitHub username is set
if [ "$GITHUB_USER" = "YOUR_GITHUB_USERNAME" ]; then
    echo -e "${RED}Error: Please update GITHUB_USER in this script with your GitHub username!${NC}"
    echo "Edit this script and change line 8"
    exit 1
fi

# Function to check prerequisites
check_prerequisites() {
    if ! command -v pct &> /dev/null; then
        echo -e "${RED}Error: This script must be run on a Proxmox VE host${NC}"
        exit 1
    fi
    
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}Error: This script must be run as root${NC}"
        exit 1
    fi
}

# Function to check if container exists
check_container() {
    if pct status $CTID &> /dev/null; then
        echo -e "${YELLOW}Container $CTID already exists!${NC}"
        read -p "Do you want to destroy it and create a new one? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "Stopping and destroying existing container..."
            pct stop $CTID 2>/dev/null || true
            sleep 2
            pct destroy $CTID
        else
            echo "Exiting..."
            exit 1
        fi
    fi
}

# Function to create container
create_container() {
    echo -e "${GREEN}Creating LXC container $CTID...${NC}"
    
    # Download template if needed
    if ! pveam list local | grep -q $TEMPLATE; then
        echo "Downloading Ubuntu 24.04 template..."
        pveam download local $TEMPLATE
    fi
    
    # Create container
    pct create $CTID local:vztmpl/$TEMPLATE \
        --hostname $HOSTNAME \
        --storage $STORAGE \
        --rootfs $DISK_SIZE \
        --memory $MEMORY \
        --swap 512 \
        --cores $CORES \
        --net0 name=eth0,bridge=vmbr0,ip=dhcp \
        --nameserver 1.1.1.1,8.8.8.8 \
        --unprivileged 0 \
        --features nesting=1,keyctl=1 \
        --onboot 1 \
        --start 1
    
    echo "Waiting for container to start..."
    sleep 10
}

# Function to install packages
install_packages() {
    echo -e "${GREEN}Installing system packages...${NC}"
    
    pct exec $CTID -- bash -c "
        export DEBIAN_FRONTEND=noninteractive
        apt-get update && \
        apt-get upgrade -y && \
        apt-get install -y \
            python3 python3-pip python3-venv \
            git curl wget nano \
            ffmpeg python3-dev build-essential \
            pkg-config libcairo2-dev libgirepository1.0-dev
    "
}

# Function to setup application
setup_application() {
    echo -e "${GREEN}Setting up Frigate AI Processor...${NC}"
    
    # Create directory structure
    pct exec $CTID -- mkdir -p /opt/frigate-ai-processor/{app/templates,config,logs,temp}
    
    # Clone or download from GitHub
    echo "Downloading application files from GitHub..."
    
    # Base URLs
    RAW_URL="https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$GITHUB_BRANCH"
    
    # Download main files
    echo "Downloading main.py..."
    pct exec $CTID -- wget -q -O /opt/frigate-ai-processor/app/main.py \
        "$RAW_URL/app/main.py"
    
    echo "Downloading templates..."
    pct exec $CTID -- wget -q -O /opt/frigate-ai-processor/app/templates/index.html \
        "$RAW_URL/app/templates/index.html"
    
    pct exec $CTID -- wget -q -O /opt/frigate-ai-processor/app/templates/config.html \
        "$RAW_URL/app/templates/config.html"
    
    echo "Downloading requirements.txt..."
    pct exec $CTID -- wget -q -O /opt/frigate-ai-processor/requirements.txt \
        "$RAW_URL/requirements.txt"
    
    echo "Downloading default configuration..."
    pct exec $CTID -- wget -q -O /opt/frigate-ai-processor/config/config.json \
        "$RAW_URL/config/config.json"
    
    # Setup Python environment
    echo "Creating Python virtual environment..."
    pct exec $CTID -- bash -c "
        cd /opt/frigate-ai-processor && \
        python3 -m venv venv && \
        source venv/bin/activate && \
        pip install --upgrade pip && \
        pip install -r requirements.txt
    "
}

# Function to create systemd service
create_service() {
    echo -e "${GREEN}Creating systemd service...${NC}"
    
    pct exec $CTID -- bash -c 'cat > /etc/systemd/system/frigate-ai-processor.service << EOF
[Unit]
Description=Frigate AI Processor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/frigate-ai-processor/app
Environment="PATH=/opt/frigate-ai-processor/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/frigate-ai-processor/venv/bin/python /opt/frigate-ai-processor/app/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF'
    
    # Enable and start service
    pct exec $CTID -- systemctl daemon-reload
    pct exec $CTID -- systemctl enable frigate-ai-processor
    pct exec $CTID -- systemctl start frigate-ai-processor
}

# Function to show completion message
show_completion() {
    # Get container IP
    IP=$(pct exec $CTID -- ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    
    # Check service status
    STATUS=$(pct exec $CTID -- systemctl is-active frigate-ai-processor)
    
    echo
    echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║        Installation Complete!          ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
    echo
    echo -e "${GREEN}Container Details:${NC}"
    echo "  • ID: $CTID"
    echo "  • IP: $IP"
    echo "  • Service: $STATUS"
    echo
    echo -e "${GREEN}Web Interface:${NC}"
    echo "  • Dashboard: http://$IP:5001"
    echo "  • Configuration: http://$IP:5001/config"
    echo
    echo -e "${YELLOW}Next Steps:${NC}"
    echo "1. Open http://$IP:5001/config in your browser"
    echo "2. Configure your MQTT broker settings"
    echo "3. Add your Google Gemini API key"
    echo "4. Set camera and object filters"
    echo "5. Save configuration"
    echo
    echo -e "${YELLOW}Useful Commands:${NC}"
    echo "• View logs:        pct exec $CTID -- journalctl -u frigate-ai-processor -f"
    echo "• Enter container:  pct enter $CTID"
    echo "• Restart service:  pct exec $CTID -- systemctl restart frigate-ai-processor"
    echo "• Check status:     pct exec $CTID -- systemctl status frigate-ai-processor"
    echo
    echo -e "${GREEN}GitHub Repository:${NC} https://github.com/$GITHUB_USER/$GITHUB_REPO"
    echo
}

# Main execution
main() {
    check_prerequisites
    check_container
    create_container
    install_packages
    setup_application
    create_service
    show_completion
}

# Run main function
main

# Optional: Create update script
echo -e "${GREEN}Creating update script...${NC}"
pct exec $CTID -- bash -c "cat > /opt/frigate-ai-processor/update.sh << 'EOF'
#!/bin/bash
# Update Frigate AI Processor from GitHub

cd /opt/frigate-ai-processor

# Stop service
systemctl stop frigate-ai-processor

# Backup config
cp config/config.json config/config.json.bak

# Download latest files
wget -q -O app/main.py https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$GITHUB_BRANCH/app/main.py
wget -q -O app/templates/index.html https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$GITHUB_BRANCH/app/templates/index.html
wget -q -O app/templates/config.html https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$GITHUB_BRANCH/app/templates/config.html
wget -q -O requirements.txt https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$GITHUB_BRANCH/requirements.txt

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt

# Start service
systemctl start frigate-ai-processor

echo \"Update complete!\"
EOF"

pct exec $CTID -- chmod +x /opt/frigate-ai-processor/update.sh

echo -e "${GREEN}Update script created at: /opt/frigate-ai-processor/update.sh${NC}"