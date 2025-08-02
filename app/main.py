import os
import json
import logging
import threading
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt
import requests
import google.generativeai as genai
import eventlet

# Required for SocketIO async mode
eventlet.monkey_patch()

# --- Configuration ---
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
DEFAULT_CONFIG = {
    "frigate_url": "http://192.168.1.10:5000",
    "mqtt_broker": "192.168.1.11",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_events_topic": "frigate/events", # New configurable topic
    "mqtt_result_topic": "frigate/analyzer/result",
    "gemini_api_key": "YOUR_GEMINI_API_KEY",
    "filters": [
        {"camera": "front_door", "label": "person"}
    ],
    "debug": False
}

def load_config():
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
    log_level = level.upper()
    if log_level == 'DEBUG' and not config.get('debug', False):
        return
    logging.log(getattr(logging, log_level), message)
    socketio.emit('log', {'level': level.lower(), 'message': message})

# --- Web Routes & SocketIO Events ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/config', methods=['GET', 'POST'])
def config_editor():
    if request.method == 'POST':
        global config
        new_config_data = request.form.to_dict()
        filters = []
        filter_cameras = request.form.getlist('camera')
        filter_labels = request.form.getlist('label')
        for i in range(len(filter_cameras)):
            if filter_cameras[i] and filter_labels[i]:
                 filters.append({'camera': filter_cameras[i], 'label': filter_labels[i]})
        new_config = {
            "frigate_url": new_config_data.get('frigate_url'),
            "mqtt_broker": new_config_data.get('mqtt_broker'),
            "mqtt_port": int(new_config_data.get('mqtt_port')),
            "mqtt_username": new_config_data.get('mqtt_username'),
            "mqtt_password": new_config_data.get('mqtt_password'),
            "mqtt_events_topic": new_config_data.get('mqtt_events_topic'), # Save the new topic
            "mqtt_result_topic": new_config_data.get('mqtt_result_topic'),
            "gemini_api_key": new_config_data.get('gemini_api_key'),
            "filters": filters,
            "debug": 'debug' in new_config_data
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_config, f, indent=4)
        log_and_emit('info', 'Configuratie opgeslagen. Service wordt herstart...')
        os._exit(1)
    return render_template('config.html', config=config)

@app.route('/api/thumbnail/<event_id>')
def get_thumbnail(event_id):
    try:
        url = f"{config['frigate_url']}/api/events/{event_id}/thumbnail.jpg"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content, 200, {'Content-Type': 'image/jpeg'}
    except requests.RequestException as e:
        log_and_emit('error', f"Kon thumbnail voor event {event_id} niet ophalen: {e}")
        return "Not Found", 404

@socketio.on('connect')
def handle_connect():
    log_and_emit('info', 'Web client verbonden.')

# --- MQTT & Analysis Logic ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        events_topic = config.get('mqtt_events_topic', 'frigate/events')
        log_and_emit('info', f"Verbonden met MQTT Broker! Luisteren op topic: {events_topic}")
        client.subscribe(events_topic) # Use the configured topic
    else:
        log_and_emit('error', f"Kon niet verbinden met MQTT, return code {rc}")

def on_message(client, userdata, msg):
    try:
        event = json.loads(msg.payload.decode())
        # In this version, we don't check for 'type' == 'end' as the structure might be different
        # We assume any message on the topic is a valid event object
        socketio.start_background_task(target=process_event, event=event)
    except Exception as e:
        log_and_emit('error', f"Fout bij verwerken MQTT bericht: {e}")

