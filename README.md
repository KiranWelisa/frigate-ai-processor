# Frigate AI Processor

An intelligent event processor that monitors Frigate NVR events via MQTT, analyzes video clips using Google Gemini AI to detect specific objects (e.g., herons), and provides a real-time web dashboard for monitoring and configuration.

## ✨ Features

- **Real-time Event Processing**: Listens to Frigate events via MQTT and processes them instantly.
- **AI-Powered Analysis**: Uses Google Gemini 1.5 Flash for accurate and fast video frame analysis.
- **Dynamic Web Dashboard**: A live-updating single-page interface to view processed events and logs.
- **Web-Based Configuration**: Easily configure all settings (MQTT, Frigate, Gemini, Filters) from a web form. No SSH required.
- **Persistent JSON Config**: All your settings are saved and survive container restarts.
- **Smart Filtering**: Process only the events you care about based on camera and object labels.
- **Health Monitoring**: Includes a `/health` endpoint for container health checks.

## 🚀 Quick Installation on Proxmox VE

This script will create a new LXC container, install all dependencies, and set up the application to run as a service.

```bash
# Download the latest setup script, overwriting any old version, and run it
wget -O setup.sh [https://raw.githubusercontent.com/KiranWelisa/frigate-ai-processor/main/setup.sh](https://raw.githubusercontent.com/KiranWelisa/frigate-ai-processor/main/setup.sh)
chmod +x setup.sh
./setup.sh
````

You can pass a container ID as an argument (e.g., `./setup.sh 301`). If not provided, it defaults to `300`.

## 📋 Prerequisites

  - A **Proxmox VE** host system.
  - A running **Frigate NVR** instance accessible via HTTP API.
  - An **MQTT Broker** (e.g., Mosquitto) that Frigate is publishing events to.
  - A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/apikey).

## ⚙️ Configuration

After the installation script finishes, access the web interface at `http://<CONTAINER_IP>:5001/config` to set up:

1.  **Frigate & MQTT**: URLs, ports, and credentials.
2.  **Gemini AI**: Your API key.
3.  **Event Filters**: The specific `camera` and `object label` combinations you want to analyze.

The application will automatically restart to apply the new settings upon saving.

## 🖥️ Usage

  - **Dashboard**: `http://<CONTAINER_IP>:5001`
  - **Configuration**: `http://<CONTAINER_IP>:5001/config`

### Service Management

You can manage the application service from your Proxmox host using `pct exec`.

```bash
# (Example using CT ID 300)

# View live logs
pct exec 300 -- journalctl -u frigate-ai-processor -f

# Restart the service
pct exec 300 -- systemctl restart frigate-ai-processor

# Check the service status
pct exec 300 -- systemctl status frigate-ai-processor
```

## License

This project is licensed under the MIT License - see the `LICENSE` file for details.
