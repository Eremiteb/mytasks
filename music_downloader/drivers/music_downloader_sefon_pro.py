import base64
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

def sefon_decrypt(url_str, key_str):
    if not url_str or not key_str:
        return None
    if url_str.startswith('#'):
        url_str = url_str[1:]
    reversed_key = key_str[::-1]
    for char in reversed_key:
        parts = url_str.split(char)
        url_str = char.join(parts[::-1])
    try:
        padding = len(url_str) % 4
        if padding:
            url_str += '=' * (4 - padding)
        return base64.b64decode(url_str).decode('utf-8')
    except Exception:
        return None

def get_tracks(session, base_url, category_path):
    target_url = urljoin(base_url, category_path)
    resp = session.get(target_url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    blocks = soup.find_all('div', class_='mp3')
    
    tracks = []
    for idx, block in enumerate(blocks):
        mp3_id = block.get('data-mp3_id')
        el = block.find(class_='url_protected')
        if not mp3_id or not el:
            continue
        
        path = sefon_decrypt(el.get('data-url') or el.get('href'), el.get('data-key'))
        if not path:
            continue

        artist_el = block.select_one('.artist_name')
        title_el = block.select_one('.song_name')
        artist = artist_el.get_text(strip=True) if artist_el else None
        title = title_el.get_text(strip=True) if title_el else f"Track_{idx}"
        
        tracks.append({
            'id': mp3_id, 'artist': artist, 'title': title,
            'download_url': urljoin(base_url, path)
        })
    return tracks