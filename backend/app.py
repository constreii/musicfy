from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from ytmusicapi import YTMusic
from pathlib import Path
import json, uuid, requests
from datetime import datetime

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

# ============================================
# SETUP
# ============================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
PLAYLISTS_FILE = DATA_DIR / 'playlists.json'
OAUTH_FILE = BASE_DIR / 'oauth.json'

# ============================================
# YT MUSIC INSTANCE
# ============================================
yt = None
is_logged_in = False

def init_ytm():
    global yt, is_logged_in
    if OAUTH_FILE.exists():
        try:
            yt = YTMusic(str(OAUTH_FILE))
            is_logged_in = True
            print('✅ YT Music: LOGGED IN (oauth.json)')
            return
        except Exception as e:
            print(f'⚠️ OAuth error: {e}')
    yt = YTMusic()
    is_logged_in = False
    print('👤 YT Music: GUEST MODE')

init_ytm()

# ============================================
# PLAYLIST HELPERS
# ============================================
def load_playlists():
    if PLAYLISTS_FILE.exists():
        try:
            return json.loads(PLAYLISTS_FILE.read_text(encoding='utf-8'))
        except: return []
    return []

def save_playlists(data):
    PLAYLISTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

# ============================================
# ROUTES: FRONTEND
# ============================================
@app.route('/')
def index():
    return send_from_directory('../frontend', 'index.html')

# ============================================
# ROUTES: SEARCH
# ============================================
@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'Query kosong'}), 400

    try:
        results = yt.search(q, filter='songs', limit=30)
        songs = []

        for r in results:
            if not r.get('videoId'):
                continue
            thumbs = r.get('thumbnails') or []
            thumb = thumbs[-1]['url'] if thumbs else ''
            artists = r.get('artists') or []
            artist_name = ', '.join(a.get('name', '') for a in artists) or 'Unknown Artist'
            album = ''
            if r.get('album') and r['album'].get('name'):
                album = r['album']['name']

            songs.append({
                'videoId': r['videoId'],
                'title': r.get('title', 'Unknown'),
                'artist': artist_name,
                'album': album,
                'duration': r.get('duration', '0:00'),
                'thumbnail': thumb,
            })

        return jsonify({'songs': songs, 'count': len(songs)})
    except Exception as e:
        print(f'❌ Search error: {e}')
        return jsonify({'error': str(e)}), 500

# ============================================
# ROUTES: LYRICS (proxy ke lrclib.net)
# ============================================
@app.route('/api/lyrics')
def lyrics():
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    duration = request.args.get('duration', '').strip()

    if not title:
        return jsonify({'error': 'Title wajib'}), 400

    try:
        # Pakai endpoint search (lebih fleksibel)
        query = f'{title} {artist}'.strip()
        res = requests.get(
            'https://lrclib.net/api/search',
            params={'q': query},
            timeout=8,
            headers={'User-Agent': 'Musicfy/1.0'}
        )
        res.raise_for_status()
        results = res.json()

        if not results:
            return jsonify({'found': False, 'message': 'Lirik tidak ditemukan'})

        # Cari yang paling cocok
        best = results[0]

        # Coba exact match dulu pakai /api/get
        if artist:
            try:
                exact = requests.get(
                    'https://lrclib.net/api/get',
                    params={
                        'track_name': title,
                        'artist_name': artist.split(',')[0].strip()
                    },
                    timeout=5,
                    headers={'User-Agent': 'Musicfy/1.0'}
                )
                if exact.status_code == 200:
                    best = exact.json()
            except: pass

        if best.get('instrumental'):
            return jsonify({
                'found': True,
                'instrumental': True,
                'message': 'Lagu instrumental — tidak ada lirik'
            })

        return jsonify({
            'found': True,
            'title': best.get('trackName', title),
            'artist': best.get('artistName', artist),
            'album': best.get('albumName', ''),
            'syncedLyrics': best.get('syncedLyrics', ''),
            'plainLyrics': best.get('plainLyrics', ''),
        })
    except Exception as e:
        print(f'❌ Lyrics error: {e}')
        return jsonify({'found': False, 'message': f'Error: {str(e)}'})

# ============================================
# ROUTES: PLAYLISTS (CRUD)
# ============================================
@app.route('/api/playlists', methods=['GET'])
def get_playlists():
    return jsonify(load_playlists())

@app.route('/api/playlists', methods=['POST'])
def create_playlist():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nama wajib diisi'}), 400

    playlists = load_playlists()
    new_pl = {
        'id': str(uuid.uuid4())[:8],
        'name': name,
        'description': data.get('description', ''),
        'created': datetime.now().isoformat(),
        'songs': []
    }
    playlists.append(new_pl)
    save_playlists(playlists)
    return jsonify(new_pl), 201

@app.route('/api/playlists/<pid>', methods=['GET'])
def get_playlist(pid):
    for pl in load_playlists():
        if pl['id'] == pid:
            return jsonify(pl)
    return jsonify({'error': 'Tidak ditemukan'}), 404

@app.route('/api/playlists/<pid>', methods=['DELETE'])
def delete_playlist(pid):
    playlists = load_playlists()
    new_list = [p for p in playlists if p['id'] != pid]
    if len(new_list) == len(playlists):
        return jsonify({'error': 'Tidak ditemukan'}), 404
    save_playlists(new_list)
    return jsonify({'success': True})

@app.route('/api/playlists/<pid>/songs', methods=['POST'])
def add_song(pid):
    data = request.get_json() or {}
    if not data.get('videoId'):
        return jsonify({'error': 'videoId wajib'}), 400

    playlists = load_playlists()
    for pl in playlists:
        if pl['id'] == pid:
            # Cek duplikat
            if any(s['videoId'] == data['videoId'] for s in pl['songs']):
                return jsonify({'error': 'Lagu sudah ada di playlist'}), 409
            pl['songs'].append({
                'videoId': data['videoId'],
                'title': data.get('title', ''),
                'artist': data.get('artist', ''),
                'duration': data.get('duration', ''),
                'thumbnail': data.get('thumbnail', ''),
            })
            save_playlists(playlists)
            return jsonify(pl)
    return jsonify({'error': 'Playlist tidak ditemukan'}), 404

@app.route('/api/playlists/<pid>/songs/<vid>', methods=['DELETE'])
def remove_song(pid, vid):
    playlists = load_playlists()
    for pl in playlists:
        if pl['id'] == pid:
            pl['songs'] = [s for s in pl['songs'] if s['videoId'] != vid]
            save_playlists(playlists)
            return jsonify(pl)
    return jsonify({'error': 'Tidak ditemukan'}), 404

# ============================================
# ROUTES: AUTH (info login)
# ============================================
@app.route('/api/auth/status')
def auth_status():
    info = {'logged_in': is_logged_in}
    if is_logged_in:
        try:
            # Ambil info user kalau bisa
            account = yt.get_account_info()
            info['account'] = account
        except: pass
    return jsonify(info)

# ============================================
# RUN
# ============================================
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
