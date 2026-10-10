from bs4 import BeautifulSoup
import re


def parse_elicense_results(html):
    """
    Принимает HTML страницы с результатами поиска elicense.kz.
    Возвращает список словарей с уведомлениями о начале СМР.
    """
    soup = BeautifulSoup(html, "lxml")

    # Находим таблицу результатов
    table = soup.find("table", id="table__content__body")
    if not table:
        print("Таблица результатов не найдена")
        return []

    results = []

    # Пропускаем заголовочную строку <tr> с <th>
    rows = table.find_all("tr")
    for row in rows:
        # Пропускаем строки с заголовками (там есть <th>)
        if row.find("th"):
            continue

        # Извлекаем все ячейки
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # --- Столбец 3: Вид деятельности ---
        # Ищем блок "Вид деятельности" и берём текст после него
        activity = _extract_field(cells[2], "Вид деятельности")
        if not activity:
            continue

        # Фильтруем только уведомления о начале СМР
        if "Уведомление о начале производства строительно-монтажных работ" not in activity:
            continue

        # --- Столбец 2: номер, дата ---
        nikad     = _extract_field(cells[1], "НИКАД:")
        uniq_num  = _extract_field(cells[1], "Уникальный номер документа:")
        issue_dt  = _extract_field(cells[1], "Дата выдачи:")
        app_nikad = _extract_field(cells[1], "НИКАД заявления:")

        # --- Столбец 3: лицензиар и лицензиат ---
        licensiar  = _extract_field(cells[2], "Лицензиар:")
        licensiat  = _extract_field(cells[2], "Лицензиат:")

        # --- Столбец 4: статус и ссылка ---
        status = None
        detail_url = None
        link = cells[3].find("a")
        if link:
            detail_url = link.get("href")
            status_span = link.find("span")
            if status_span:
                status = status_span.get_text(strip=True)

        results.append({
            "nikad":              nikad,
            "unique_number":      uniq_num,
            "issue_date":         issue_dt,
            "application_nikad":  app_nikad,
            "licensiar":          licensiar,
            "licensiat":          licensiat,
            "activity":           activity,
            "status":             status,
            "detail_url":         detail_url,
        })

    return results


def _extract_field(cell, title):
    """
    Внутри ячейки ищет <div class="row_result_field">, где
    <div class="row__content__title"> = title,
    и возвращает текст следующего <div>.
    """
    for field_div in cell.find_all("div", class_="row_result_field"):
        title_div = field_div.find("div", class_="row__content__title")
        if not title_div:
            continue
        if title_div.get_text(strip=True) != title:
            continue
        # Значение — следующий <div> после title
        value_div = title_div.find_next_sibling("div")
        if value_div:
            return value_div.get_text(strip=True)
    return None