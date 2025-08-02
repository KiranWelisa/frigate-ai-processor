# Frigate AI Processor

An intelligent event processor that monitors Frigate NVR events via MQTT, analyzes video clips using Google Gemini AI to detect herons (Reiger), and provides a web-based dashboard for monitoring and configuration.

## Features

- 🎥 **Real-time Event Processing**: Monitors Frigate events via MQTT
- 🤖 **AI-Powered Detection**: Uses Google Gemini 2.0 Flash for heron detection
- 🌐 **Web Dashboard**: Live monitoring interface with event display
- ⚙️ **Web Configuration**: Easy configuration without command line access
- 🐛 **Debug Mode**: Detailed logging for troubleshooting
- 💾 **Persistent Config**: JSON-based configuration that survives restarts

## Quick Installation on Proxmox VE

```bash
# Download and run the setup script
wget https://raw.githubusercontent.com/KiranWelisa/frigate-ai-processor/main/setup.sh
chmod +x setup.sh
./setup.sh
```

The script will:
1. Create an LXC container with Ubuntu 24.04
2. Install all dependencies
3. Download and configure the application
4. Start the service automatically

## Prerequisites

- **Proxmox VE** host system
- **Frigate NVR** accessible via HTTP API
- **MQTT Broker** (e.g., Mosquitto)
- **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/apikey)

## Configuration

After installation, access the web interface at `http://CONTAINER_IP:5001/config` to configure:

- MQTT broker connection details
- Frigate API URL
- Google Gemini API key
- Camera and object filters

## Usage

### Web Interface
- **Dashboard**: `http://CONTAINER_IP:5001`
- **Configuration**: `http://CONTAINER_IP:5001/config`

### Service Management
```bash
# View logs
pct exec 300 -- journalctl -u frigate-ai-processor -f

# Restart service
pct exec 300 -- systemctl restart frigate-ai-processor

# Check status
pct exec 300 -- systemctl status frigate-ai-processor
```

## How It Works

1. **Event Reception**: Listens to Frigate's MQTT events
2. **Filtering**: Processes only events matching configured cameras/objects
3. **Video Download**: Retrieves video clip from Frigate API
4. **Frame Extraction**: Extracts frames for analysis
5. **AI Analysis**: Sends frames to Gemini for heron detection
6. **Result Publishing**: Publishes results back to MQTT

## MQTT Message Format

### Input (Frigate Events)
```json
{
  "type": "new",
  "after": {
    "id": "event-id",
    "camera": "Tuin",
    "label": "bird",
    "start_time": 1234567890.123
  }
}
```

### Output (Analysis Results)
```json
{
  "event_id": "event-id",
  "timestamp": "2025-01-20T10:30:00Z",
  "camera": "Tuin",
  "reiger_detected": true,
  "probability": 0.85
}
```

## License

MIT License - See LICENSE file for details