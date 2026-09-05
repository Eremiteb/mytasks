import logging
import random  # Добавлено для рандомизации
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

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
        logger.warning(f"PJAX Warmup failed: {e}")

    # 2. Основной запрос
    try:
        resp = session.get(target_url, timeout=20, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        if "500" in str(e):
            # Если поймали 500, уходим в глубокую паузу
            cool_down = random.randint(30, 60)
            logger.error(f"LMusic Server Error (500). Охлаждение {cool_down} сек...")
            time.sleep(cool_down)
        return []

    tracks = []
    # На основе вашего HTML: ищем блоки 'c-card-mp3'
    items = soup.find_all('div', class_='c-card-mp3')

    for item in items:
        try:
            # ID и ссылки берем из data-атрибутов (это надежнее, чем парсить текст)
            t_id = item.get('data-mp3_id')
            artist = item.get('data-artist_name')
            title = item.get('data-song_name')
            dl_url = item.get('data-download_url')

            if not t_id or not dl_url:
                continue

            tracks.append({
                'id': t_id,
                'artist': artist,
                'title': title,
                'download_url': urljoin(base_url, dl_url),
                'referer': target_url
            })
        except Exception:
            continue

    # 3. Рандомизированная финальная пауза
    # Базовые 8 секунд + случайные от 5 до 15 секунд
    total_wait = 8 + random.randint(5, 15)
    logger.info(f"LMusic: категория {category_path} обработана. Пауза {total_wait} сек.")
    time.sleep(total_wait)
    
    return tracks