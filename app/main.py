import os
import json
import logging
import threading
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
import paho.mqtt.client as mqtt
import requests
import google.generativeai as genai

# --- Configuration ---
# The config file is stored in the app directory.
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
DEFAULT_CONFIG = {
    "frigate_url": "http://192.168.1.10:5000",
    "mqtt_broker": "192.168.1.11",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_result_topic": "frigate/analyzer/result",
    "gemini_api_key": "YOUR_GEMINI_API_KEY",
    "filters": [
        {"camera": "front_door", "label": "person"}
    ],
    "debug": False
}

def load_config():
    """Loads configuration from a JSON file, creating it if it doesn't exist."""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config = load_config()

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log_messages = [] # In-memory log for the web UI

def log_message(level, message):
    """Logs a message and adds it to the in-memory log for the UI."""
    log_level = level.upper()
    if log_level == 'DEBUG' and not config.get('debug', False):
        return
        
    log_entry = f"<span class='log-{level.lower()}'>[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{log_level}] {message}</span>"
    log_messages.insert(0, log_entry)
    if len(log_messages) > 200: # Keep the log list from growing indefinitely
        log_messages.pop()
    logging.log(getattr(logging, log_level), message)

# --- Flask Web App ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

@app.route('/')
def index():
    """Renders the main dashboard page."""
    return render_template('index.html', logs=log_messages, debug=config.get('debug', False))

@app.route('/config', methods=['GET', 'POST'])
def config_editor():
    """Handles the configuration page for viewing and updating settings."""
    if request.method == 'POST':
        global config
        # A more robust way to handle form data for lists
        new_config_data = request.form.to_dict()
        
        filters = []
        filter_cameras = request.form.getlist('camera')
        filter_labels = request.form.getlist('label')
        for i in range(len(filter_cameras)):
            if filter_cameras[i] and filter_labels[i]: # Add filter only if both fields are filled
                 filters.append({'camera': filter_cameras[i], 'label': filter_labels[i]})
        
        # Reconstruct the config object from form data
        new_config = {
            "frigate_url": new_config_data.get('frigate_url'),
            "mqtt_broker": new_config_data.get('mqtt_broker'),
            "mqtt_port": int(new_config_data.get('mqtt_port')),
            "mqtt_username": new_config_data.get('mqtt_username'),
            "mqtt_password": new_config_data.get('mqtt_password'),
            "mqtt_result_topic": new_config_data.get('mqtt_result_topic'),
            "gemini_api_key": new_config_data.get('gemini_api_key'),
            "filters": filters,
            "debug": 'debug' in new_config_data
        }

        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_config, f, indent=4)

        log_message('INFO', 'Configuration saved. Please restart the service for changes to take full effect.')
        # For a production app, a more graceful restart mechanism is recommended.
        # A simple approach is to have the systemd service restart the app on exit.
        os._exit(1)
        
    return render_template('config.html', config=config)

@app.route('/health')
def health():
    """A simple health check endpoint."""
    return "OK", 200

# --- MQTT Client Logic ---
mqtt_client = None

def on_connect(client, userdata, flags, rc):
    """Callback for when the client connects to the MQTT broker."""
    if rc == 0:
        log_message('INFO', "Connected to MQTT Broker!")
        client.subscribe("frigate/events")
    else:
        log_message('ERROR', f"Failed to connect to MQTT, return code {rc}")

def on_message(client, userdata, msg):
    """Callback for when a message is received from the MQTT broker."""
    try:
        event = json.loads(msg.payload.decode())
        # We are interested in the 'end' event type, which signifies the event is complete.
        if event.get('type') == 'end':
            # Process each event in its own thread to avoid blocking the MQTT loop
            threading.Thread(target=process_event, args=(event,)).start()
    except json.JSONDecodeError:
        log_message('ERROR', "Failed to decode MQTT message payload.")
    except Exception as e:
        log_message('ERROR', f"Error processing MQTT message: {e}")

def process_event(event):
    """Filters an event and triggers analysis if it matches."""
    event_id = event.get('id', 'unknown_event')
    camera = event.get('camera', 'unknown_camera')
    label = event.get('label', 'unknown_label')
    
    log_message('DEBUG', f"Received event: id={event_id}, camera={camera}, label={label}")

    filter_passed = any(
        f.get('camera') == camera and f.get('label') == label
        for f in config.get('filters', [])
    )
    
    log_message('DEBUG', f"Event {event_id}: Filter check passed: {filter_passed}")

    if filter_passed:
        log_message('INFO', f"Event {event_id} matched filter ({camera}/{label}). Starting analysis...")
        analyze_video(event)
    elif config.get('debug', False):
        log_message('INFO', f"Event {event_id} did not match filters.")

