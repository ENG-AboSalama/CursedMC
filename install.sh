#!/bin/bash

# ============================================
# CursedMC - Installation & Management Script
# ============================================

set -e

PURPLE='\033[0;35m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

INSTALL_DIR="/opt/cursedmc"
SERVICE_NAME="cursedmc"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

show_banner() {
    echo -e "${PURPLE}"
    cat << "EOF"
   ██████╗██╗   ██╗██████╗ ███████╗███████╗██████╗ ███╗   ███╗ ██████╗
  ██╔════╝██║   ██║██╔══██╗██╔════╝██╔════╝██╔══██╗████╗ ████║██╔════╝
  ██║     ██║   ██║██████╔╝███████╗█████╗  ██║  ██║██╔████╔██║██║     
  ██║     ██║   ██║██╔══██╗╚════██║██╔══╝  ██║  ██║██║╚██╔╝██║██║     
  ╚██████╗╚██████╔╝██║  ██║███████║███████╗██████╔╝██║ ╚═╝ ██║╚██████╗
   ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚══════╝╚═════╝ ╚═╝     ╚═╝ ╚═════╝
EOF
    echo ""
    echo "        Minecraft Server Manager with Web Dashboard"
    echo "              © 2026 CursedMC. All rights reserved."
    echo -e "${NC}"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Error: Please run as root: sudo bash install.sh${NC}"
        exit 1
    fi
}

show_help() {
    echo -e "${CYAN}CursedMC Installation Script${NC}"
    echo ""
    echo "Usage: sudo bash install.sh [OPTION]"
    echo ""
    echo "Options:"
    echo "  install     Install CursedMC (default if no option given)"
    echo "  reinstall   Fresh install while keeping server data (worlds, mods, configs)"
    echo "  uninstall   Completely remove CursedMC from the system"
    echo "  update      Update CursedMC to the latest version (keeps settings)"
    echo "  status      Show service status"
    echo "  restart     Restart CursedMC service"
    echo "  help        Show this help message"
    echo ""
    echo "Examples:"
    echo "  sudo bash install.sh           # Install CursedMC"
    echo "  sudo bash install.sh install   # Install CursedMC"
    echo "  sudo bash install.sh reinstall # Fresh install, keep server data"
    echo "  sudo bash install.sh uninstall # Remove CursedMC"
    echo ""
}

