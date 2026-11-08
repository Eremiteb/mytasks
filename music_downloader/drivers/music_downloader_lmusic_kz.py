import logging
import random  # Добавлено для рандомизации
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

# Значения по умолчанию; переопределяются блоком drivers.music_downloader_lmusic_kz в music_downloader.json.
SETTINGS = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "warmup_timeout_seconds": 15,
    "warmup_pause_range_seconds": [2, 5],
    "category_timeout_seconds": 20,
    "server_error_pause_range_seconds": [30, 60],
    "final_pause_seconds": 8,
    "final_pause_extra_range_seconds": [5, 15],
    "search_connect_timeout_seconds": 5,
    "search_read_timeout_seconds": 15,
}

def parse_tracks(html, base_url, referer):
    """Разбирает карточки категории или поисковой выдачи без запросов и пауз."""
    soup = BeautifulSoup(html, 'html.parser')
    tracks = []
    for item in soup.find_all('div', class_='c-card-mp3'):
        track_id = item.get('data-mp3_id')
        download_url = item.get('data-download_url')
        if track_id and download_url:
            tracks.append({
                'id': track_id,
                'artist': item.get('data-artist_name'),
                'title': item.get('data-song_name'),
                'download_url': urljoin(base_url, download_url),
                'referer': referer,
            })
    return tracks


def get_tracks(session, base_url, category_path):
    target_url = urljoin(base_url, category_path)
    headers = {
        'User-Agent': SETTINGS["user_agent"],
        'Referer': base_url,
        'X-Requested-With': 'XMLHttpRequest' # Помогает при PJAX запросах
    }
    
    # 1. PJAX прогрев
    params = {"pjax": "true", "is_pagination": "true"}
    try:
        session.get(target_url, params=params, timeout=SETTINGS["warmup_timeout_seconds"], headers=headers)
        # Рандомная пауза после прогрева
        time.sleep(random.uniform(*SETTINGS["warmup_pause_range_seconds"]))
    except Exception as e:
        logger.warning(f"Не удалось выполнить предварительный PJAX-запрос: {e}")

    # 2. Основной запрос
    try:
        resp = session.get(target_url, timeout=SETTINGS["category_timeout_seconds"], headers=headers)
        resp.raise_for_status()

    except Exception as e:
        if "500" in str(e):
            # Если поймали 500, уходим в глубокую паузу
            cool_down = random.randint(*map(int, SETTINGS["server_error_pause_range_seconds"]))
            logger.error(f"LMusic: ошибка сервера (500). Пауза {cool_down} сек...")
            time.sleep(cool_down)
        return []

    tracks = parse_tracks(resp.text, base_url, target_url)

    # 3. Рандомизированная финальная пауза
    # Базовая пауза final_pause_seconds + случайная добавка из final_pause_extra_range_seconds (SETTINGS)
    total_wait = SETTINGS["final_pause_seconds"] + random.randint(*map(int, SETTINGS["final_pause_extra_range_seconds"]))
    logger.info(f"LMusic: категория {category_path} обработана. Пауза {total_wait} сек.")
    time.sleep(total_wait)
    
    return tracks

def search_tracks(session, base_url, search_path):
    """Поисковая выдача LMusic (`/search?q=`) без PJAX-прогрева и пауз раздела."""
    url = urljoin(base_url, search_path)
    response = session.get(url, timeout=(SETTINGS["search_connect_timeout_seconds"], SETTINGS["search_read_timeout_seconds"]))
    response.raise_for_status()
    return parse_tracks(response.text, base_url, url)
