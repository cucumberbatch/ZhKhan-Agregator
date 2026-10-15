"""
Сопоставление уведомлений elicense.kz с ЖК на карте.

Порядок:
  1. По наименованию объекта. В PDF есть поле «наименование объекта»,
     а в реестре qportal — поле name с тем же описанием. Замер на
     реальных данных: 21 совпадение из 28, средняя оценка 0.80.
  2. По БИН. БИН заявителя есть и в PDF, и в реестре (customer_bin).
     Служит фильтром: одно наименование используют десятки разных
     компаний, поэтому без БИН подбор уводит к чужому застройщику.
  3. По адресу и геокодированию — запасные пути. Работают заметно
     слабее: адрес в PDF и в реестре записывается по-разному.
     Работает как запасной путь, порог расстояния настраивается.

Геокодер передаётся снаружи (см. server._nominatim_geocode), чтобы не
дублировать логику Nominatim и не тянуть сюда HTTP-клиент.
"""

import math
import re
from difflib import SequenceMatcher

# Порог, начиная с которого адрес из уведомления считаем совпадающим
# с запрошенным. Ниже — совпадение считается случайным.
ADDRESS_MATCH_THRESHOLD = 0.30

# Порог для наименования объекта. Замер на реальных данных: у верных
# пар оценка 0.8–1.0, у чужих — 0.2–0.4. Порог 0.6 разделяет их.
NAME_MATCH_THRESHOLD = 0.60

# Порог-вето для адреса. Наименование у застройщиков повторяется, поэтому
# одно только совпадение названия подставляет неверный ЖК: у «Asyll Dala»
# в реестре два ЖК на Е 117, а семь уведомлений — про Букар Жырау 26,
# объекта которого в реестре нет. Название совпадало с оценкой 1.000.
#
# Замер по 28 уведомлениям: у неверных подборов адрес 0.067–0.091,
# у верных 0.182–0.750. Порог 0.15 разделяет их с запасом.
ADDRESS_GATE_THRESHOLD = 0.15

# Порог гео-подбора, метры.
DEFAULT_RADIUS_M = 300

# Слова-обвязки казахстанских адресов, которые не несут смысла
_NOISE = re.compile(
    r"\b(республика\s+казахстан|г\.|г\.\s*|город|район|жалобы|ауыл|"
    r"к\.|улица|проспект|пересечение|улиц|шоссе|мкр|жилой\s+массив|"
    r"ж\.?м\.?|новый|старый)\b",
    re.IGNORECASE)


def _clean_bin(value):
    return re.sub(r"\D", "", str(value or "")) or None


