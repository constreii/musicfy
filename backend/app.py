from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, redirect
from flask_cors import CORS
from ytmusicapi import YTMusic
from pathlib import Path
import json, uuid, requests, os, time
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
# MUSIC API (BhariyaMusic) CONFIG
# ============================================
MUSICAPI_BASE = 'https://bhindi1.ddns.net/music/api'
MUSICAPI_TIMEOUT = 25  # API ini kadang lambat
MUSICAPI_ENABLED = True  # Toggle on/off kalau lagi down

# Cache song_id → audio_url
_musicapi_cache = {}       # song_id → {audio_url, title, expires}
_musicapi_prepare_cache = {}  # videoId → {song_id, expires}
CACHE_TTL = 3600           # 1 jam

# ============================================
# YT-DLP CONFIG (Fallback)
# ============================================
YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'skip_download': True,
    'format': 'bestaudio/best',
    'extract_flat': False,
    'nocheckcertificate': True,
    'geo_bypass': True,
    'extractor_args': {
        'youtube': {
            'player_client': ['android_vr', 'ios', 'web_safari'],
            'skip': ['hls', 'dash']
        }
    },
    'http_headers': {
        'User-Agent': 'com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip',
        'Accept-Language': 'en-US,en;q=0.9',
    },
    'socket_timeout': 15,
}

# Cache yt-dlp
_ytdlp_cache = {}  # videoId → {url, mime, expires}

# ============================================
# YT MUSIC INSTANCE (buat search doang)
# ============================================
yt = None
is_logged_in = False

def init_ytm():
    global yt, is_logged_in
    if OAUTH_FILE.exists():
        try:
            yt = YTMusic(str(OAUTH_FILE))
            is_logged_in = True
            print('✅ YT Music: LOGGED IN')
            return
        except Exception as e:
            print(f'⚠️ OAuth error: {e}')
    yt = YTMusic()
    is_logged_in = False
    print('👤 YT Music: GUEST MODE')

init_ytm()

# ============================================
# MUSIC API HELPERS
# ============================================
def musicapi_prepare(query):
    """
    Kirim query ke MusicAPI → dapet song_id.
    Query bisa: nama lagu, "artist - title", atau URL YouTube.
    """
    try:
        url = f'{MUSICAPI_BASE}/prepare/{requests.utils.quote(query, safe="")}'
        print(f'🎵 MusicAPI prepare: {query}')
        res = requests.get(url, timeout=MUSICAPI_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        print(f'🎵 MusicAPI prepare response: {data}')

        # Coba beberapa format response
        song_id = None
        if isinstance(data, dict):
            song_id = (data.get('song_id') or 
                      data.get('songId') or 
                      data.get('id') or
                      data.get('data', {}).get('song_id') if isinstance(data.get('data'), dict) else None)
        elif isinstance(data, str):
            song_id = data
        elif isinstance(data, list) and len(data) > 0:
            song_id = data[0].get('song_id') or data[0].get('id')

        return song_id
    except Exception as e:
        print(f'❌ MusicAPI prepare error: {e}')
        return None


def musicapi_fetch(song_id):
    """
    Ambil detail song dari MusicAPI pakai song_id.
    """
    try:
        url = f'{MUSICAPI_BASE}/fetch/{song_id}'
        print(f'🎵 MusicAPI fetch: {song_id}')
        res = requests.get(url, timeout=MUSICAPI_TIMEOUT)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f'❌ MusicAPI fetch error: {e}')
        return None


