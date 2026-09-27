import os, time, json, logging, traceback, threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from browser import (
    get_lancers_jobs,
    get_cw_jobs,
    get_coconala_jobs,
    login,
    init_driver,
    apply_fast_chrome_options,
    harden_driver,
)
from notifySlack import notify_slack
from translate import translate_to_english, is_japanese_text
from selenium.webdriver import ChromeOptions
from selenium.webdriver import Chrome

TARGET_URLS_Lancers = {
    "Lancers_web":    "https://www.lancers.jp/work/search/web?open=1",
    "Lancers_system": "https://www.lancers.jp/work/search/system?open=1"
}

TARGET_URLS_CW = {
    "CW_web": "https://crowdworks.jp/public/jobs/search?category_id=230&order=new",
    "CW_system": "https://crowdworks.jp/public/jobs/search?category_id=226&order=new",
    "CW_AI": "https://crowdworks.jp/public/jobs/search?category_id=311&order=new",
    "CW_Android": "https://crowdworks.jp/public/jobs/search?category_id=242&order=new"
}

TARGET_URLS_Coconala = {
    "Coconala_web": "https://coconala.com/requests/categories/22?categoryId=22&page=1&recruiting=true",
    "Coconala_system": "https://coconala.com/requests/categories/11?categoryId=11&page=1&recruiting=true",
    "Coconala_AI": "https://coconala.com/requests/categories/28?categoryId=28&page=1&recruiting=true"
}

POLL_PAUSE_SECONDS = 3
PROFILE_LANCERS = "chrome_profile_lancers_login"
PROFILE_CW = "chrome_profile_cw"
PROFILE_Coconala = "chrome_profile_coconala"

if not os.path.exists('seen.json'):
    with open('seen.json','w') as f: f.write('[]')

def load_seen():
    if not os.path.exists("seen.json"):
        return []

    with open("seen.json", "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

def save_seen(data):
    with open("seen.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

def alert_new_job(dtype, price, title, url):
    try:
        if is_japanese_text(title):
            title = translate_to_english(title)
        notify_slack(dtype, price, title, url)
    except Exception as e:
        print(f"Alert failed: {e}")
        logging.error(f"Alert failed: {e}")

def fetch_site_jobs(driver, targets, getter, seen_ids):
    jobs = []
    for dtype, url in targets.items():
        try:
            jobs.extend(getter(driver, url, dtype, seen_ids))
        except Exception as e:
            print(f"⚠️ Error fetching {dtype} jobs: {e}")
            logging.error(f"⚠️ Error fetching {dtype} jobs: {e}")
    return jobs

def job_check(lancers_driver, cw_driver, coconala_driver, seen, seen_ids, index):
    print(f"{index} Checking for new jobs...")

    with ThreadPoolExecutor(max_workers=3) as pool:
        lancers_future = pool.submit(
            fetch_site_jobs, lancers_driver, TARGET_URLS_Lancers, get_lancers_jobs, seen_ids
        )
        cw_future = pool.submit(
            fetch_site_jobs, cw_driver, TARGET_URLS_CW, get_cw_jobs, seen_ids
        )
        coconala_future = pool.submit(
            fetch_site_jobs, coconala_driver, TARGET_URLS_Coconala, get_coconala_jobs, seen_ids
        )
        new = lancers_future.result() + cw_future.result() + coconala_future.result()

    added = 0
    for job in new:
        dtype, jid, job_type, title, price, url = (
            job["dtype"], str(job["id"]), job["type"], job["title"], job["price"], job["url"]
        )

        if jid in seen_ids:
            continue

        print("✨NEW JOB✨")
        print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Type: {dtype}")
        print(f"ID: {jid}")
        print(f"Price: {price}")
        print(f"Job Type: {job_type}")
        print(f"Title: {title}")
        print(f"URL: {url}")
        print("-----------------------------------------------")

        threading.Thread(
            target=alert_new_job,
            args=(dtype, price, title, url),
            daemon=True,
        ).start()

        seen.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dtype": dtype,
            "id": jid,
            "type": job_type,
            "title": title,
            "url": url,
            "price": price,
        })
        seen_ids.add(jid)
        added += 1

    if added:
        save_seen(seen)

    print(f"{index} checked ({added} new)")