install_cursedmc() {
    echo -e "${YELLOW}Starting CursedMC Installation...${NC}"
    echo ""

    echo -e "${YELLOW}[1/5]${NC} Installing system packages..."
    apt-get update -qq
    
    if apt-cache show openjdk-21-jre-headless &>/dev/null; then
        echo -e "  Installing Java 21 (recommended)..."
        apt-get install -y -qq openjdk-21-jre-headless python3 python3-pip python3-venv curl wget screen
    elif apt-cache show openjdk-17-jre-headless &>/dev/null; then
        echo -e "${YELLOW}  Java 21 not available, installing Java 17 (minimum supported)...${NC}"
        apt-get install -y -qq openjdk-17-jre-headless python3 python3-pip python3-venv curl wget screen
    else
        echo -e "${RED}Error: Neither Java 21 nor Java 17 is available in your repositories.${NC}"
        echo "Please manually install Java 17 or higher before running this installer."
        exit 1
    fi
    
    if ! java -version &>/dev/null; then
        echo -e "${RED}Error: Java installation failed.${NC}"
        exit 1
    fi
    JAVA_VERSION=$(java -version 2>&1 | head -n 1 | cut -d'"' -f2 | cut -d'.' -f1)
    if [ "$JAVA_VERSION" = "1" ]; then
        JAVA_VERSION=$(java -version 2>&1 | head -n 1 | cut -d'"' -f2 | cut -d'.' -f2)
    fi
    echo -e "${GREEN}✓ Java $JAVA_VERSION installed${NC}"

    echo -e "${YELLOW}[2/5]${NC} Installing Playit.gg..."
    if ! command -v playit &> /dev/null; then
        curl -SsL https://playit-cloud.github.io/ppa/key.gpg | gpg --dearmor | tee /etc/apt/trusted.gpg.d/playit.gpg >/dev/null
        echo "deb [signed-by=/etc/apt/trusted.gpg.d/playit.gpg] https://playit-cloud.github.io/ppa/data ./" | tee /etc/apt/sources.list.d/playit-cloud.list >/dev/null
        apt-get update -qq
        apt-get install -y -qq playit
    fi
    echo -e "${GREEN}✓ Playit installed${NC}"

    echo -e "${YELLOW}[3/5]${NC} Setting up CursedMC..."
    
    mkdir -p $INSTALL_DIR/{server,server/mods,backups,logs,dashboard/templates}

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp -r $SCRIPT_DIR/dashboard/* $INSTALL_DIR/dashboard/ 2>/dev/null || true

    echo -e "${YELLOW}[4/5]${NC} Setting up Python environment..."
    python3 -m venv $INSTALL_DIR/venv
    source $INSTALL_DIR/venv/bin/activate
    pip install -q --upgrade pip
    pip install -q flask flask-socketio psutil requests pyngrok python-engineio python-socketio werkzeug urllib3 bcrypt

    echo -e "${YELLOW}[5/5]${NC} Creating CursedMC service..."

    systemctl stop playit 2>/dev/null || true
    systemctl disable playit 2>/dev/null || true
    rm /etc/systemd/system/playit.service 2>/dev/null || true

    cat > $SERVICE_FILE << 'EOF'
[Unit]
Description=CursedMC Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/cursedmc
ExecStart=/opt/cursedmc/venv/bin/python /opt/cursedmc/dashboard/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable cursedmc
    systemctl start cursedmc

    SERVER_IP=$(hostname -I | awk '{print $1}' | head -1)

    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              ✓ Installation Complete!                     ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "🌐 Open in browser: ${PURPLE}http://${SERVER_IP}:8080${NC}"
    echo ""
    echo -e "📝 Important: A password is ${RED}REQUIRED${NC} during setup."
    echo ""
    echo -e "To uninstall later: ${YELLOW}sudo bash install.sh uninstall${NC}"
    echo ""
    echo -e "💜 © 2026 CursedMC. All rights reserved."
}

uninstall_cursedmc() {
    echo -e "${YELLOW}Starting CursedMC Uninstallation...${NC}"
    echo ""
    
    echo -e "${RED}⚠️  WARNING: This will completely remove CursedMC from your system.${NC}"
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo -e "${GREEN}Uninstallation cancelled.${NC}"
        exit 0
    fi
    
    echo ""
    
    echo -e "${CYAN}Do you want to keep your Minecraft server data?${NC}"
    echo "  - World files, plugins, mods, configurations"
    echo ""
    read -p "Keep server data? (yes/no): " keep_data
    
    echo ""
    echo -e "${YELLOW}[1/5]${NC} Stopping all running services..."
    
    if [ -f "$INSTALL_DIR/server.pid" ]; then
        PID=$(cat "$INSTALL_DIR/server.pid" 2>/dev/null || echo "")
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo "  Stopping Minecraft server (PID: $PID)..."
            kill "$PID" 2>/dev/null || true
            sleep 3
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$INSTALL_DIR/server.pid"
    fi
    
    if screen -list | grep -q "playit"; then
        echo "  Stopping Playit tunnel..."
        screen -S playit -X quit 2>/dev/null || true
    fi
    
    pkill -f "ngrok" 2>/dev/null || true
    
    echo -e "${GREEN}  ✓ Services stopped${NC}"
    
    echo -e "${YELLOW}[2/5]${NC} Stopping and removing CursedMC service..."
    
    if systemctl is-active --quiet $SERVICE_NAME 2>/dev/null; then
        systemctl stop $SERVICE_NAME
    fi
    if systemctl is-enabled --quiet $SERVICE_NAME 2>/dev/null; then
        systemctl disable $SERVICE_NAME
    fi
    
    rm -f $SERVICE_FILE
    systemctl daemon-reload
    
    echo -e "${GREEN}  ✓ Service removed${NC}"
    
    echo -e "${YELLOW}[3/5]${NC} Removing installed files..."
    
    if [ "$keep_data" = "yes" ]; then
        echo "  Keeping server data..."
        
        BACKUP_DIR="/opt/cursedmc_backup_$(date +%Y%m%d_%H%M%S)"
        if [ -d "$INSTALL_DIR/server" ]; then
            mkdir -p $BACKUP_DIR
            cp -r $INSTALL_DIR/server $BACKUP_DIR/
            echo -e "${CYAN}  Server data backed up to: $BACKUP_DIR${NC}"
        fi
        
        rm -rf $INSTALL_DIR/venv
        rm -rf $INSTALL_DIR/dashboard
        rm -rf $INSTALL_DIR/logs
        rm -f $INSTALL_DIR/cursedmc.db
        rm -f $INSTALL_DIR/*.pid
        rm -rf $INSTALL_DIR/backups
        rm -rf $INSTALL_DIR/server 
    else
        rm -rf $INSTALL_DIR
    fi
    
    echo -e "${GREEN}  ✓ Files removed${NC}"
    
    echo -e "${YELLOW}[4/5]${NC} Cleaning up system configuration..."
    
    screen -wipe 2>/dev/null || true
    
    echo -e "${GREEN}  ✓ System cleaned${NC}"
    
    echo -e "${YELLOW}[5/5]${NC} Finalizing..."
    
    if [ -d "$INSTALL_DIR" ]; then
        rmdir $INSTALL_DIR 2>/dev/null || true
    fi
    
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              ✓ Uninstallation Complete!                   ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    if [ "$keep_data" = "yes" ] && [ -d "$BACKUP_DIR" ]; then
        echo -e "📁 Your server data has been saved to:"
        echo -e "   ${CYAN}$BACKUP_DIR${NC}"
        echo ""
    fi
    
    echo -e "CursedMC has been completely removed from your system."
    echo ""
    echo -e "Note: The following packages were NOT removed:"
    echo "  - Java (openjdk-21-jre-headless)"
    echo "  - Playit"
    echo "  - Python3"
    echo ""
    echo "To remove them manually:"
    echo "  sudo apt remove openjdk-21-jre-headless playit"
    echo ""
    echo -e "💜 Thank you for using CursedMC!"
}

reinstall_cursedmc() {
    echo -e "${YELLOW}Reinstalling CursedMC...${NC}"
    echo ""
    echo -e "${CYAN}This will perform a fresh installation while preserving:${NC}"
    echo "  ✓ Server files (worlds, mods, plugins, configs)"
    echo "  ✓ Backups"
    echo ""
    echo -e "${RED}This will RESET:${NC}"
    echo "  ✗ Dashboard settings (password, ngrok token, etc.)"
    echo "  ✗ Database (cursedmc.db)"
    echo "  ✗ Python environment"
    echo ""
    read -p "Continue with reinstall? (yes/no): " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo -e "${GREEN}Reinstall cancelled.${NC}"
        exit 0
    fi
    
    echo ""
    echo -e "${YELLOW}[1/6]${NC} Stopping services..."
    
    if [ -f "$INSTALL_DIR/server.pid" ]; then
        PID=$(cat "$INSTALL_DIR/server.pid" 2>/dev/null || echo "")
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo "  Stopping Minecraft server..."
            kill "$PID" 2>/dev/null || true
            sleep 3
            kill -9 "$PID" 2>/dev/null || true
        fi
    fi
    
    if screen -list | grep -q "playit"; then
        screen -S playit -X quit 2>/dev/null || true
    fi
    
    pkill -f "ngrok" 2>/dev/null || true
    
    systemctl stop $SERVICE_NAME 2>/dev/null || true
    systemctl disable $SERVICE_NAME 2>/dev/null || true
    
    echo -e "${GREEN}  ✓ Services stopped${NC}"
    
    echo -e "${YELLOW}[2/6]${NC} Backing up server data..."
    
    TEMP_SERVER=""
    TEMP_BACKUPS=""
    if [ -d "$INSTALL_DIR/server" ]; then
        TEMP_SERVER=$(mktemp -d)
        cp -r $INSTALL_DIR/server/* $TEMP_SERVER/ 2>/dev/null || true
        echo -e "${GREEN}  ✓ Server data preserved${NC}"
    fi
    if [ -d "$INSTALL_DIR/backups" ]; then
        TEMP_BACKUPS=$(mktemp -d)
        cp -r $INSTALL_DIR/backups/* $TEMP_BACKUPS/ 2>/dev/null || true
        echo -e "${GREEN}  ✓ Backups preserved${NC}"
    fi
    
    echo -e "${YELLOW}[3/6]${NC} Removing old installation..."
    
    rm -f $SERVICE_FILE
    systemctl daemon-reload
    
    rm -rf $INSTALL_DIR
    
    echo -e "${GREEN}  ✓ Old installation removed${NC}"
    
    echo -e "${YELLOW}[4/6]${NC} Performing fresh install..."
    
    apt-get update -qq
    if apt-cache show openjdk-21-jre-headless &>/dev/null; then
        apt-get install -y -qq openjdk-21-jre-headless python3 python3-pip python3-venv curl wget screen
    elif apt-cache show openjdk-17-jre-headless &>/dev/null; then
        apt-get install -y -qq openjdk-17-jre-headless python3 python3-pip python3-venv curl wget screen
    fi
    
    if ! command -v playit &> /dev/null; then
        curl -SsL https://playit-cloud.github.io/ppa/key.gpg | gpg --dearmor | tee /etc/apt/trusted.gpg.d/playit.gpg >/dev/null
        echo "deb [signed-by=/etc/apt/trusted.gpg.d/playit.gpg] https://playit-cloud.github.io/ppa/data ./" | tee /etc/apt/sources.list.d/playit-cloud.list >/dev/null
        apt-get update -qq
        apt-get install -y -qq playit
    fi
    
    mkdir -p $INSTALL_DIR/{server,server/mods,backups,logs,dashboard/templates}
    
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp -r $SCRIPT_DIR/dashboard/* $INSTALL_DIR/dashboard/ 2>/dev/null || true
    
    python3 -m venv $INSTALL_DIR/venv
    source $INSTALL_DIR/venv/bin/activate
    pip install -q --upgrade pip
    pip install -q flask flask-socketio psutil requests pyngrok python-engineio python-socketio werkzeug urllib3 bcrypt
    
    echo -e "${GREEN}  ✓ Fresh install complete${NC}"
    
    echo -e "${YELLOW}[5/6]${NC} Restoring server data..."
    
    if [ -n "$TEMP_SERVER" ] && [ -d "$TEMP_SERVER" ]; then
        cp -r $TEMP_SERVER/* $INSTALL_DIR/server/ 2>/dev/null || true
        rm -rf $TEMP_SERVER
        echo -e "${GREEN}  ✓ Server data restored${NC}"
    fi
    if [ -n "$TEMP_BACKUPS" ] && [ -d "$TEMP_BACKUPS" ]; then
        cp -r $TEMP_BACKUPS/* $INSTALL_DIR/backups/ 2>/dev/null || true
        rm -rf $TEMP_BACKUPS
        echo -e "${GREEN}  ✓ Backups restored${NC}"
    fi
    
    echo -e "${YELLOW}[6/6]${NC} Creating and starting service..."
    
    cat > $SERVICE_FILE << 'SERVICEEOF'
[Unit]
Description=CursedMC Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/cursedmc
ExecStart=/opt/cursedmc/venv/bin/python /opt/cursedmc/dashboard/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF
    
    systemctl daemon-reload
    systemctl enable cursedmc
    systemctl start cursedmc
    
    SERVER_IP=$(hostname -I | awk '{print $1}' | head -1)
    
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              ✓ Reinstallation Complete!                   ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "🌐 Open in browser: ${PURPLE}http://${SERVER_IP}:8080${NC}"
    echo ""
    echo -e "📝 Your server data (worlds, mods, configs) has been preserved."
    echo -e "📝 You will need to complete the setup wizard again (password, etc.)"
    echo ""
    echo -e "💜 © 2026 CursedMC. All rights reserved."
}

update_cursedmc() {
    echo -e "${YELLOW}Updating CursedMC...${NC}"
    echo ""
    
    if [ ! -d "$INSTALL_DIR" ]; then
        echo -e "${RED}Error: CursedMC is not installed.${NC}"
        echo "Run: sudo bash install.sh install"
        exit 1
    fi
    
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    echo -e "${YELLOW}[1/3]${NC} Stopping service..."
    systemctl stop $SERVICE_NAME 2>/dev/null || true
    
    echo -e "${YELLOW}[2/3]${NC} Updating files..."
    cp -r $SCRIPT_DIR/dashboard/* $INSTALL_DIR/dashboard/ 2>/dev/null || true
    
    source $INSTALL_DIR/venv/bin/activate
    pip install -q --upgrade flask flask-socketio psutil requests pyngrok python-engineio python-socketio werkzeug
    
    echo -e "${YELLOW}[3/3]${NC} Restarting service..."
    systemctl start $SERVICE_NAME
    
    echo ""
    echo -e "${GREEN}✓ CursedMC has been updated!${NC}"
}

show_status() {
    echo -e "${CYAN}CursedMC Service Status${NC}"
    echo ""
    
    if systemctl is-active --quiet $SERVICE_NAME 2>/dev/null; then
        echo -e "Service: ${GREEN}● Running${NC}"
    else
        echo -e "Service: ${RED}● Stopped${NC}"
    fi
    
    if [ -f "$INSTALL_DIR/server.pid" ]; then
        PID=$(cat $INSTALL_DIR/server.pid 2>/dev/null)
        if [ -n "$PID" ] && kill -0 $PID 2>/dev/null; then
            echo -e "Minecraft Server: ${GREEN}● Running (PID: $PID)${NC}"
        else
            echo -e "Minecraft Server: ${YELLOW}● Not running${NC}"
        fi
    else
        echo -e "Minecraft Server: ${YELLOW}● Not running${NC}"
    fi
    
    if screen -list | grep -q "playit"; then
        echo -e "Playit Tunnel: ${GREEN}● Running${NC}"
    else
        echo -e "Playit Tunnel: ${YELLOW}● Not running${NC}"
    fi
    
    echo ""
    systemctl status $SERVICE_NAME --no-pager 2>/dev/null || true
}

restart_service() {
    echo -e "${YELLOW}Restarting CursedMC service...${NC}"
    systemctl restart $SERVICE_NAME
    echo -e "${GREEN}✓ Service restarted${NC}"
}

main() {
    show_banner
    check_root
    
    case "${1:-install}" in
        install)
            install_cursedmc
            ;;
        reinstall)
            reinstall_cursedmc
            ;;
        uninstall)
            uninstall_cursedmc
            ;;
        update)
            update_cursedmc
            ;;
        status)
            show_status
            ;;
        restart)
            restart_service
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

main "$@"
