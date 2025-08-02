import eventlet
# monkey_patch() must be called before any other modules are imported.
eventlet.monkey_patch()

import os
import json
import logging
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt
import requests
import google.generativeai as genai

# --- Configuration ---
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
DEFAULT_CONFIG = {
    "frigate_url": "http://192.168.1.10:5000",
    "mqtt_broker": "192.168.1.11",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_events_topic": "frigate/#",
    "mqtt_result_topic": "frigate/analyzer/result",
    "gemini_api_key": "YOUR_GEMINI_API_KEY",
    "filters": [
        {"camera": "front_door", "label": "person"}
    ],
    "debug": False
}

def load_config():
    """Loads configuration from JSON file, creating it if it doesn't exist."""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config = load_config()

# --- Logging & Flask App Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
socketio = SocketIO(app, async_mode='eventlet')

def log_and_emit(level, message):
    """Logs a message and emits it to the web client via Socket.IO."""
    log_level = level.upper()
    if log_level == 'DEBUG' and not config.get('debug', False):
        return
    logging.log(getattr(logging, log_level), message)
    socketio.emit('log', {'level': level.lower(), 'message': f"[{log_level}] {message}"})

# --- Web Routes & SocketIO Events ---
@app.route('/')
def index():
    """Serves the main dashboard page."""
    return render_template('index.html')

@app.route('/config', methods=['GET', 'POST'])
def config_editor():
    """Serves the configuration editor page and handles config updates."""
    if request.method == 'POST':
        new_config_data = request.form.to_dict(flat=False)
        filters = []
        if 'camera' in new_config_data and 'label' in new_config_data:
            for i in range(len(new_config_data['camera'])):
                if new_config_data['camera'][i] and new_config_data['label'][i]:
                    filters.append({
                        'camera': new_config_data['camera'][i],
                        'label': new_config_data['label'][i]
                    })
        new_config = {
            "frigate_url": new_config_data.get('frigate_url', [''])[0],
            "mqtt_broker": new_config_data.get('mqtt_broker', [''])[0],
            "mqtt_port": int(new_config_data.get('mqtt_port', [1883])[0]),
            "mqtt_username": new_config_data.get('mqtt_username', [''])[0],
            "mqtt_password": new_config_data.get('mqtt_password', [''])[0],
            "mqtt_events_topic": new_config_data.get('mqtt_events_topic', ['frigate/#'])[0],
            "mqtt_result_topic": new_config_data.get('mqtt_result_topic', ['frigate/analyzer/result'])[0],
            "gemini_api_key": new_config_data.get('gemini_api_key', [''])[0],
            "filters": filters,
            "debug": 'debug' in request.form
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_config, f, indent=4)
        log_and_emit('info', 'Configuration saved. Restarting service to apply changes...')
        socketio.sleep(1)
        os._exit(1)
    return render_template('config.html', config=load_config())


@app.route('/health')
def health_check():
    """Provides a simple health check endpoint."""
    return jsonify({"status": "ok"}), 200

@app.route('/api/thumbnail/<event_id>')
def get_thumbnail(event_id):
    """Proxies thumbnail requests from the web UI to Frigate."""
    try:
        base_url = config['frigate_url'].rstrip('/')
        url = f"{base_url}/api/events/{event_id}/thumbnail.jpg"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content, 200, {'Content-Type': 'image/jpeg'}
    except requests.RequestException as e:
        log_and_emit('error', f"Could not fetch thumbnail for event {event_id}: {e}")
        return "Not Found", 404

@socketio.on('connect')
def handle_connect():
    """Logs when a new web client connects."""
    socketio.emit('status_update', {'type': 'mqtt', 'status': 'Connected' if mqtt_client.is_connected() else 'Disconnected'})
    socketio.emit('status_update', {'type': 'gemini', 'status': 'Initialized' if config.get('gemini_api_key') and config.get('gemini_api_key') != 'YOUR_GEMINI_API_KEY' else 'Not Initialized'})
    log_and_emit('info', 'Web client connected.')


# --- MQTT & Analysis Logic ---

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        events_topic = config.get('mqtt_events_topic', 'frigate/#')
        log_and_emit('info', f"Connected to MQTT Broker! Subscribing to topic: {events_topic}")
        client.subscribe(events_topic)
        socketio.emit('status_update', {'type': 'mqtt', 'status': 'Connected'})
    else:
        log_and_emit('error', f"Failed to connect to MQTT, return code {rc}")
        socketio.emit('status_update', {'type': 'mqtt', 'status': 'Disconnected'})

def on_message(client, userdata, msg):
    socketio.start_background_task(handle_mqtt_message, msg)

def handle_mqtt_message(msg):
    try:
        payload_str = msg.payload.decode('utf-8')
        event = json.loads(payload_str)
        if not isinstance(event, dict):
            log_and_emit('debug', f"Ignoring non-dict message on topic '{msg.topic}'.")
            return
        process_event_json(event)
    except (UnicodeDecodeError, json.JSONDecodeError):
        log_and_emit('debug', f"Message on topic '{msg.topic}' ignored (not valid JSON).")
    except Exception as e:
        log_and_emit('error', f"Unexpected error processing MQTT message on topic {msg.topic}: {e}")

