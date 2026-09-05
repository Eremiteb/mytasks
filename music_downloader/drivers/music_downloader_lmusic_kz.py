import logging
import random  # Добавлено для рандомизации
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': base_url,
        'X-Requested-With': 'XMLHttpRequest' # Помогает при PJAX запросах
    }
    
    # 1. PJAX прогрев
    params = {"pjax": "true", "is_pagination": "true"}
    try:
        session.get(target_url, params=params, timeout=15, headers=headers)
        # Рандомная пауза после прогрева
        time.sleep(random.uniform(2, 5)) 
    except Exception as e:
        logger.warning(f"Не удалось выполнить предварительный PJAX-запрос: {e}")

    # 2. Основной запрос
    try:
        resp = session.get(target_url, timeout=20, headers=headers)
        resp.raise_for_status()

    except Exception as e:
        if "500" in str(e):
            # Если поймали 500, уходим в глубокую паузу
            cool_down = random.randint(30, 60)
            logger.error(f"LMusic: ошибка сервера (500). Пауза {cool_down} сек...")
            time.sleep(cool_down)
        return []

    tracks = parse_tracks(resp.text, base_url, target_url)

    # 3. Рандомизированная финальная пауза
    # Базовые 8 секунд + случайные от 5 до 15 секунд
    total_wait = 8 + random.randint(5, 15)
    logger.info(f"LMusic: категория {category_path} обработана. Пауза {total_wait} сек.")
    time.sleep(total_wait)
    
    return tracks