def process_event(event):
    # Since the event structure is now different, we adapt how we get the data
    event_id = event.get('id', 'unknown_event')
    camera = event.get('camera', 'unknown_camera')
    label = event.get('label', 'unknown_label')
    
    # If the event is nested under 'after', use that structure
    if 'after' in event and isinstance(event['after'], dict):
        after_event = event['after']
        event_id = after_event.get('id', event_id)
        camera = after_event.get('camera', camera)
        label = after_event.get('label', label)

    log_and_emit('debug', f"Ontvangen event: id={event_id}, camera={camera}, label={label}")
    filter_passed = any(f.get('camera') == camera and f.get('label') == label for f in config.get('filters', []))
    log_and_emit('debug', f"Event {event_id}: Filter check geslaagd: {filter_passed}")

    if filter_passed:
        log_and_emit('info', f"Event {event_id} matched filter ({camera}/{label}). Analyse wordt gestart...")
        analyze_video(event)
    elif config.get('debug', False):
        log_and_emit('info', f"Event {event_id} matched de filters niet.")

def analyze_video(event):
    # Adapt to get the event_id from the correct place
    event_id = event.get('id')
    if 'after' in event and isinstance(event['after'], dict):
        event_id = event['after'].get('id', event_id)
    
    if not event_id:
        log_and_emit('error', "Kon geen event_id vinden in het bericht.")
        return

    clip_path = f"/tmp/{config.get('camera', 'cam')}-{event_id}.mp4"
    frames_dir = f"/tmp/{event_id}_frames"
    try:
        # Download clip
        response = requests.get(f"{config['frigate_url']}/api/events/{event_id}/clip.mp4", stream=True, timeout=60)
        response.raise_for_status()
        with open(clip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192): f.write(chunk)
        log_and_emit('info', f"Event {event_id}: Clip gedownload.")
        # Extract frames
        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(['ffmpeg','-i',clip_path,'-vf','fps=1',f'{frames_dir}/frame-%04d.jpg'],check=True,capture_output=True,text=True,timeout=60)
        log_and_emit('info', f"Event {event_id}: Frames geëxtraheerd.")
        # Analyze with Gemini
        genai.configure(api_key=config['gemini_api_key'])
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        frame_files = sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir)])
        if not frame_files:
            log_and_emit('warning', f"Event {event_id}: Geen frames geëxtraheerd.")
            return
        sample_frames = frame_files[::len(frame_files)//20 + 1][:20]
        log_and_emit('debug', f"Event {event_id}: {len(sample_frames)} frames worden naar Gemini gestuurd.")
        prompt = "Analyseer deze videoframes. Is er een 'Reiger' aanwezig? Antwoord met een JSON-object met een boolean 'Reiger' veld en een 'Probability' veld (0-1)."
        files_to_send = [genai.upload_file(path=f) for f in sample_frames]
        response = model.generate_content([prompt] + files_to_send, generation_config={"response_mime_type": "application/json"})
        result = json.loads(response.text)
        log_and_emit('info', f"Event {event_id}: Gemini analyseresultaat: {result}")
        # Publish result
        result_payload = {"event_id": event_id,"timestamp": datetime.now().isoformat(),"camera": event.get('camera'),"label": event.get('label'),"reiger_detected": result.get("Reiger", False),"probability": result.get("Probability", 0.0)}
        mqtt_client.publish(config['mqtt_result_topic'], json.dumps(result_payload))
        log_and_emit('info', f"Event {event_id}: Analyseresultaat gepubliceerd op MQTT.")
        socketio.emit('analysis_result', result_payload)
    except Exception as e:
        log_and_emit('error', f"Event {event_id}: Analyse mislukt: {e}")
    finally:
        subprocess.run(['rm', '-rf', frames_dir, clip_path], check=False)

def start_mqtt_client():
    global mqtt_client
    mqtt_client = mqtt.Client(client_id=f"frigate-analyzer-{os.getpid()}")
    if config.get('mqtt_username'):
        mqtt_client.username_pw_set(config['mqtt_username'], config.get('mqtt_password'))
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(config['mqtt_broker'], config['mqtt_port'], 60)
        mqtt_client.loop_start()
    except Exception as e:
        log_and_emit('critical', f"Kon niet verbinden met MQTT broker. Fout: {e}")

# --- Main Execution ---
if __name__ == '__main__':
    start_mqtt_client()
    log_and_emit('info', "Frigate AI Processor wordt opgestart...")
    socketio.run(app, host='0.0.0.0', port=5001)
