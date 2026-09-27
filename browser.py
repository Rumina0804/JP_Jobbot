from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
import re
import time
from selenium.webdriver.chrome.options import Options
import logging
import json
from selenium.common.exceptions import TimeoutException, WebDriverException

PAGE_WAIT = 8

def apply_fast_chrome_options(options, profile_dir=None, allow_media=False):
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-gpu")
    prefs = {"profile.default_content_setting_values.notifications": 2}
    if allow_media:
        # Full page load so the AWS WAF captcha scripts and fonts can attach.
        options.page_load_strategy = "normal"
    else:
        options.add_argument("--blink-settings=imagesEnabled=false")
        prefs["profile.managed_default_content_settings.images"] = 2
        options.page_load_strategy = "none"
    options.add_experimental_option("prefs", prefs)
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={os.path.abspath(profile_dir)}")

def harden_driver(driver, block_media=True, page_load_timeout=20):
    driver.set_page_load_timeout(page_load_timeout)
    driver.set_script_timeout(30)
    if not block_media:
        return driver
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.ico",
                "*.woff", "*.woff2", "*.ttf", "*.eot",
            ]
        })
    except Exception:
        pass
    return driver

def init_driver(profile_dir=None, allow_media=False):
    options = Options()
    apply_fast_chrome_options(options, profile_dir=profile_dir, allow_media=allow_media)
    driver = webdriver.Chrome(service=Service(), options=options)
    if allow_media:
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })
        except Exception:
            pass
    return harden_driver(
        driver,
        block_media=not allow_media,
        page_load_timeout=180 if allow_media else 20,
    )

def already_logged_in(driver):
    try:
        return (driver.current_url or "").startswith("https://www.lancers.jp/mypage")
    except WebDriverException:
        return False

def login(driver, email, password):
    logging.info("Navigating to login page...")
    print("Navigating to login page...")
    try:
        driver.get("https://www.lancers.jp/user/login?ref=header_menu")
    except TimeoutException:
        print("Login page load timed out; continuing if the window is usable.")

    def email_field_ready(d):
        return bool(d.find_elements(By.ID, "UserEmail"))

    # Saved session may already be on mypage (no captcha, no login form).
    try:
        WebDriverWait(driver, 15).until(
            lambda d: already_logged_in(d)
            or email_field_ready(d)
            or "Human Verification" in (d.title or "")
            or bool(d.find_elements(By.ID, "captcha-container"))
        )
    except (TimeoutException, WebDriverException):
        pass

    if already_logged_in(driver):
        logging.info("✅ Already logged in.")
        print("✅ Already logged in.")
        return

    captcha_shown = False
    try:
        captcha_shown = "Human Verification" in (driver.title or "") or bool(
            driver.find_elements(By.ID, "captcha-container")
        )
    except WebDriverException:
        captcha_shown = True

    if captcha_shown or not email_field_ready(driver):
        print("Lancers showed a human verification captcha.")
        print("Use the Lancers Chrome window, click Begin, and finish the puzzle.")
        print("Waiting up to 5 minutes for the login form or mypage...")
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                if already_logged_in(driver):
                    logging.info("✅ Already logged in.")
                    print("✅ Already logged in.")
                    return
                if email_field_ready(driver):
                    print("Captcha passed. Continuing login.")
                    break
            except WebDriverException:
                pass
            time.sleep(1)
        else:
            raise TimeoutException("Login form did not appear after captcha wait.")

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.ID, "UserEmail"))
    ).send_keys(email)

    driver.find_element(By.ID, "UserPassword").send_keys(password)
    driver.find_element(By.ID, "form_submit").click()

    try:
        WebDriverWait(driver, 10).until(already_logged_in)
        logging.info("✅ Login successful.")
        print("✅ Login successful.")
    except:
        logging.error("❌ Login failed or took too long.")
        print("❌ Login failed or took too long.")
        raise