def process_event_json(event):
    if event.get('type') != 'end':
        return

    event_details = event.get('after')
    if not isinstance(event_details, dict):
        return

    event_id = event_details.get('id')
    camera = event_details.get('camera', 'unknown_camera')
    label = event_details.get('label', 'unknown_label')

    if not event_id:
        return

    log_and_emit('info', f"New event received: ID='{event_id}', Camera='{camera}', Label='{label}'")
    
    filter_passed = any(f.get('camera') == camera and f.get('label') == label for f in config.get('filters', []))

    if filter_passed:
        log_and_emit('info', f"✅ Event {event_id} matched filter. Starting analysis.")
        socketio.start_background_task(analyze_video_clip, event_details)
    else:
        log_and_emit('info', f"❌ Event {event_id} did not match any filters.")
        result_payload = {
            "event_id": event_id,
            "timestamp": datetime.now().isoformat(),
            "camera": camera,
            "label": label,
            "status": "Filtered"
        }
        socketio.emit('analysis_result', result_payload)

def analyze_video_clip(event_details):
    event_id = event_details['id']
    clip_path = f"/tmp/{event_id}.mp4"
    frames_dir = f"/tmp/{event_id}_frames"
    
    socketio.emit('analysis_result', {"event_id": event_id, "status": "Analyzing..."})
    
    try:
        base_url = config['frigate_url'].rstrip('/')
        clip_url = f"{base_url}/api/events/{event_id}/clip.mp4"
        response = requests.get(clip_url, timeout=60)
        response.raise_for_status()
        with open(clip_path, 'wb') as f: f.write(response.content)
        log_and_emit('info', f"Event {event_id}: Clip downloaded.")

        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(['ffmpeg','-i',clip_path,'-vf','fps=1',f'{frames_dir}/frame-%04d.jpg', '-v', 'error'], check=True, timeout=60)
        log_and_emit('info', f"Event {event_id}: Frames extracted.")

        genai.configure(api_key=config['gemini_api_key'])
        json_schema = {"type": "object", "properties": {"Reiger": {"type": "boolean"}, "Probability": {"type": "number"}}, "required": ["Reiger", "Probability"]}
        generation_config = genai.GenerationConfig(response_mime_type="application/json", response_schema=genai.protos.Schema.from_dict(json_schema), temperature=0.0)
        model = genai.GenerativeModel('gemini-1.5-flash-latest')

        frame_files = sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir)])
        if not frame_files:
            log_and_emit('warning', f"Event {event_id}: No frames were extracted.")
            return

        sample_frames = frame_files[::len(frame_files)//20 + 1][:20]
        prompt = "Analyseer deze videoframes. Is er een 'Reiger' (heron) aanwezig? Geef alleen een JSON-object terug volgens het opgegeven schema."
        files_to_send = [genai.upload_file(path=f) for f in sample_frames]
        
        response = model.generate_content([prompt] + files_to_send, generation_config=generation_config)
        result = json.loads(response.text)
        
        log_and_emit('info', f"✨ Event {event_id}: Gemini analysis complete. Result: {json.dumps(result)}")
        
        reiger_detected = result.get("Reiger", False)
        probability = result.get("Probability", 0.0)

        result_payload = {
            "event_id": event_id, "timestamp": datetime.now().isoformat(), "camera": event_details['camera'],
            "label": event_details['label'], "reiger_detected": reiger_detected, "probability": probability,
            "status": "Analyzed"
        }
        
        mqtt_client.publish(config['mqtt_result_topic'], json.dumps(result_payload))
        log_and_emit('info', f"Event {event_id}: Analysis result published to MQTT.")
        socketio.emit('analysis_result', result_payload)

    except Exception as e:
        log_and_emit('error', f"Event {event_id}: A failure occurred during analysis: {e}")
        socketio.emit('analysis_result', {"event_id": event_id, "status": "Failed", "error": str(e)})
    finally:
        subprocess.run(['rm', '-rf', frames_dir, clip_path], check=False)

def start_mqtt_client():
    global mqtt_client
    client_id = f"frigate-ai-processor-{os.getpid()}"
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if config.get('mqtt_username'):
        mqtt_client.username_pw_set(config['mqtt_username'], config.get('mqtt_password'))
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(config['mqtt_broker'], config['mqtt_port'], 60)
        mqtt_client.loop_start()
    except Exception as e:
        log_and_emit('critical', f"Could not connect to MQTT broker: {e}")

# --- Main Execution ---
if __name__ == '__main__':
    log_and_emit('info', "Starting Frigate AI Processor...")
    start_mqtt_client()
    socketio.emit('status_update', {'type': 'gemini', 'status': 'Initialized' if config.get('gemini_api_key') and config.get('gemini_api_key') != 'YOUR_GEMINI_API_KEY' else 'Not Initialized'})
    socketio.run(app, host='0.0.0.0', port=5001, use_reloader=False)
