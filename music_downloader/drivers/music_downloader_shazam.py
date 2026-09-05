import base64
import logging
import random
import subprocess
import time
from urllib.parse import quote, urljoin

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
    
    # Используем curl без фейкового User-Agent, так как Shazam может блокировать браузерные заголовки от автоматизированных запросов
    try:
        result = subprocess.run(['curl', '-s', target_url], 
                                capture_output=True, text=True, timeout=15)
        html_content = result.stdout
        if not html_content:
            logger.error(f"Shazam: Empty response from curl for {target_url}")
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
    except Exception as e:
        logger.error(f"Shazam: Failed to fetch {target_url} via curl: {e}")
        return []
        
    # Парсинг песен из Shazam
    song_titles = [a.get_text(strip=True) for a in soup.find_all('a', {'data-test-id': 'charts_userevent_list_songTitle'}) if a.get_text(strip=True)]
    artist_names = [a.get_text(strip=True) for a in soup.find_all('a', {'data-test-id': 'charts_userevent_list_artistName'}) if a.get_text(strip=True)]
    
    min_len = min(len(song_titles), len(artist_names))
    parsed_songs = []
    
    def has_container(c):
        return bool(c and 'SongItem-module_mainItemsContainer' in c)
        
    blocks = soup.find_all('div', class_=has_container)
    if blocks:
        for block in blocks:
            t_el = block.find('a', {'data-test-id': 'charts_userevent_list_songTitle'})
            a_el = block.find('a', {'data-test-id': 'charts_userevent_list_artistName'})
            if t_el and a_el:
                t = t_el.get_text(strip=True)
                a = a_el.get_text(strip=True)
                if t and a:
                    parsed_songs.append({'artist': a, 'title': t})
    
    if not parsed_songs and min_len > 0:
        for i in range(min_len):
            parsed_songs.append({'artist': artist_names[i], 'title': song_titles[i]})
            
    if not parsed_songs:
        logger.warning("Shazam: No songs parsed from chart.")
        return []

    logger.info(f"Shazam: Parsed {len(parsed_songs)} songs. Searching in Sefon.pro...")
    
    tracks = []
    max_search = 100
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    
    for _idx, song in enumerate(parsed_songs[:max_search]):
        artist = song['artist']
        title = song['title']
        
        query = quote(f"{artist} {title}")
        s_url = f"https://sefon.pro/search/?q={query}"
        
        try:
            # Для Sefon используем cloudscraper из сессии
            s_resp = session.get(s_url, headers=headers, timeout=10)
            if s_resp.status_code != 200:
                time.sleep(1)
                continue
                
            s_soup = BeautifulSoup(s_resp.text, 'html.parser')
            s_blocks = s_soup.find_all('div', class_='mp3')
            
            if not s_blocks:
                time.sleep(random.uniform(0.5, 1.5))
                continue
                
            block = s_blocks[0]
            mp3_id = block.get('data-mp3_id')
            el = block.find(class_='url_protected')
            
            if not mp3_id or not el:
                continue
                
            path = sefon_decrypt(el.get('data-url') or el.get('href'), el.get('data-key'))
            if not path:
                continue
                
            dl_url = urljoin('https://sefon.pro', path)
            
            tracks.append({
                'id': f"shazam_{mp3_id}",
                'artist': artist,
                'title': title,
                'download_url': dl_url,
                'referer': s_url
            })
            
            time.sleep(random.uniform(0.3, 1.0))
            
        except Exception as e:
            logger.debug(f"Shazam->Sefon search failed for {artist} - {title}: {e}")
            continue

    logger.info(f"Shazam: Found {len(tracks)} downloadable tracks out of {len(parsed_songs)}.")
    return tracks