def get_lancers_jobs(driver, url, dtype, seen_ids=None):
    print(f"Getting {dtype} jobs from Lancers...")
    seen_ids = seen_ids or set()
    try:
        driver.get(url)
    except TimeoutException:
        print("⚠️ Job list page load exceeded timeout, proceeding by stopping load.")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    try:
        WebDriverWait(driver, PAGE_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".p-search-job-medias--lancer, .p-search-job-media"))
        )
    except TimeoutException:
        print("No jobs found or page failed to load")
        return []

    jobs = []
    for card in driver.find_elements(By.CSS_SELECTOR, ".p-search-job-media.c-media.c-media--item "):
        onclick_str = card.get_attribute("onclick")
        match = None
        if onclick_str:
            match = re.search(r"goToLjpWorkDetail\((\d+)\)", onclick_str)            
            
        if not match:
            continue

        jid = match.group(1)
        if str(jid) in seen_ids:
            break

        title_element = card.find_element(By.CSS_SELECTOR, ".p-search-job-media__title.c-media__title")

        title_text = title_element.get_attribute("textContent").strip()
        # Remove known tag texts if necessary (optional cleanup)
        title_lines = [line.strip() for line in title_text.split('\n') if line.strip()]
        title = title_lines[-1]  # Usually the actual title is last

        job_type = card.find_element(By.CSS_SELECTOR, ".c-badge__text").text.strip()

        # Get price range
        price_element = card.find_element(By.CSS_SELECTOR, ".p-search-job-media__price")
        price_numbers = price_element.find_elements(By.CSS_SELECTOR, ".p-search-job-media__number")
        if len(price_numbers) == 2:
            price_min = price_numbers[0].text.strip()
            price_max = price_numbers[1].text.strip()
            price_range = f"{price_min} ~ {price_max}"
        else:
            price_range = price_numbers[0].text.strip() if price_numbers else "N/A"
        
        link = f"https://www.lancers.jp/work/detail/{jid}"
        # link = card.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
        if(job_type != "求人" and job_type != "コンペ"):
            jobs.append({"dtype": dtype, "id": jid, "type": job_type, "title": title, "price": price_range, "url": link})
        else:
            continue
    print(f"Found {len(jobs)} {dtype} jobs from Lancers.")
    return jobs

def get_cw_jobs(driver, url, dtype, seen_ids=None):
    print(f"Getting {dtype} jobs from Crowdworks...")
    seen_ids = seen_ids or set()
    try:
        driver.get(url)
    except TimeoutException:
        print("⚠️ Job list page load exceeded timeout, proceeding by stopping load.")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    def cw_data_ready(d):
        els = d.find_elements(By.ID, "vue-container")
        if not els:
            return False
        data = els[0].get_attribute("data")
        return bool(data and data.strip())

    try:
        WebDriverWait(driver, PAGE_WAIT).until(cw_data_ready)
        container = driver.find_element(By.ID, "vue-container")
        data_json = container.get_attribute("data")
        if not data_json or not data_json.strip():
            print(f"⚠️ No data attribute found in vue-container for {dtype} after waiting. Skipping this source.")
            return []
        data = json.loads(data_json)
    except TimeoutException:
        print(f"⚠️ Timeout waiting for Crowdworks page to load for {dtype}. Skipping this source.")
        return []
    except Exception as e:
        print(f"⚠️ Error parsing Crowdworks data for {dtype}: {e}. Skipping this source.")
        return []

    # Safely extract job offers
    try:
        if "searchResult" not in data or "job_offers" not in data["searchResult"]:
            print(f"⚠️ No job_offers found in Crowdworks data for {dtype}. Skipping this source.")
            return []
        job_offers = data["searchResult"]["job_offers"]
    except Exception as e:
        print(f"⚠️ Error extracting job offers for {dtype}: {e}. Skipping this source.")
        return []

    jobs = []
    for offer in job_offers:
        try:
            job = offer["job_offer"]
            job_id = job["id"]
            title = job["title"]
            # Payment info
            payment = offer.get("payment", {})
            price_range = "discuss"
            if "fixed_price_payment" in payment:
                min_budget = payment["fixed_price_payment"].get("min_budget")
                max_budget = payment["fixed_price_payment"].get("max_budget")
                if min_budget and max_budget:
                    price_range = f"{int(min_budget)} ~ {int(max_budget)}"
                elif max_budget:
                    price_range = f"{int(max_budget)}"
            elif "hourly_payment" in payment:
                min_wage = payment["hourly_payment"].get("min_hourly_wage")
                max_wage = payment["hourly_payment"].get("max_hourly_wage")
                if min_wage and max_wage:
                    price_range = f"{int(min_wage)} ~ {int(max_wage)} (hourly)"
                elif max_wage:
                    price_range = f"{int(max_wage)} (hourly)"
            if str(job_id) in seen_ids:
                break
            link = f"https://crowdworks.jp/public/jobs/{job_id}"
            jobs.append({
                "dtype": dtype,
                "id": str(job_id),
                "type": "not_specified",
                "title": title,
                "price": price_range,
                "url": link
            })
        except Exception as e:
            print(f"⚠️ Error processing individual job offer for {dtype}: {e}. Skipping this job.")
            continue
    
    print(f"Found {len(jobs)} {dtype} jobs from Crowdworks.")
    return jobs

