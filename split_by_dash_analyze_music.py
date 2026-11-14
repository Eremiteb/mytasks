#!/usr/bin/env python3
"""Анализ /mnt/copy/Music (только чтение): статистика и кандидаты на объединение папок
по разным паттернам. Печатает число групп и примеры для каждого паттерна."""
import collections
import os
import re
import sys
import unicodedata
from pathlib import Path

MUSIC = Path("/mnt/copy/Music")
AUDIO = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".wav", ".aac"}
EX = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def base_key(n: str) -> str:
    """Текущий ключ скрипта: &amp;->&, слова и/and убраны, только буквы/цифры."""
    s = " " + n.lower().replace("&amp;", "&") + " "
    s = re.sub(r"\s+(и|and)\s+", " ", s)
    s = re.sub(r"\s+(и|and)\s+", " ", s)
    return "".join(c for c in s if c.isalnum())


# --- сбор
dirs = sorted(d for d in MUSIC.iterdir() if d.is_dir() and not d.name.startswith("."))
root_files = [f for f in MUSIC.iterdir() if f.is_file()]
info = {}
tot_files = tot_audio = 0
ext_count = collections.Counter()
for d in dirs:
    files = [p for p in d.rglob("*") if p.is_file()]
    sub = [p for p in d.iterdir() if p.is_dir()]
    audio = [p for p in files if p.suffix.lower() in AUDIO]
    for p in files:
        ext_count[p.suffix.lower() or "(без расширения)"] += 1
    info[d.name] = {"files": len(files), "audio": len(audio), "subdirs": len(sub),
                    "names": [p.name for p in files]}
    tot_files += len(files)
    tot_audio += len(audio)

names = [d.name for d in dirs if d.name != "_cloud"]
print("=== Общая картина")
print(f"папок верхнего уровня: {len(dirs)} (без _cloud: {len(names)}); файлов в них: {tot_files}, аудио: {tot_audio}")
print(f"файлов в корне Music: {len(root_files)}: {[f.name for f in root_files][:5]}")
cloud = MUSIC / "_cloud"
if cloud.is_dir():
    cf = [p for p in cloud.iterdir() if p.is_file()]
    nd = [p for p in cf if not re.search(r"\s[-–—]\s", p.name)]
    print(f"_cloud: файлов {len(cf)}, без « - » в имени (сортировщик их не разложит): {len(nd)}; примеры: {[p.name for p in nd[:4]]}")
print("расширения:", ", ".join(f"{e} {c}" for e, c in ext_count.most_common(12)))
sizes = collections.Counter(min(info[n]["files"], 6) for n in names)
print("папок по числу файлов (6 = 6+):", dict(sorted(sizes.items())))
empty = [n for n in names if info[n]["files"] == 0]
print(f"пустых папок: {len(empty)} {empty[:EX]}")
noaudio = [n for n in names if info[n]["files"] and not info[n]["audio"]]
print(f"папок без аудио: {len(noaudio)} {noaudio[:EX]}")
nested = [n for n in names if info[n]["subdirs"]]
print(f"папок с подпапками (альбомы): {len(nested)}")


def show(title, groups, explain):
    groups = [g for g in groups if len(g) > 1]
    print(f"\n=== {title}: групп {len(groups)}")
    print("    " + explain)
    for g in groups[:EX]:
        print("   ", " | ".join(f"{n} ({info[n]['files']})" for n in g))


def group_by(fn, pool=None):
    g = collections.defaultdict(list)
    for n in (pool or names):
        k = fn(n)
        if k:
            g[k].append(n)
    return [v for v in g.values() if len(v) > 1 and len({base_key(x) for x in v}) > 1]


# 1. Порядок соавторов
SEP = re.compile(r"\s*(?:&|,|\bи\b|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?|\bпри уч\.?)\s*", re.I)


def artists(n):
    parts = [base_key(p) for p in SEP.split(n.lower().replace("&amp;", "&")) if p and p.strip()]
    return [p for p in parts if p]


show("1. Те же соавторы в другом порядке (A & B / B & A)",
     group_by(lambda n: "|".join(sorted(artists(n))) if len(artists(n)) > 1 else None),
     "ключ — множество исполнителей, разделители &, «,», и, and, feat., ft., x, vs")

# 2. feat.-папки при существующей папке основного исполнителя
by_key = {base_key(n): n for n in names}
feat = []
for n in names:
    m = re.match(r"^(.*?)\s+(?:feat\.?|ft\.?|featuring|при уч\.?)\s+.+$", n, re.I)
    if m and base_key(m.group(1)) in by_key and by_key[base_key(m.group(1))] != n:
        feat.append([by_key[base_key(m.group(1))], n])
print(f"\n=== 2. «A feat. B» при существующей папке «A»: {len(feat)}")
print("    перенос коллабораций в папку основного исполнителя")
for g in feat[:EX]:
    print("   ", " <- ".join(f"{x} ({info[x]['files']})" for x in g))
