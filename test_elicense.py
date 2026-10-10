from playwright.sync_api import sync_playwright
import time
import ddddocr

def search_elicense_manual(bin_number):
    url = "https://elicense.kz/LicensingContent/SimpleSearchLicense?isExtended=true&lang=ru"
    ocr = ddddocr.DdddOcr()
    
    with sync_playwright() as p:
        # headless=False — открывает видимое окно браузера
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        
        # 1. Открываем страницу расширенного поиска
        page.goto(url, wait_until="domcontentloaded")
        print("Страница загружена. Заполняем БИН...")
        
        # 2. Заполняем поле БИН (id="BinStr")
        page.fill('input#BinStr', bin_number)

        # 3. Ищем капчу и делаем скриншот с ней
        captcha = page.locator('#ExampleCaptcha_CaptchaImage')
        captcha.wait_for(timeout=5000)  # Ждём загрузки
        img_bytes = captcha.screenshot(path="./captcha.png")

        # 4. Распознаём текст капчи локально
        captcha_text = ocr.classification(img_bytes)
        print(f"Распознано: {captcha_text}")

        # 5. Заполняем поле капчи
        page.fill('#Captcha', captcha_text)

        # 6. Отправляем форму
        page.click('input[type="submit"]')  # Кнопка "Поиск"

        time.sleep(2)

        if is_captcha_failed(page):
            print("failed")
        else:
            print("solved")

        # 7. Ждём появления таблицы с результатами
        page.wait_for_selector('table#table__content__body', timeout=5000)
              
        # 8. Сохраняем HTML-код страницы в файл
        # html = page.content()
        # with open("elicense_result.html", "w", encoding="utf-8") as f:
        #     f.write(html)
        
        # 9. Также делаем скриншот для наглядности
        # page.screenshot(path="elicense_result.png", full_page=True)
        
        print("Готово! Файлы elicense_result.html и elicense_result.png сохранены.")
        
        # Небольшая пауза, чтобы вы увидели результат
        # time.sleep(3)
        
        page.close()
        browser.close()
        
        return html

def is_captcha_failed(page):
    """Проверяет, провалилась ли капча на elicense.kz."""
    try:
    # Ищем сообщение об ошибке капчи в блоке с классом new-container
        el = page.locator('.tab div[style*="color: red"], .tab div[style*="color:red"]')
        if el.count() > 0:
            text = el.first.inner_text().strip()
            if "капчи" in text.lower() or "капча" in text.lower():
                return True, text
    except Exception:
        pass
    return False, ""


if __name__ == "__main__":
    # ⚠️ ЗАМЕНИТЕ на реальный БИН застройщика, который вы хотите проверить
    test_bin = "250640027866"  # Пример: QazDome Systems
    search_elicense_manual(test_bin)
