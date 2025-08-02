import os
import json
import time
import logging
import threading
import queue
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from PIL import Image
import cv2
import numpy as np
from google import genai
from google.genai.types import Tool, GenerateContentConfig, GenerationConfig
import tempfile

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/opt/frigate-ai-processor/logs/processor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Load configuration
CONFIG_FILE = '/opt/frigate-ai-processor/config/config.json'

def load_config():
    default_config = {
        "mqtt": {
            "broker": "192.168.2.76",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "frigate-ai-processor",
            "topics": {
                "events": "frigate/events",
                "results": "frigate/ai/results"
            }
        },
        "frigate": {
            "api_url": "http://192.168.2.72:5000",
            "api_key": ""
        },
        "gemini": {
            "api_key": "",
            "model": "gemini-2.0-flash-exp",
            "temperature": 0,
            "max_tokens": 1000,
            "frames_to_extract": 20
        },
        "filters": {
            "cameras": ["Tuin"],
            "objects": ["bird"]
        },
        "debug_mode": False
    }
    
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    else:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=2)
        return default_config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

# Global variables
config = load_config()
mqtt_client = None
gemini_client = None
event_queue = queue.Queue()
debug_mode = config.get('debug_mode', False)

# Initialize Gemini client
def init_gemini():
    global gemini_client
    if config['gemini']['api_key']:
        try:
            gemini_client = genai.Client(api_key=config['gemini']['api_key'])
            logger.info("Gemini client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            gemini_client = None

# MQTT callbacks
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe(config['mqtt']['topics']['events'])
        socketio.emit('log', {'message': 'Connected to MQTT broker', 'level': 'info'})
    else:
        logger.error(f"Failed to connect to MQTT broker: {rc}")
        socketio.emit('log', {'message': f'Failed to connect to MQTT broker: {rc}', 'level': 'error'})

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        event_queue.put(payload)
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")

def on_disconnect(client, userdata, rc):
    logger.warning("Disconnected from MQTT broker")
    socketio.emit('log', {'message': 'Disconnected from MQTT broker', 'level': 'warning'})

# Initialize MQTT client
def init_mqtt():
    global mqtt_client
    mqtt_client = mqtt.Client(client_id=config['mqtt']['client_id'])
    
    if config['mqtt']['username']:
        mqtt_client.username_pw_set(config['mqtt']['username'], config['mqtt']['password'])
    
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.on_disconnect = on_disconnect
    
    try:
        mqtt_client.connect(config['mqtt']['broker'], config['mqtt']['port'], 60)
        mqtt_client.loop_start()
    except Exception as e:
        logger.error(f"Failed to connect to MQTT broker: {e}")

# Process events
def process_events():
    while True:
        try:
            event = event_queue.get(timeout=1)
            process_single_event(event)
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Error processing event: {e}")

def process_single_event(event):
    # Log all events in debug mode
    if debug_mode:
        socketio.emit('debug_event', {
            'event': event,
            'timestamp': datetime.now().isoformat()
        })
    
    # Check if event matches filters
    if event.get('type') == 'new':
        after = event.get('after', {})
        camera = after.get('camera')
        label = after.get('label')
        
        # Check filters
        camera_match = camera in config['filters']['cameras']
        object_match = label in config['filters']['objects']
        
        if debug_mode:
            socketio.emit('log', {
                'message': f"Event from {camera} with {label} - Camera match: {camera_match}, Object match: {object_match}",
                'level': 'debug'
            })
        
        if camera_match and object_match:
            logger.info(f"Processing event: {after.get('id')} - {camera}/{label}")
            socketio.emit('log', {
                'message': f"Processing event: {after.get('id')} - {camera}/{label}",
                'level': 'info'
            })
            
            # Process with Gemini
            process_with_gemini(event)

def download_video_clip(event_id):
    """Download video clip from Frigate"""
    try:
        url = f"{config['frigate']['api_url']}/api/events/{event_id}/clip.mp4"
        headers = {}
        if config['frigate']['api_key']:
            headers['Authorization'] = f"Bearer {config['frigate']['api_key']}"
        
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()
        
        # Save to temporary file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
        for chunk in response.iter_content(chunk_size=8192):
            temp_file.write(chunk)
        temp_file.close()
        
        return temp_file.name
    except Exception as e:
        logger.error(f"Failed to download video clip: {e}")
        return None

def extract_frames(video_path, num_frames=20):
    """Extract frames from video"""
    frames = []
    try:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        if total_frames == 0:
            return frames
        
        indices = np.linspace(0, total_frames - 1, num_frames, dtype=int)
        
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                # Convert BGR to RGB
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(frame_rgb)
        
        cap.release()
    except Exception as e:
        logger.error(f"Failed to extract frames: {e}")
    
    return frames

def analyze_with_gemini(frames):
    """Analyze frames with Gemini AI"""
    if not gemini_client:
        logger.error("Gemini client not initialized")
        return None
    
    try:
        # Convert frames to PIL images
        pil_images = [Image.fromarray(frame) for frame in frames]
        
        # Create prompt
        prompt = "Analyze these video frames. Is there a Reiger (heron) visible? Respond with JSON only."
        
        # Prepare content
        content = [prompt]
        for img in pil_images:
            content.append(img)
        
        # Generate response with structured output
        response = gemini_client.models.generate_content(
            model=config['gemini']['model'],
            contents=content,
            config=GenerateContentConfig(
                temperature=config['gemini']['temperature'],
                max_output_tokens=config['gemini']['max_tokens'],
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": {
                        "Reiger": {"type": "boolean"},
                        "Probability": {"type": "number"}
                    },
                    "required": ["Probability"]
                }
            )
        )
        
        # Parse response
        result = json.loads(response.text)
        return result
    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return None