def musicapi_get_audio_url(video_id, query=None):
    """
    Flow lengkap: prepare → fetch → audio_url
    Return: (audio_url, title) atau (None, None)
    """
    # Cek cache dulu
    now = time.time()
    cached = _musicapi_prepare_cache.get(video_id)
    if cached and cached['expires'] > now:
        song_id = cached['song_id']
        audio_cached = _musicapi_cache.get(song_id)
        if audio_cached and audio_cached['expires'] > now:
            print(f'✅ MusicAPI cache hit: {video_id}')
            return audio_cached['audio_url'], audio_cached.get('title')
    else:
        # Prepare: pake YouTube URL biar lebih akurat
        yt_url = f'https://www.youtube.com/watch?v={video_id}'
        song_id = musicapi_prepare(yt_url)

        # Kalau gagal, coba pakai query nama lagu
        if not song_id and query:
            song_id = musicapi_prepare(query)

        if not song_id:
            return None, None

        _musicapi_prepare_cache[video_id] = {
            'song_id': song_id,
            'expires': now + CACHE_TTL
        }

    # Fetch detail
    detail = musicapi_fetch(song_id)
    if not detail:
        return None, None

    # Cari audio URL di response
    audio_url = None
    title = None

    if isinstance(detail, dict):
        # Coba berbagai field name
        audio_url = (detail.get('audio_url') or 
                    detail.get('audioUrl') or
                    detail.get('audio') or
                    detail.get('stream_url') or
                    detail.get('url'))
        title = detail.get('title') or detail.get('song_name') or detail.get('name')

        # Nested data
        if not audio_url and isinstance(detail.get('data'), dict):
            d = detail['data']
            audio_url = d.get('audio_url') or d.get('audioUrl') or d.get('audio')
            title = title or d.get('title') or d.get('song_name')

    if not audio_url:
        print(f'⚠️ MusicAPI: audio_url gak ketemu di response: {detail}')
        return None, None

    # Simpan cache
    _musicapi_cache[song_id] = {
        'audio_url': audio_url,
        'title': title,
        'expires': now + CACHE_TTL
    }

    return audio_url, title


def musicapi_audio_endpoint(song_id):
    """Langsung pakai endpoint /audio/{song_id}"""
    return f'{MUSICAPI_BASE}/audio/{song_id}'

# ============================================
# YT-DLP HELPER (Fallback)
# ============================================
def ytdlp_get_audio_url(video_id):
    """Fallback pakai yt-dlp."""
    now = time.time()
    cached = _ytdlp_cache.get(video_id)
    if cached and cached['expires'] > now:
        return cached['url'], cached.get('mime', 'audio/mp4')

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
            info = ydl.extract_info(
                f'https://www.youtube.com/watch?v={video_id}',
                download=False
            )
            audio_url = info.get('url')

            if not audio_url:
                for fmt in reversed(info.get('formats', [])):
                    if fmt.get('acodec') != 'none' and fmt.get('url'):
                        audio_url = fmt['url']
                        break

            if not audio_url:
                return None, None

            ext = info.get('ext', 'm4a')
            mime_map = {
                'm4a': 'audio/mp4',
                'webm': 'audio/webm',
                'mp3': 'audio/mpeg',
                'opus': 'audio/ogg',
            }
            mime = mime_map.get(ext, 'audio/mp4')

            _ytdlp_cache[video_id] = {
                'url': audio_url,
                'mime': mime,
                'expires': now + CACHE_TTL
            }
            return audio_url, mime
    except Exception as e:
        print(f'❌ yt-dlp error [{video_id}]: {e}')
        return None, None

# ============================================
# ROUTES: STATIC
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
def sw_alias():
    response = send_from_directory('../frontend', 'sw.js')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory('../frontend/icons', filename)

# ============================================
# ROUTES: SEARCH (via ytmusicapi)
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
# ROUTES: STREAM AUDIO (MusicAPI → yt-dlp fallback)
# ============================================
@app.route('/api/stream/<video_id>')
def stream_audio(video_id):
    """
    Coba MusicAPI dulu, kalau gagal fallback ke yt-dlp.
    Return redirect ke audio URL.
    """
    title = request.args.get('title', '')
    artist = request.args.get('artist', '')
    query = f'{artist} - {title}'.strip(' -') if (title or artist) else None

    # ============ COBA MUSICAPI DULU ============
    if MUSICAPI_ENABLED:
        print(f'🎯 Streaming {video_id} via MusicAPI...')
        audio_url, _ = musicapi_get_audio_url(video_id, query)
        if audio_url:
            print(f'✅ MusicAPI sukses: {video_id}')
            resp = redirect(audio_url, code=302)
            resp.headers['Cache-Control'] = 'public, max-age=3600'
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp
        print(f'⚠️ MusicAPI gagal, fallback ke yt-dlp')

    # ============ FALLBACK KE YT-DLP ============
    print(f'🎯 Streaming {video_id} via yt-dlp...')
    audio_url, mime = ytdlp_get_audio_url(video_id)
    if audio_url:
        print(f'✅ yt-dlp sukses: {video_id}')
        resp = redirect(audio_url, code=302)
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

    # ============ DUA-DUANYA GAGAL ============
    print(f'❌ Semua source gagal: {video_id}')
    return jsonify({
        'error': 'Gagal stream audio dari semua source',
        'video_id': video_id
    }), 502