def analyze_video(event):
    """Downloads a video clip, extracts frames, and sends them to Gemini for analysis."""
    event_id = event['id']
    clip_path = f"/tmp/{event['camera']}-{event_id}.mp4"
    
    # 1. Download clip from Frigate API
    try:
        url = f"{config['frigate_url']}/api/events/{event_id}/clip.mp4"
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        with open(clip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        log_message('INFO', f"Event {event_id}: Clip downloaded successfully.")
    except requests.exceptions.RequestException as e:
        log_message('ERROR', f"Event {event_id}: Failed to download clip: {e}")
        return

    # 2. Extract frames using ffmpeg
    frames_dir = f"/tmp/{event_id}_frames"
    os.makedirs(frames_dir, exist_ok=True)
    try:
        subprocess.run(
            ['ffmpeg', '-i', clip_path, '-vf', 'fps=1', f'{frames_dir}/frame-%04d.jpg'],
            check=True, capture_output=True, text=True, timeout=60
        )
        log_message('INFO', f"Event {event_id}: Extracted frames from video.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log_message('ERROR', f"Event {event_id}: Failed to extract frames: {getattr(e, 'stderr', e)}")
        subprocess.run(['rm', '-rf', frames_dir, clip_path], check=False)
        return

    # 3. Analyze with Gemini
    try:
        genai.configure(api_key=config['gemini_api_key'])
        # Use a stable, recommended model
        model = genai.GenerativeModel('gemini-1.5-flash-latest')

        frame_files = sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir)])
        if not frame_files:
            log_message('WARNING', f"Event {event_id}: No frames were extracted.")
            return

        # Use a subset of frames to be efficient
        sample_frames = frame_files[::len(frame_files)//20 + 1][:20]
        log_message('DEBUG', f"Event {event_id}: Sending {len(sample_frames)} frames to Gemini.")

        # Prepare the prompt and files for the API call
        prompt = "Analyze these video frames. Is a 'Reiger' (heron) present? Respond with a JSON object containing a boolean 'Reiger' field and a 'Probability' field (0-1)."
        files_to_send = [genai.upload_file(path=f) for f in sample_frames]
        
        # Generate content with the specified JSON schema in the prompt itself
        response = model.generate_content([prompt] + files_to_send,
                                          generation_config={"response_mime_type": "application/json"})
        
        result = json.loads(response.text)
        log_message('INFO', f"Event {event_id}: Gemini analysis result: {result}")

        # 4. Publish result to MQTT
        result_payload = {
            "event_id": event_id,
            "camera": event['camera'],
            "label": event['label'],
            "reiger_detected": result.get("Reiger", False),
            "probability": result.get("Probability", 0.0),
        }
        mqtt_client.publish(config['mqtt_result_topic'], json.dumps(result_payload))
        log_message('INFO', f"Event {event_id}: Published analysis result to MQTT.")

    except Exception as e:
        log_message('ERROR', f"Event {event_id}: Gemini analysis failed: {e}")
    finally:
        # 5. Cleanup temporary files
        log_message('DEBUG', f"Event {event_id}: Cleaning up temporary files.")
        subprocess.run(['rm', '-rf', frames_dir], check=False)
        subprocess.run(['rm', '-f', clip_path], check=False)

def start_mqtt_client():
    """Initializes and starts the MQTT client loop."""
    global mqtt_client
    client_id = f"frigate-analyzer-{os.getpid()}"
    mqtt_client = mqtt.Client(client_id=client_id)
    if config.get('mqtt_username'):
        mqtt_client.username_pw_set(config['mqtt_username'], config.get('mqtt_password'))
    
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    try:
        mqtt_client.connect(config['mqtt_broker'], config['mqtt_port'], 60)
        mqtt_client.loop_forever() # This is a blocking call
    except Exception as e:
        log_message('CRITICAL', f"Could not connect to MQTT broker. The application will not work. Error: {e}")

# --- Main Application Execution ---
if __name__ == '__main__':
    # Start the MQTT client in a separate thread so it doesn't block the web server
    mqtt_thread = threading.Thread(target=start_mqtt_client, daemon=True)
    mqtt_thread.start()
    
    # Run the Flask web server
    # Use 'debug=False' for production. The reloader can interfere with threads.
    app.run(host='0.0.0.0', port=5001, debug=False)
