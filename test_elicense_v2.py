import time
import os
import ddddocr
from playwright.sync_api import sync_playwright

def test_captcha_accuracy(bin_number, iterations=100):
    ocr = ddddocr.DdddOcr(beta=True)
    
    captcha_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captchas")
    os.makedirs(captcha_dir, exist_ok=True)
    
    success = 0
    failed = 0
    errors = 0
    
    with sync_playwright() as p:
        # Один браузер на все итерации — быстрее и меньше подозрений
        browser = p.chromium.launch(headless=False)
        
        for i in range(iterations):
            print(f"\n=== Прогон {i+1}/{iterations} ===")
            
            try:
                # Новая сессия на каждый прогон (новые cookie)
                context = browser.new_context()
                page = context.new_page()
                
                page.goto(
                    "https://elicense.kz/LicensingContent/SimpleSearchLicense?isExtended=true",
                    wait_until="domcontentloaded"
                )
                
                # Заполняем БИН
                page.fill('input#BinStr', bin_number)
                
                # Ждём появления капчи
                captcha_img = page.locator('#ExampleCaptcha_CaptchaImage')
                captcha_img.wait_for(state="visible", timeout=10000)
                
                # Небольшая пауза после загрузки — картинка может дорисовываться
                page.wait_for_timeout(500)
                
                # Скачиваем байты капчи через контекст браузера (надёжнее скриншота)
                from urllib.parse import urljoin
                captcha_src = captcha_img.get_attribute("src")
                captcha_url = urljoin(page.url, captcha_src)
                img_bytes = page.request.get(captcha_url).body()
                
                # Распознаём
                captcha_text = ocr.classification(img_bytes)
                print(f"  OCR распознал: {captcha_text!r}")
                
                # Сохраняем картинку для отладки
                safe = "".join(c for c in captcha_text if c.isalnum()) or "empty"
                file_path = os.path.join(captcha_dir, f"run_{i+1:03d}_{safe}.png")
                with open(file_path, "wb") as f:
                    f.write(img_bytes)
                
                # Заполняем и отправляем
                page.fill('#Captcha', captcha_text)
                page.click('input[type="submit"]')
                
                # Ждём загрузки результата
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1000)   # дать JS отрисовать сообщение
                
                # Проверяем результат
                is_failed, msg = is_captcha_failed(page)
                
                if is_failed:
                    failed += 1
                    print(f"  ✗ Капча не решена: {msg}")
                elif page.locator('table#table__content__body').count() > 0:
                    success += 1
                    print(f"  ✓ Успех! Таблица с результатами появилась")
                else:
                    # Ни ошибки капчи, ни таблицы — что-то другое (возможно, "не найдено")
                    body_text = page.locator('body').inner_text()
                    if "не найдено" in body_text.lower() or "табылған жоқ" in body_text:
                        success += 1   # капча прошла, просто нет результатов
                        print(f"  ✓ Капча прошла, но результатов нет")
                    else:
                        errors += 1
                        print(f"  ? Неизвестный результат")
                
                context.close()
                
                # Пауза между прогонами — чтобы не долбить сервер
                time.sleep(3)
                
            except Exception as e:
                errors += 1
                print(f"  ! Ошибка: {e}")
                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(5)
        
        browser.close()
    
    # Итоговая статистика
    print(f"\n{'='*50}")
    print(f"Итого прогонов: {iterations}")
    print(f"  ✓ Успешно:     {success}  ({success/iterations*100:.1f}%)")
    print(f"  ✗ Не решено:   {failed}   ({failed/iterations*100:.1f}%)")
    print(f"  ! Ошибок:      {errors}   ({errors/iterations*100:.1f}%)")
    print(f"Картинки сохранены в: {captcha_dir}")

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
    test_captcha_accuracy(bin_number="250640027866", iterations=10)