# ============================================
# ROUTES: LYRICS
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
                'message': 'Lagu instrumental'
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
# ROUTES: PLAYLISTS
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
# AUTH STATUS
# ============================================
@app.route('/api/auth/status')
def auth_status():
    info = {'logged_in': is_logged_in}
    if is_logged_in:
        try:
            info['account'] = yt.get_account_info()
        except:
            pass
    return jsonify(info)

# ============================================
# HEALTH CHECK
# ============================================
@app.route('/api/health')
def health():
    """Cek status semua service."""
    # Cek MusicAPI
    musicapi_status = 'unknown'
    try:
        r = requests.get(f'{MUSICAPI_BASE}/prepare/test', timeout=8)
        musicapi_status = 'ok' if r.status_code < 500 else f'error {r.status_code}'
    except Exception as e:
        musicapi_status = f'down: {str(e)[:50]}'

    # Cek yt-dlp
    ytdlp_status = 'ok'
    try:
        v = yt_dlp.version.__version__
        ytdlp_status = f'v{v}'
    except:
        ytdlp_status = 'error'

    return jsonify({
        'status': 'ok',
        'musicapi': {
            'enabled': MUSICAPI_ENABLED,
            'base_url': MUSICAPI_BASE,
            'status': musicapi_status,
        },
        'ytdlp': {
            'version': ytdlp_status,
            'cache_size': len(_ytdlp_cache),
        },
        'ytmusic': 'logged_in' if is_logged_in else 'guest',
        'cache': {
            'musicapi_songs': len(_musicapi_cache),
            'musicapi_prepare': len(_musicapi_prepare_cache),
            'ytdlp': len(_ytdlp_cache),
        },
        'playlists_count': len(load_playlists()),
    })

# ============================================
# TOGGLE MUSICAPI (buat debug)
# ============================================
@app.route('/api/toggle-musicapi')
def toggle_musicapi():
    global MUSICAPI_ENABLED
    MUSICAPI_ENABLED = not MUSICAPI_ENABLED
    return jsonify({'musicapi_enabled': MUSICAPI_ENABLED})

# ============================================
# DEBUG: TEST MUSICAPI ENDPOINT
# ============================================
@app.route('/api/debug/musicapi/<path:query>')
def debug_musicapi(query):
    """Debug endpoint buat test MusicAPI langsung."""
    song_id = musicapi_prepare(query)
    if not song_id:
        return jsonify({'error': 'prepare gagal', 'query': query}), 500

    detail = musicapi_fetch(song_id)
    audio_url, title = musicapi_get_audio_url(query, query)

    return jsonify({
        'query': query,
        'song_id': song_id,
        'detail': detail,
        'audio_url': audio_url,
        'title': title,
    })

# ============================================
# RUN
# ============================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('\n' + '=' * 60)
    print('🎵 Musicfy Backend — MusicAPI + yt-dlp Dual Source')
    print('=' * 60)
    print(f'🌐 Server     : http://localhost:{port}')
    print(f'📁 Data       : {DATA_DIR}')
    print(f'🔐 YT Music   : {"LOGGED IN" if is_logged_in else "GUEST MODE"}')
    print(f'🎧 yt-dlp     : v{yt_dlp.version.__version__}')
    print(f'🎵 MusicAPI   : {MUSICAPI_BASE}')
    print(f'   Status     : {"ENABLED" if MUSICAPI_ENABLED else "DISABLED"}')
    print('=' * 60 + '\n')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