def process_with_gemini(event):
    """Main processing function"""
    event_id = event['after']['id']
    
    # Download video
    socketio.emit('log', {'message': f'Downloading video for event {event_id}', 'level': 'info'})
    video_path = download_video_clip(event_id)
    
    if not video_path:
        socketio.emit('log', {'message': f'Failed to download video for event {event_id}', 'level': 'error'})
        return
    
    try:
        # Extract frames
        socketio.emit('log', {'message': f'Extracting frames from video', 'level': 'info'})
        frames = extract_frames(video_path, config['gemini']['frames_to_extract'])
        
        if not frames:
            socketio.emit('log', {'message': f'No frames extracted', 'level': 'error'})
            return
        
        # Analyze with Gemini
        socketio.emit('log', {'message': f'Analyzing with Gemini AI', 'level': 'info'})
        result = analyze_with_gemini(frames)
        
        if result:
            # Publish result to MQTT
            mqtt_result = {
                'event_id': event_id,
                'timestamp': datetime.now().isoformat(),
                'camera': event['after']['camera'],
                'reiger_detected': result.get('Reiger', False),
                'probability': result.get('Probability', 0.0)
            }
            
            mqtt_client.publish(
                config['mqtt']['topics']['results'],
                json.dumps(mqtt_result)
            )
            
            # Emit to web interface
            socketio.emit('analysis_result', mqtt_result)
            socketio.emit('log', {
                'message': f'Analysis complete - Reiger: {mqtt_result["reiger_detected"]}, Probability: {mqtt_result["probability"]:.2f}',
                'level': 'success'
            })
        
    finally:
        # Cleanup
        if os.path.exists(video_path):
            os.unlink(video_path)

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/config')
def config_page():
    return render_template('config.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(config)

@app.route('/api/config', methods=['POST'])
def update_config():
    global config, debug_mode
    try:
        new_config = request.json
        save_config(new_config)
        config = new_config
        debug_mode = config.get('debug_mode', False)
        
        # Restart services with new config
        restart_services()
        
        return jsonify({'status': 'success', 'message': 'Configuration updated successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'mqtt_connected': mqtt_client.is_connected() if mqtt_client else False,
        'gemini_initialized': gemini_client is not None
    })

@app.route('/api/thumbnail/<event_id>')
def get_thumbnail(event_id):
    try:
        url = f"{config['frigate']['api_url']}/api/events/{event_id}/thumbnail.jpg"
        headers = {}
        if config['frigate']['api_key']:
            headers['Authorization'] = f"Bearer {config['frigate']['api_key']}"
        
        response = requests.get(url, headers=headers)
        return response.content, 200, {'Content-Type': 'image/jpeg'}
    except:
        return '', 404

# WebSocket events
@socketio.on('connect')
def handle_connect():
    emit('connected', {'data': 'Connected to server'})

@socketio.on('toggle_debug')
def handle_debug_toggle(data):
    global debug_mode
    debug_mode = data.get('enabled', False)
    config['debug_mode'] = debug_mode
    save_config(config)
    emit('debug_mode_changed', {'enabled': debug_mode})

def restart_services():
    """Restart MQTT and Gemini services with new config"""
    global mqtt_client
    
    # Restart MQTT
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    init_mqtt()
    
    # Restart Gemini
    init_gemini()

# Main startup
if __name__ == '__main__':
    # Initialize services
    init_gemini()
    init_mqtt()
    
    # Start event processor thread
    processor_thread = threading.Thread(target=process_events, daemon=True)
    processor_thread.start()
    
    # Start Flask app
    socketio.run(app, host='0.0.0.0', port=5001, debug=False)