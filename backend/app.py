from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from ytmusicapi import YTMusic
from pathlib import Path
import json, uuid, requests, subprocess, os, re
from datetime import datetime
import yt_dlp

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

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
# YT-DLP CONFIG
# ============================================
YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'skip_download': True,
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'extract_flat': False,
    'nocheckcertificate': True,
    # Pakai client alternatif biar gak sering error
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
            'skip': ['hls', 'dash']
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    },
}

# Cache hasil extract (video_id → {url, expires})
# Biar gak extract tiap request
_audio_cache = {}
CACHE_TTL_SECONDS = 3600  # 1 jam


# ============================================
# ROUTES: STATIC FRONTEND
# ============================================
@app.route('/')
def index():
    return send_from_directory('../frontend', 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('../frontend', 'manifest.json')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('../frontend', 'sw.js')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/service-worker.js')
def service_worker_alt():
    # Alias buat backward compat
    response = send_from_directory('../frontend', 'sw.js')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory('../frontend/icons', filename)


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
# ROUTES: STREAM AUDIO (buat background playback!)
# ============================================
@app.route('/api/stream/<video_id>')
def stream_audio(video_id):
    """
    Stream audio dari YouTube pakai yt-dlp.
    Redirect ke URL audio langsung yang bisa diputar <audio> HTML5.
    """
    import time
    now = time.time()

    # Cek cache dulu
    cached = _audio_cache.get(video_id)
    if cached and cached['expires'] > now:
        return _proxy_or_redirect(cached['url'], cached.get('mime', 'audio/mp4'))

    try:
        # Extract audio URL pakai yt-dlp
        with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
            info = ydl.extract_info(
                f'https://www.youtube.com/watch?v={video_id}',
                download=False
            )

            audio_url = info.get('url')
            if not audio_url:
                # Coba ambil dari formats
                formats = info.get('formats', [])
                # Prioritas: m4a → webm → apapun
                for fmt in reversed(formats):
                    if fmt.get('acodec') != 'none' and fmt.get('url'):
                        audio_url = fmt['url']
                        break

            if not audio_url:
                return jsonify({'error': 'Audio URL tidak ditemukan'}), 404

            ext = info.get('ext', 'm4a')
            mime_map = {
                'm4a': 'audio/mp4',
                'webm': 'audio/webm',
                'mp3': 'audio/mpeg',
                'opus': 'audio/ogg',
            }
            mime = mime_map.get(ext, 'audio/mp4')

            # Simpan cache
            _audio_cache[video_id] = {
                'url': audio_url,
                'mime': mime,
                'expires': now + CACHE_TTL_SECONDS
            }

            # Redirect — browser bakal fetch audio dari URL ini
            from flask import redirect
            resp = redirect(audio_url, code=302)
            resp.headers['Cache-Control'] = 'public, max-age=3600'
            return resp

    except Exception as e:
        print(f'❌ Stream error [{video_id}]: {e}')
        return jsonify({'error': f'Gagal stream: {str(e)}'}), 500


def _proxy_or_redirect(url, mime):
    """Kalau cache hit, redirect aja."""
    from flask import redirect
    resp = redirect(url, code=302)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ============================================
# ROUTES: LYRICS (proxy ke lrclib.net)
# ============================================
@app.route('/api/lyrics')
def lyrics():
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()

    if not title:
        return jsonify({'error': 'Title wajib'}), 400

    try:
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

        best = results[0]

        # Coba exact match dulu
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
            except:
                pass

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
def load_playlists():
    if PLAYLISTS_FILE.exists():
        try:
            return json.loads(PLAYLISTS_FILE.read_text(encoding='utf-8'))
        except:
            return []
    return []

def save_playlists(data):
    PLAYLISTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

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
# ROUTES: AUTH STATUS
# ============================================
@app.route('/api/auth/status')
def auth_status():
    info = {'logged_in': is_logged_in}
    if is_logged_in:
        try:
            account = yt.get_account_info()
            info['account'] = account
        except:
            pass
    return jsonify(info)


# ============================================
# HEALTH CHECK
# ============================================
@app.route('/api/health')
def health():
    import yt_dlp as ytdlp_module
    return jsonify({
        'status': 'ok',
        'ytmusic': 'logged_in' if is_logged_in else 'guest',
        'yt_dlp_version': ytdlp_module.version.__version__,
        'cached_audio': len(_audio_cache),
        'playlists_count': len(load_playlists()),
    })


# ============================================
# RUN
# ============================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('\n' + '=' * 55)
    print('🎵 Musicfy Backend — HTML5 Audio Edition')
    print('=' * 55)
    print(f'🌐 Server     : http://localhost:{port}')
    print(f'📁 Data       : {DATA_DIR}')
    print(f'🔐 YT Music   : {"LOGGED IN" if is_logged_in else "GUEST MODE"}')
    print(f'🎧 yt-dlp     : v{yt_dlp.version.__version__}')
    print('=' * 55 + '\n')
    app.run(host='0.0.0.0', port=port, debug=False)
