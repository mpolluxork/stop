import random
import string
import socket
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'basta-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Global State
players = {}  # sid: {'name': str, 'score': int, 'answers': {}, 'ready': bool, ...}
player_order = []  # List of SIDs
current_turn_index = 0
used_letters = set()
current_letter = ""
game_state = 'LOBBY'  # LOBBY, PLAYING, REVIEW, SCORES
categories = ["Nombre", "Apellido", "Ciudad/País", "Flor/Fruto", "Animal", "Cosa", "Color"]
settings = {
    'countdown_time': 10,
    'categories': categories.copy()
}
stop_timer = None
basta_triggered = False

# Persisted scores by player name (for reconnection)
persisted_scores = {}  # name: score

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No necesita ser real, solo para detectar la interfaz
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

server_ip = get_ip()
server_port = 5000
server_url = f"http://{server_ip}:{server_port}"

def get_current_host_sid():
    if not player_order:
        return None
    return player_order[current_turn_index % len(player_order)]

def broadcast_state():
    host_sid = get_current_host_sid()
    socketio.emit('state_update', {
        'game_state': game_state,
        'players': players,
        'player_order': player_order,
        'current_host_sid': host_sid,
        'current_letter': current_letter,
        'categories': settings['categories'],
        'used_letters': list(used_letters),
        'settings': settings,
        'server_url': server_url
    })

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('join_game')
def handle_join(data):
    global player_order, persisted_scores
    sid = request.sid
    name = data.get('name', f'Player {len(players) + 1}')
    
    # Restore score if player reconnects with same name
    restored_score = persisted_scores.get(name, 0)
    
    players[sid] = {
        'name': name,
        'score': restored_score,
        'answers': {},
        'provisional_scores': {},
        'invalidated': {}, # category: bool
        'ready': False
    }
    
    if sid not in player_order:
        player_order.append(sid)
    
    if restored_score > 0:
        print(f"Player {name} rejoined with restored score: {restored_score}")
    else:
        print(f"Player {name} joined with SID {sid}")
    broadcast_state()

@socketio.on('start_round')
def handle_start_round():
    global game_state, current_letter, used_letters, basta_triggered
    if request.sid != get_current_host_sid():
        return

    # Check if all players are ready
    if not all(p.get('ready', False) for p in players.values()):
        print("Not all players are ready")
        return

    available_letters = [l for l in string.ascii_uppercase if l not in used_letters]
    if not available_letters:
        used_letters.clear()
        available_letters = list(string.ascii_uppercase)
    
    current_letter = random.choice(available_letters)
    used_letters.add(current_letter)
    game_state = 'PLAYING'
    basta_triggered = False
    
    # Reset answers for new round
    for sid in players:
        players[sid]['answers'] = {}
        players[sid]['provisional_scores'] = {}
        players[sid]['invalidated'] = {cat: False for cat in settings['categories']}
        players[sid]['ready'] = False # Reset ready for next round? Or keep? Let's reset.
    
    print(f"Round started with letter: {current_letter}")
    broadcast_state()

@socketio.on('basta_triggered')
def handle_basta():
    global game_state, basta_triggered
    if game_state != 'PLAYING' or basta_triggered:
        return
    
    basta_triggered = True
    player_name = players.get(request.sid, {}).get('name', 'Unknown')
    print(f"BASTA triggered by {player_name}")
    
    # Notify ALL players to play the BASTA sound (including the one who triggered)
    emit('play_basta_sound', {'triggered_by': player_name}, broadcast=True)
    emit('basta_countdown', {'seconds': settings['countdown_time']}, broadcast=True)
    
    def end_writing():
        try:
            global game_state
            socketio.sleep(settings['countdown_time'])
            game_state = 'REVIEW'
            socketio.emit('stop_writing')
            # Wait for submissions
            socketio.sleep(3)
            calculate_provisional_scores()
            broadcast_state()
        except Exception as e:
            print(f"Error in end_writing: {e}")

    socketio.start_background_task(end_writing)

@socketio.on('submit_answers')
def handle_submission(data):
    sid = request.sid
    if sid in players:
        players[sid]['answers'] = data.get('answers', {})
        print(f"Answers received from {players[sid]['name']}")