def get_coconala_jobs(driver, url, dtype, seen_ids=None):
    print(f"Getting {dtype} jobs from Coconala...")
    seen_ids = seen_ids or set()
    try:
        driver.get(url)
    except TimeoutException:
        print("⚠️ Job list page load exceeded timeout, proceeding by stopping load.")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    try:
        WebDriverWait(driver, PAGE_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".c-searchItemWrapper"))
        )
    except TimeoutException:
        print(f"No Coconala jobs found or page failed to load for {dtype}")
        return []

    jobs = []
    for card in driver.find_elements(By.CSS_SELECTOR, ".c-searchItemWrapper"):
        try:
            link_el = card.find_elements(By.CSS_SELECTOR, "a.c-searchItem_detailLink[href*='/requests/']")
            href = link_el[0].get_attribute("href") if link_el else ""
            match = re.search(r"/requests/(\d+)", href or "")
            if not match:
                title_link = card.find_elements(By.CSS_SELECTOR, ".c-itemInfo_title a[href*='/requests/']")
                href = title_link[0].get_attribute("href") if title_link else ""
                match = re.search(r"/requests/(\d+)", href or "")
            if not match:
                continue

            jid = f"coconala_{match.group(1)}"
            if jid in seen_ids:
                break

            title_el = card.find_elements(By.CSS_SELECTOR, ".c-itemInfo_title")
            title = title_el[0].get_attribute("textContent").strip() if title_el else ""
            if not title:
                continue

            job_type_el = card.find_elements(By.CSS_SELECTOR, ".c-itemInfo_category")
            job_type = job_type_el[0].get_attribute("textContent").strip() if job_type_el else "request"

            price_el = card.find_elements(By.CSS_SELECTOR, ".d-requestBudget")
            if price_el:
                price = " ".join(price_el[0].get_attribute("textContent").split())
            else:
                price = "N/A"

            jobs.append({
                "dtype": dtype,
                "id": jid,
                "type": job_type or "request",
                "title": title,
                "price": price or "N/A",
                "url": f"https://coconala.com/requests/{match.group(1)}",
            })
        except Exception as e:
            print(f"⚠️ Error processing Coconala card for {dtype}: {e}. Skipping this job.")
            continue

    print(f"Found {len(jobs)} {dtype} jobs from Coconala.")
    return jobs

def get_description(driver, url):
    try:
        driver.get(url)
    except TimeoutException:
        print("⚠️ Job description page load exceeded timeout, proceeding by stopping load.")
        driver.execute_script("window.stop();")

    # Get job description text
    description_element = WebDriverWait(driver, 60).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".p-work-detail-lancer__postscript-description"))
    )
    description_text = description_element.get_attribute("textContent").strip()

    # Get apply URL
    apply_element = WebDriverWait(driver, 60).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='propose_start']"))
    )
    apply_url = apply_element.get_attribute("href")
    
    # Generate proposal using the job info and description
    job_info = { 
        "description": description_text,
        "apply_url": apply_url
    }

    return job_info
    # proposal_text = generate_proposal(job_info)
    # # Replace selectors as needed
    # driver.find_element(By.NAME, "proposal").send_keys(proposal_text)
    # driver.find_element(By.NAME, "price").send_keys(price)
    # driver.find_element(By.NAME, "deadline").send_keys(deadline)
    # driver.find_element(By.CSS_SELECTOR, "button.send-bid").click()

def submit_bid(driver, url, proposal):
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "textarea[name='proposal']")))
    driver.find_element(By.CSS_SELECTOR, "textarea[name='proposal']").send_keys(proposal)
    driver.find_element(By.CSS_SELECTOR, "button.send-bid").click()