def create_proxy_auth_extension(proxy_address, username, password):
    """
    Creates a Chrome extension for proxy authentication.
    """
    import zipfile

    proxy_host, proxy_port = proxy_address.split(":")

    manifest_json = """
    {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Authentication",
        "permissions": [
            "proxy",
            "tabs",
            "unlimitedStorage",
            "storage",
            "<all_urls>"
        ],
        "background": {
            "scripts": ["background.js"]
        }
    }
    """

    background_js = f"""
    var config = {{
        mode: "fixed_servers",
        rules: {{
            singleProxy: {{
                scheme: "http",
                host: "{proxy_host}",
                port: parseInt("{proxy_port}")
            }},
            bypassList: ["localhost"]
        }}
    }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
    chrome.webRequest.onAuthRequired.addListener(
        function(details) {{
            return {{
                authCredentials: {{
                    username: "{username}",
                    password: "{password}"
                }}
            }};
        }},
        {{urls: ["<all_urls>"]}},
        ["blocking"]
    );
    """

    plugin_file = "proxy_auth_plugin.zip"
    with zipfile.ZipFile(plugin_file, "w") as zp:
        zp.writestr("manifest.json", manifest_json)
        zp.writestr("background.js", background_js)

    return plugin_file

def init_driver_with_proxy(profile_dir=None, allow_media=False):
    proxy_address = os.getenv("proxy_address")
    proxy_username = os.getenv("proxy_username")
    proxy_password = os.getenv("proxy_password")

    if not proxy_address or not proxy_username or not proxy_password:
        raise ValueError("Proxy details are missing in the .env file.")

    chrome_options = ChromeOptions()
    apply_fast_chrome_options(chrome_options, profile_dir=profile_dir, allow_media=allow_media)
    chrome_options.add_argument(f"--proxy-server=http://{proxy_address}")
    plugin_file = create_proxy_auth_extension(proxy_address, proxy_username, proxy_password)
    chrome_options.add_extension(plugin_file)
    return harden_driver(
        Chrome(options=chrome_options),
        block_media=not allow_media,
        page_load_timeout=180 if allow_media else 20,
    )

def create_driver(profile_dir, allow_media=False):
    proxy_address = os.getenv("proxy_address")
    proxy_username = os.getenv("proxy_username")
    proxy_password = os.getenv("proxy_password")
    if proxy_address and proxy_username and proxy_password:
        return init_driver_with_proxy(profile_dir=profile_dir, allow_media=allow_media)
    return init_driver(profile_dir=profile_dir, allow_media=allow_media)

def quit_drivers(*drivers):
    for driver in drivers:
        if driver is None:
            continue
        try:
            driver.quit()
        except Exception:
            pass

def main():
    load_dotenv()

    lancers_driver = None
    cw_driver = None
    coconala_driver = None
    try:
        if not (os.getenv("proxy_address") and os.getenv("proxy_username") and os.getenv("proxy_password")):
            print("⚠️ Proxy env vars not set; starting Chrome without a proxy.")

        # Only open Lancers first so the captcha has one usable window.
        lancers_driver = create_driver(PROFILE_LANCERS, allow_media=True)
        login(lancers_driver, os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        harden_driver(lancers_driver, block_media=True)
        cw_driver = create_driver(PROFILE_CW)
        coconala_driver = create_driver(PROFILE_Coconala, allow_media=True)

        seen = load_seen()
        seen_ids = {str(item.get("id")) for item in seen if item.get("id") is not None}
        index = 0

        while True:
            try:
                job_check(lancers_driver, cw_driver, coconala_driver, seen, seen_ids, index)
                index += 1
                time.sleep(POLL_PAUSE_SECONDS)
            except Exception as e:
                print(f"❌ Error in main loop: {e}")
                traceback.print_exc()
                logging.error(f"❌ Error in main loop: {e}")
                quit_drivers(lancers_driver, cw_driver, coconala_driver)
                os._exit(1)

    except Exception as e:
        print(f"❌ Critical error in main: {e}")
        logging.error(f"❌ Critical error in main: {e}")
        quit_drivers(lancers_driver, cw_driver, coconala_driver)
        os._exit(1)
    finally:
        quit_drivers(lancers_driver, cw_driver, coconala_driver)

if __name__ == "__main__":
    main()