import unicodedata

def normalize_word(word):
    if not word:
        return ""
    # Remove accents and convert to lowercase
    return "".join(
        c for c in unicodedata.normalize('NFD', word.strip().lower())
        if unicodedata.category(c) != 'Mn'
    )

def calculate_provisional_scores():
    # Group answers by category
    category_map = {cat: {} for cat in settings['categories']} # cat: {normalized_word: [sids]}
    
    for sid, p_data in players.items():
        ans = p_data.get('answers', {})
        for cat in settings['categories']:
            word = normalize_word(ans.get(cat, ""))
            if word and len(word) > 1:
                if word not in category_map[cat]:
                    category_map[cat][word] = []
                category_map[cat][word].append(sid)

    # Assign scores - divide 100 points among all players with same word
    for sid, p_data in players.items():
        ans = p_data.get('answers', {})
        scores = {}
        for cat in settings['categories']:
            word = normalize_word(ans.get(cat, ""))
            # Check length and starting letter
            if not word or len(word) <= 1 or not word.startswith(normalize_word(current_letter)):
                scores[cat] = 0
            else:
                # Divide 100 points by number of players with same word
                num_players_with_word = len(category_map[cat][word])
                scores[cat] = int(100 / num_players_with_word)
        players[sid]['provisional_scores'] = scores

@socketio.on('toggle_invalidation')
def handle_invalidation(data):
    if request.sid != get_current_host_sid():
        return
    
    target_sid = data.get('sid')
    category = data.get('category')
    
    if target_sid in players and category in settings['categories']:
        players[target_sid]['invalidated'][category] = not players[target_sid]['invalidated'].get(category, False)
        broadcast_state()

@socketio.on('confirm_round')
def handle_confirm_round():
    global game_state, current_turn_index, persisted_scores
    if request.sid != get_current_host_sid():
        return
    
    # Finalize scores
    for sid in players:
        p_data = players[sid]
        round_total = 0
        for cat in settings['categories']:
            if not p_data['invalidated'].get(cat, False):
                round_total += p_data['provisional_scores'].get(cat, 0)
        players[sid]['score'] += round_total
        # Update persisted scores for reconnection
        persisted_scores[players[sid]['name']] = players[sid]['score']
    
    current_turn_index = (current_turn_index + 1) % len(player_order)
    game_state = 'SCORES'
    broadcast_state()

@socketio.on('next_round_lobby')
def handle_next_round():
    global game_state
    if request.sid != get_current_host_sid():
        return
    game_state = 'LOBBY'
    broadcast_state()

@socketio.on('toggle_ready')
def handle_toggle_ready():
    sid = request.sid
    if sid in players:
        players[sid]['ready'] = not players[sid].get('ready', False)
        print(f"Player {players[sid]['name']} ready: {players[sid]['ready']}")
        broadcast_state()

@socketio.on('update_settings')
def handle_update_settings(data):
    global settings
    if request.sid != get_current_host_sid():
        return
    
    new_categories = data.get('categories')
    new_countdown = data.get('countdown_time')
    
    if new_categories is not None:
        settings['categories'] = new_categories
    if new_countdown is not None:
        try:
            settings['countdown_time'] = int(new_countdown)
        except ValueError:
            pass
            
    print(f"Settings updated: {settings}")
    broadcast_state()

@socketio.on('disconnect')
def handle_disconnect():
    global persisted_scores
    sid = request.sid
    if sid in players:
        name = players[sid]['name']
        score = players[sid]['score']
        # Persist the score for potential reconnection
        persisted_scores[name] = score
        del players[sid]
        if sid in player_order:
            player_order.remove(sid)
        print(f"Player {name} disconnected. Score {score} saved for reconnection.")
        broadcast_state()

if __name__ == '__main__':
    print(f"\n --- JUEGO LISTO ---")
    print(f" Escanea para entrar o escribe: {server_url}")
    print(f" -------------------\n")
    
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(server_url)
        qr.print_ascii()
    except ImportError:
        print("Tip: Instala 'qrcode' para ver el QR en la terminal (pip install qrcode)")

    socketio.run(app, host='0.0.0.0', port=server_port, debug=True)