all_feat = [n for n in names if re.search(r"\b(feat\.?|ft\.?|featuring)\b", n, re.I)]
print(f"    всего папок с feat./ft.: {len(all_feat)}")

# 3. Коллаборации «A & B», когда есть папка «A»
collab = []
for n in names:
    a = artists(n)
    if len(a) > 1 and "feat" not in n.lower():
        first = a[0]
        if first in by_key and by_key[first] != n:
            collab.append([by_key[first], n])
print(f"\n=== 3. «A & B» / «A, B» при существующей папке «A» (первый исполнитель): {len(collab)}")
for g in collab[:EX]:
    print("   ", " <- ".join(f"{x} ({info[x]['files']})" for x in g))

# 4. Артикль The
show("4. Артикль «The X» / «X»",
     group_by(lambda n: base_key(re.sub(r"^the\s+", "", n, flags=re.I))),
     "The Game / Game, The Wanted / Wanted")

# 5. Диакритика и ё
def strip_marks(n):
    n = n.replace("ё", "е").replace("Ё", "Е")
    return base_key("".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c)))


show("5. Диакритика и ё/е (Beyoncé / Beyonce, Алёна / Алена)", group_by(strip_marks),
     "NFKD без диакритики, ё -> е")

# 6. Казахские буквы
KZ = str.maketrans("әғқңөұүһіӘҒҚҢӨҰҮҺІ", "агкноууhиАГКНОУУHИ")
show("6. Казахские буквы vs русские (Ағайындылар / Агайындылар)",
     group_by(lambda n: base_key(n.translate(KZ))), "ә->а, ғ->г, қ->к, ң->н, ө->о, ұ/ү->у, һ->h, і->и")

# 7. Смешение кириллицы и латиницы (омоглифы)
HOMO = str.maketrans("аеорсухкмтвнАЕОРСУХКМТВН", "aeopcyxkmtbhAEOPCYXKMTBH")
mixed = [n for n in names if re.search(r"[a-zA-Z]", n) and re.search(r"[а-яА-ЯёЁ]", n)
         and any(re.search(r"[a-zA-Z]", w) and re.search(r"[а-яА-ЯёЁ]", w) for w in n.split())]
print(f"\n=== 7. Слова со смесью кириллицы и латиницы (омоглифы, Bоney M.): {len(mixed)}")
print("    ", mixed[:EX])
show("7a. Совпадение после замены омоглифов", group_by(lambda n: base_key(n.translate(HOMO))), "кириллические а,е,о,р,с,у,х,к,м,т,в,н -> латиница")

# 8. Транслитерация
TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
      "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
      "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def translit(n):
    k = base_key(n.translate(KZ))
    out = "".join(TR.get(c, c) for c in k)
    out = out.replace("kh", "h").replace("ks", "x").replace("iy", "y").replace("yy", "y")
    return out


show("8. Транслитерация (Gradusy / Градусы)", group_by(translit), "кириллица -> латиница по упрощённой схеме")

# 9. Хвосты-мусор: сайты, (official), группа/тобы, band
NOISE = re.compile(r"\s*[\(\[]?\b(?:\w+\.(?:kz|ru|com|net|org)|official|music|band|группа|гр\.|тобы|тобі|ансамбль)\b[\)\]]?\s*", re.I)
show("9. Служебные слова и сайты (группа, тобы, band, official, *.kz)",
     group_by(lambda n: base_key(NOISE.sub(" ", n))), "отбрасываются сайты, «группа», «тобы», band, official")

# 10. Папки, похожие на «Исполнитель - Название» (ошибочная раскладка)
dash = [n for n in names if re.search(r"\s[-–—]\s", n)]
print(f"\n=== 10. Имя папки содержит « - » (вероятно ошибочная раскладка): {len(dash)}")
print("    ", dash[:EX])

# 11. Опечатки: расстояние Левенштейна 1 (длина ключа >= 6)
def lev1(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            j += 1
            if diff > 1:
                return False
        else:
            i += 1
            j += 1
    return True


keys = collections.defaultdict(list)
for n in names:
    keys[base_key(n)].append(n)
buckets = collections.defaultdict(list)
for k in keys:
    if len(k) >= 6:
        buckets[k[:3]].append(k)
typo = []
for b in buckets.values():
    for i in range(len(b)):
        for j in range(i + 1, len(b)):
            if lev1(b[i], b[j]):
                typo.append([keys[b[i]][0], keys[b[j]][0]])
print(f"\n=== 11. Возможные опечатки (одна буква, ключ >= 6 символов): {len(typo)}")
print("    только кандидаты для ручной проверки (Log Dog / Loc Dog, Eminem / Eminen)")
for g in typo[:EX * 2]:
    print("   ", " | ".join(f"{x} ({info[x]['files']})" for x in g))

# 12. Префикс DJ / MC
show("12. Префикс DJ/MC (DJ Smash / Smash)",
     group_by(lambda n: base_key(re.sub(r"^(dj|mc)\s+", "", n, flags=re.I))), "")