def _haversine(lat1, lon1, lat2, lon2):
    """Расстояние между точками в метрах."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# Письмо и цифры в адресе пишут по-разному: «ул. Е117» в PDF против
# «улица Е 117» в реестре. Склеиваем такие пары в один токен.
_RE_LETTER_NUM = re.compile(r"(?<=[а-яa-zё])\s+(?=\d)")

# Обрезаем слово до корня. Русские адреса в источниках стоят в разных
# падежах: «Ж. Нәжімеденова» в PDF и «Жұмекен Нәжімеденов» в реестре —
# адрес один, а слова совпадают лишь по началу.
_STEM_LEN = 6


def _stem(token):
    return token[:_STEM_LEN] if len(token) > _STEM_LEN else token


def address_tokens(address, stem=True):
    """
    Значимые куски адреса без служебных слов.

    stem=True обрезает слова до корня. Без этого «Нәжімеденова» и
    «Нәжімеденов» считаются разными улицами, а это одна и та же.
    """
    if not address:
        return set()
    cleaned = _NOISE.sub(" ", str(address))
    cleaned = re.sub(r"[«»\"'`().,;]+", " ", cleaned)
    cleaned = _RE_LETTER_NUM.sub("", cleaned)
    tokens = {t for t in cleaned.lower().split() if len(t) > 2}
    return {_stem(t) for t in tokens} if stem else tokens


def address_similarity(a, b):
    """Доля совпавших значимых слов. 0..1."""
    ta, tb = address_tokens(a), address_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def build_token_weights(addresses):
    """
    Вес каждого слова по корпусу адресов: чем реже встречается, тем важнее.

    Зачем: без этого «Астана» и «Есиль» встречаются в каждом адресе и
    размывают различие улиц. Из-за этого адрес «улица Достык, 13»
    набирал 0.33 против «ул. Е 117» — ложное срабатывание. С весами
    IDF общие слова почти не влияют на оценку, а название улицы
    определяет результат.

    Возвращает {слово: вес}. Пустой корпус — пустой словарь, тогда
    сравнение идёт равномерно, как раньше.
    """
    df = {}
    total = 0
    for address in addresses:
        total += 1
        for token in address_tokens(address):
            df[token] = df.get(token, 0) + 1

    if not total:
        return {}
    return {token: math.log(1.0 + total / count)
            for token, count in df.items()}


def weighted_similarity(a, b, weights):
    """
    Сходство с учётом весов слов. При пустых весах — как address_similarity.
    """
    ta, tb = address_tokens(a), address_tokens(b)
    if not ta or not tb:
        return 0.0
    if not weights:
        return len(ta & tb) / len(ta | tb)

    def weight(tokens):
        # неизвестные корпусу слова считаем максимально весомыми
        return sum(weights.get(t, 3.0) for t in tokens)

    common, union = ta & tb, ta | tb
    denominator = weight(union)
    if denominator <= 0:
        return 0.0
    return weight(common) / denominator


def build_bin_index(zhk_list):
    """Индекс БИН → список ЖК. Один застройщик может вести несколько ЖК."""
    index = {}
    for zhk in zhk_list or []:
        b = _clean_bin(zhk.get("customer_bin"))
        if b:
            index.setdefault(b, []).append(zhk)
    return index


def match_by_bin(notification, zhk_list, bin_index=None):
    """
    Ищет ЖК по БИН заявителя из PDF. Возвращает (список ЖК, способ).
    """
    index = bin_index if bin_index is not None else build_bin_index(zhk_list)
    bin_val = _clean_bin(notification.get("applicant_bin"))

    if not bin_val:
        return [], None

    matches = index.get(bin_val)
    if matches:
        return list(matches), "bin"
    return [], None


def match_by_geocode(notification, zhk_list, geocode, radius_m=DEFAULT_RADIUS_M):
    """
    Запасной путь: геокодирует адрес из PDF и ищет ближайшие ЖК.

    geocode(address) -> (lat, lon, display_name) либо (None, None, None).
    Возвращает (список ЖК, расстояние_в_метрах, способ).
    """
    address = notification.get("address") or notification.get("cadastral")
    if not address or geocode is None:
        return [], None, None

    lat, lon, display = geocode(address)
    if lat is None or lon is None:
        return [], None, None

    points = [(z, _haversine(lat, lon, z["lat"], z["lon"]))
              for z in (zhk_list or []) if z.get("lat") and z.get("lon")]

    near = [(z, d) for z, d in points if d <= radius_m]
    near.sort(key=lambda p: p[1])

    return ([z for z, _ in near], near[0][1] if near else None, "geocode")


def pick_best(candidates, pdf_address=None, pdf_object_name=None):
    """
    Из нескольких подходящих ЖК выбирает наиболее похожий.

    Наименование сравнивается посимвольно (name_similarity), а не
    токенизатором адресов: у наименований другой дефект — слипшиеся
    после переноса слова, и адресный токенизатор на них ошибается.
    """
    best, best_score = None, -1.0

    for zhk in candidates:
        score = 0.0
        if pdf_object_name:
            score = max(score, name_similarity(pdf_object_name,
                                                clean_zhk_name(zhk.get("name"))))
        if pdf_address:
            score = max(score, address_similarity(pdf_address, zhk.get("address")))
        if not pdf_address and not pdf_object_name:
            score = 0.0
        if score > best_score:
            best, best_score = zhk, score

    return best, max(best_score, 0.0)


def match_notification(notification, zhk_list, geocode=None,
                        radius_m=DEFAULT_RADIUS_M, bin_index=None):
    """
    Полное сопоставление уведомления с ЖК.

    Порядок:
      1. наименование объекта — основной путь;
      2. БИН заявителя;
      3. геокодирование адреса.

    Наименование у застройщика часто не уникально: у 22 компаний по
    несколько ЖК, и у половины из них названия совпадают дословно.
    Поэтому наименование сужает выбор до кандидатов, адрес выбирает
    из них и одновременно служит вето: если ни один ЖК застройщика по
    адресу не подходит, значит объекта нет в реестре проектов и
    подменять его чужим нельзя.

    Возвращает:
        zhk, method, distance_m, score, candidates, bin_verified,
        tiebreak, tiebreak_score, ambiguous, rejected

    method — «name_bin» (наименование в рамках известного застройщика),
    «name», «bin» или «geocode». bin_verified отличает надёжное
    сопоставление от подбора по одному лишь наименованию, rejected
    объясняет, почему подбора не состоялось.
    """
    result = {"zhk": None, "method": None, "distance_m": None,
              "score": None, "candidates": 0, "bin_verified": False,
              "tiebreak": None, "tiebreak_score": None,
              "ambiguous": False, "rejected": None}

    pdf_address = notification.get("address") or notification.get("cadastral")
    pdf_name = notification.get("object_name")
    bin_val = _clean_bin(notification.get("applicant_bin"))

    if bin_index is None:
        bin_index = build_bin_index(zhk_list)

    # --- 1. наименование объекта ---------------------------------
    if pdf_name:
        # Если БИН заявителя есть в реестре, ищем объект только среди
        # ЖК этого застройщика: одно наименование используют десятки
        # разных компаний, и без ограничения подбор уводит к чужому.
        scoped = bin_index.get(bin_val) or [] if bin_val else []
        pool = scoped or list(zhk_list or [])

        nm = best_name_match(pdf_name, pool)
        if nm["matched"]:
            candidates = nm["matched_list"] or [nm["best"]]

            # Адрес здесь не доводка, а вето. Наименование у застройщиков
            # повторяется дословно, поэтому совпадение названия само по
            # себе уводит на чужой адрес — отсекаем такие случаи.
            if pdf_address:
                addr_scores = [
                    (address_similarity(pdf_address, z.get("address")), z)
                    for z in candidates
                ]
                addr_scores.sort(key=lambda p: p[0], reverse=True)
                top_addr, top_addr_zhk = addr_scores[0]
                result["tiebreak_score"] = round(top_addr, 3)

                if top_addr < ADDRESS_GATE_THRESHOLD:
                    # Ни один ЖК застройщика по этому адресу не подходит:
                    # объекта просто нет в реестре проектов.
                    result["rejected"] = (
                        f"наименование совпало, но адрес не подтверждён "
                        f"({top_addr:.2f} < {ADDRESS_GATE_THRESHOLD})")
                    return result

                # Адрес указывает на конкретный ЖК — он и выигрывает.
                best = top_addr_zhk
                if len(candidates) > 1:
                    result["ambiguous"] = True
                    result["tiebreak"] = "по адресу"
            else:
                best = nm["best"]
                if len(candidates) > 1:
                    result["ambiguous"] = True
                    result["tiebreak"] = ("адрес в PDF отсутствует, "
                                          "выбран первый из кандидатов")

            result.update({
                "zhk": best,
                "method": "name_bin" if scoped else "name",
                "score": nm["similarity"],
                "candidates": len(candidates),
                "bin_verified": bool(scoped),
            })
            return result

    # --- 2. БИН заявителя ----------------------------------------
    matches, _ = match_by_bin(notification, zhk_list, bin_index)
    if matches:
        best, score = pick_best(matches, pdf_address, pdf_name)
        result.update({
            "zhk": best,
            "method": "bin",
            "candidates": len(matches),
            "score": round(score, 3),
            "bin_verified": True,
        })
        return result

    # --- 3. геокодирование ---------------------------------------
    candidates, distance, _ = match_by_geocode(
        notification, zhk_list, geocode, radius_m)
    if candidates:
        best, score = pick_best(candidates, pdf_address, pdf_name)
        result.update({
            "zhk": best,
            "method": "geocode",
            "distance_m": (round(distance, 1)
                           if distance is not None else None),
            "candidates": len(candidates),
            "score": round(score, 3),
        })

    return result


# ============================================================
# НАИМЕНОВАНИЯ ОБЪЕКТА
# ============================================================
# В реестре qportal поле name часто содержит и сам объект, и адрес:
#   «Многоквартирный жилой комплекс …, расположенный по адресу:
#     город Астана, р-н Сарайшық, улица …»
# Для сравнения с наименованием из PDF адрес надо отбросить.
_ZHK_ADDR_TAIL = re.compile(
    r"\s*,?\s*расположенн\w*\s+(?:по\s+адресу\s*:?|в)\s*.+$",
    re.IGNORECASE | re.DOTALL)


def clean_zhk_name(name):
    """Отбрасывает «расположенный по адресу: …» и обвязку в кавычках."""
    if not name:
        return ""
    return _ZHK_ADDR_TAIL.sub("", name).strip(" ,.;«»\"'")


def normalize_name(text):
    """
    Нормализация с СОХРАНЕНИЕМ границ слов.

    Пробелы убирать нельзя: без них «с встроенными» и «со встроенными»
    дают одну и ту же строку и оценку 1.000 — ложный максимум.

    Внутрисловные дефекты PDF, наоборот, лечатся:
      «помещени-ями» → «помещениями»   (мягкий перенос)
      «строительно-монтажных» → «строительномонтажных» (настоящий дефис)
    """
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"(?<=[а-яa-z])-(?=[а-яa-z])", "", t)
    t = re.sub(r"[^0-9a-zа-я]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _trigrams(text, n=3):
    if len(text) < n:
        return {text} if text else set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def name_similarity(a, b):
    """
    Сходство наименований: максимум из триграммного Жаккара и
    посимвольного SequenceMatcher.

    Посимвольное сравнение выбрано сознательно: наименования из PDF
    страдают от слипшихся слов, и сравнение по словам такие дефекты
    ломают, а триграммы — нет.
    """
    ta, tb = normalize_name(a), normalize_name(b)
    if not ta or not tb:
        return 0.0
    ga, gb = _trigrams(ta), _trigrams(tb)
    jac = len(ga & gb) / len(ga | gb) if ga and gb else 0.0
    seq = SequenceMatcher(None, ta, tb).ratio()
    return max(jac, seq)


def best_name_match(object_name, zhk_list, threshold=NAME_MATCH_THRESHOLD,
                    same_bin=None):
    """
    Ищет ЖК по наименованию объекта из PDF.

    same_bin — БИН застройщика из PDF. Если задан, кандидаты сначала
    сужаются до ЖК с таким же customer_bin: одно и то же наименование
    используют десятки разных компаний, и без этого фильтра подбор
    уводит к чужому застройщику.
    """
    result = {"matched": False, "best": None, "similarity": 0.0,
              "matched_list": []}

    candidates = zhk_list or []
    if same_bin:
        narrowed = [z for z in candidates
                    if _clean_bin(z.get("customer_bin")) == same_bin]
        if narrowed:
            candidates = narrowed

    scored = []
    for zhk in candidates:
        clean = clean_zhk_name(zhk.get("name"))
        if not clean:
            continue
        score = name_similarity(object_name, clean)
        if score > 0:
            scored.append((score, zhk))

    if not scored:
        return result

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top_item = scored[0]

    result["best"] = top_item
    result["similarity"] = round(top_score, 3)
    result["matched"] = top_score >= threshold
    result["matched_list"] = [z for s, z in scored if s >= threshold]
    return result


def best_address_match(query_address, items, address_field="address",
                       threshold=ADDRESS_MATCH_THRESHOLD, weights=None):
    """
    Сравнивает адрес из запроса с адресами, извлечёнными из уведомлений.

    Оценка — взвешенное сходство (см. weighted_similarity): общие для
    района слова почти не влияют, определяет название улицы. Устойчива
    к различиям в формулировках: «Республика Казахстан, город Астана,
    район Сарайшық, улица Х» и «город Астана, район «Сарайшық», ул. Х»
    дадут высокий балл, а чужой район — низкий.

    Возвращает:
        matched      — есть ли совпадение выше порога
        best         — лучший кандидат (или None)
        similarity   — его оценка 0..1
        matched_list — все кандидаты выше порога, по убыванию оценки
    """
    result = {"matched": False, "best": None, "similarity": 0.0,
              "matched_list": []}

    if not query_address or not items:
        return result

    scorer = (lambda x, y: weighted_similarity(x, y, weights)) if weights \
        else address_similarity

    scored = []
    for item in items:
        candidate = item.get(address_field)
        if not candidate:
            continue
        score = scorer(query_address, candidate)
        if score > 0:
            scored.append((score, item))

    if not scored:
        return result

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top_item = scored[0]

    result["best"] = top_item
    result["similarity"] = round(top_score, 3)
    result["matched"] = top_score >= threshold
    result["matched_list"] = [item for score, item in scored
                              if score >= threshold]

    return result


def match_notifications(notifications, zhk_list, geocode=None,
                        radius_m=DEFAULT_RADIUS_M, bin_index=None):
    """
    Сопоставляет уведомления (плоский список, у каждого есть 'bin')
    с ЖК на карте.

    Возвращает:
        by_zhk    — {id ЖК: {bin, company, start_date, notifications[]}}
        unmatched — уведомления, к которым ЖК не нашлось
        stats     — сводка
    """
    if bin_index is None:
        bin_index = build_bin_index(zhk_list)

    by_zhk = {}
    unmatched = []
    stats = {"notifications": 0, "matched_name_bin": 0, "matched_name": 0,
             "matched_bin": 0, "matched_geocode": 0, "no_match": 0,
             "rejected_by_address": 0}

    for notif in notifications or []:
        stats["notifications"] += 1

        res = match_notification(notif, zhk_list, geocode, radius_m, bin_index)
        zhk = res.get("zhk")

        if zhk is None:
            stats["no_match"] += 1
            if res.get("rejected"):
                stats["rejected_by_address"] += 1
            unmatched.append({
                "bin": notif.get("applicant_bin") or notif.get("bin"),
                "nikad": notif.get("nikad"),
                "address": notif.get("address"),
                "object_name": notif.get("object_name"),
                "issue_date": notif.get("issue_date_iso"),
                "reason": res.get("rejected"),
            })
            continue

        stats[f"matched_{res['method']}"] += 1
        key = zhk.get("id")

        slot = by_zhk.setdefault(key, {
            "zhk_id": key,
            "zhk_name": zhk.get("name"),
            "zhk_address": zhk.get("address"),
            "bin": notif.get("applicant_bin") or notif.get("bin"),
            "company": notif.get("licensiat"),
            "method": res["method"],
            "score": res.get("score"),
            "bin_verified": res.get("bin_verified", False),
            "start_date": None,
            "notifications": [],
        })

        slot["notifications"].append({
            "nikad": notif.get("nikad"),
            "issue_date": notif.get("issue_date_iso"),
            "received_date": notif.get("received_date"),
            "address": notif.get("address"),
            "object_name": notif.get("object_name"),
            "status": notif.get("status"),
            "detail_url": notif.get("detail_url"),
        })

        # Дата начала стройки — самая ранняя дата выдачи
        d = notif.get("issue_date_iso")
        if d and (slot["start_date"] is None or d < slot["start_date"]):
            slot["start_date"] = d

    # Хронология внутри каждого ЖК
    for slot in by_zhk.values():
        slot["notifications"].sort(
            key=lambda n: (n.get("issue_date") or "9999", n.get("nikad") or ""))

    return {
        "by_zhk": by_zhk,
        "unmatched": unmatched,
        "stats": stats,
    }