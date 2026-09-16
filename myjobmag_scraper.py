import os
import re
import csv
import time
import base64
import hashlib
import logging
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
BASE_URL     = "https://www.myjobmag.com"
START_PAGE   = 3      # scrape starts here
END_PAGE     = 1      # and goes down to (and including) here
PAGE_RANGE   = range(START_PAGE, END_PAGE - 1, -1)   # 3, 2, 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36"
    ),
    "Accept-Charset": "utf-8",
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_TIMEOUT = 20
PROCESSED_IDS_FILE = "processed.csv"

# ── WordPress ─────────────────────────────────────────────────────────────────
WP_URL      = os.environ.get("WP_BASE_URL", "")
WP_USER     = os.environ.get("WP_USERNAME", "")
WP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

WP_BASE        = WP_URL.rstrip("/")
WP_JOBS_URL    = f"{WP_BASE}/job-listings"
WP_COMPANY_URL = f"{WP_BASE}/companies"
WP_MEDIA_URL   = f"{WP_BASE}/media"

JOB_TYPE_MAPPING = {
    "full-time": "full-time", "full time": "full-time", "fulltime": "full-time",
    "part-time": "part-time", "part time": "part-time", "parttime": "part-time",
    "contract": "contract", "contractor": "contract", "contracting": "contract",
    "temporary": "temporary", "temp": "temporary",
    "freelance": "freelance",
    "internship": "internship", "intern": "internship",
    "volunteer": "volunteer",
}

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
_fh = logging.FileHandler("debug.log")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_ch)


def require_wp_config():
    missing = [n for n, v in
               [("WP_BASE_URL", WP_URL), ("WP_USERNAME", WP_USER), ("WP_APP_PASSWORD", WP_PASSWORD)]
               if not v]
    if missing:
        raise EnvironmentError(
            f"\n❌ Missing required environment variable(s): {', '.join(missing)}\n"
            f"   Fix: GitHub → Settings → Secrets and variables → Actions → New secret\n"
        )


# ════════════════════════════════════════════════════════════════════════════
# SANITIZATION
# ════════════════════════════════════════════════════════════════════════════
_MOJIBAKE = [
    ("Â", ""), ("â€™", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€", '"'),
    ("â€¢", "•"), ("Ã©", "é"), ("Ã ", "à"), ("Ã¨", "è"), ("Ã¯", "ï"),
    ("\u00a0", " "), ("\u200b", ""), ("\ufeff", ""),
]


def sanitize(value: str) -> str:
    if not isinstance(value, str):
        return value
    text = value
    for pattern, repl in _MOJIBAKE:
        text = text.replace(pattern, repl)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = re.sub(r"^N/A$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bN/A\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalise_job_type(raw: str) -> str:
    return JOB_TYPE_MAPPING.get((raw or "").lower().strip(), "full-time")


def add_three_months(posted_date: datetime) -> str:
    month = posted_date.month - 1 + 3
    year = posted_date.year + month // 12
    month = month % 12 + 1
    day = min(posted_date.day, 28)
    return datetime(year, month, day).strftime("%Y-%m-%d")


def make_job_id(job_url: str) -> str:
    return hashlib.md5(job_url.encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════════════
# DEDUP TRACKER
# ════════════════════════════════════════════════════════════════════════════
def _init_tracker():
    if not os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Job ID", "Job URL", "Job Title", "Status", "Timestamp"])


def load_processed_ids() -> set:
    _init_tracker()
    with open(PROCESSED_IDS_FILE, newline="", encoding="utf-8") as f:
        return {row["Job ID"] for row in csv.DictReader(f)}


def mark_processed(job_id: str, job_url: str, title: str, status: str):
    _init_tracker()
    with open(PROCESSED_IDS_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([job_id, job_url, title, status, datetime.now().isoformat()])


# ════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ════════════════════════════════════════════════════════════════════════════
def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def text_of(soup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""


# ════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ════════════════════════════════════════════════════════════════════════════
def scrape_job_list_page(page_num: int) -> list:
    url = f"{BASE_URL}/page/{page_num}"
    logger.info(f"Fetching page {page_num}: {url}")
    try:
        soup = get_soup(url)
    except Exception as e:
        logger.error(f"Error fetching page {page_num}: {e}")
        return []

    urls = []
    for a in soup.select("li.mag-b > h2 > a"):
        href = a.get("href")
        if href:
            urls.append(BASE_URL + href if href.startswith("/") else href)
    logger.info(f"Found {len(urls)} job URLs on page {page_num}")
    return urls


def scrape_company_details(company_url: str) -> dict:
    company = {
        "name": "", "logo": "", "industry": "", "founded": "",
        "type": "", "website": "", "address": "", "details": "",
    }
    if not company_url:
        return company
    try:
        soup = get_soup(company_url)
        company["name"] = text_of(soup, "#wrap-comp-jobs > div.company-jobs > h1").replace("Recruitment", "").strip()
        logo_el = soup.select_one("#wrap-comp-jobs > div.company-jobs > div.company-logo > img")
        if logo_el and logo_el.get("src"):
            src = logo_el["src"]
            company["logo"] = BASE_URL + src if src.startswith("/") else src
        company["industry"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(1) > span.comp-info-desc")
        company["founded"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(2) > span.comp-info-desc")
        company["type"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(3) > span.comp-info-desc")
        company["website"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(4) > span.comp-info-desc")
        company["address"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(5) > span.comp-info-desc")
        company["details"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.mag-b.fl-r.ts-13.tc-b6.bm-b-35")
        logger.info(f"Company scraped: {company['name']}")
    except Exception as e:
        logger.error(f"Company fetch failed for {company_url}: {e}")
    return company


def scrape_job_details(job_url: str) -> dict:
    soup = get_soup(job_url)

    job_title = text_of(soup, "h2.mag-b").replace("Method of Application", "").strip()

    printable_items = soup.select("#printable > ul > li")

    def item_info(idx):
        if idx < len(printable_items):
            span = printable_items[idx].select_one("span.jkey-info")
            return span.get_text(strip=True) if span else ""
        return ""

    job_type          = item_info(0)
    job_qualifications = item_info(1)
    job_experience     = item_info(2)
    job_location       = item_info(3)
    job_field          = item_info(4)

    job_salary = ""
    for li in soup.select("#printable ul li"):
        title_span = li.select_one("span.jkey-title")
        if title_span and title_span.get_text(strip=True) == "Salary Range":
            info_span = li.select_one("span.jkey-info")
            job_salary = info_span.get_text(strip=True) if info_span else ""
            break

    date_posted_str = text_of(soup, "#posted-date")
    date_posted = None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            date_posted = datetime.strptime(date_posted_str, fmt)
            break
        except ValueError:
            continue
    if date_posted is None:
        logger.warning(f"Invalid/unparseable date '{date_posted_str}' — skipping job: {job_url}")
        return None

    estimated_deadline = add_three_months(date_posted)

    deadline_raw = text_of(soup, "div.read-left-section > ul > li.read-head > div > div:nth-of-type(2)")
    deadline = deadline_raw.replace("Deadline:", "").strip()
    if not deadline or deadline.lower() == "not specified":
        deadline = estimated_deadline

    job_description_main = text_of(soup, "div.job-details")
    application_detail = text_of(soup, "#printable > div.mag-b.bm-b-30 > p")
    job_description = job_description_main + (("\n\n" + application_detail) if application_detail else "")

    application_block_text = ""
    app_block = soup.select_one("#printable > div.mag-b.bm-b-30")
    if app_block:
        application_block_text = app_block.get_text(" ", strip=True)
    email_match = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}", application_block_text)
    extracted_email = email_match.group(0) if email_match else ""

    app_link_el = soup.select_one("#printable > div.mag-b.bm-b-30 > a")
    app_link_text = app_link_el.get_text(strip=True) if app_link_el else ""
    last_part = app_link_text.split("on")[-1].strip() if app_link_text else ""

    company_urls = []
    for a in soup.select("#printable > a"):
        href = a.get("href")
        if href:
            company_urls.append(BASE_URL + href if href.startswith("/") else href)

    company = scrape_company_details(company_urls[0] if company_urls else "")
    if not company["details"] and company["name"]:
        company["details"] = search_company_details_fallback(company["name"])

    application = extracted_email or last_part or company["website"] or ""

    return {
        "job_title": sanitize(job_title),
        "job_type": sanitize(job_type),
        "job_qualifications": sanitize(job_qualifications),
        "job_experience": sanitize(job_experience),
        "job_location": sanitize(job_location),
        "job_field": sanitize(job_field),
        "date_posted": sanitize(date_posted_str),
        "deadline": sanitize(deadline),
        "job_description": sanitize(job_description),
        "application": sanitize(application),
        "company_url": sanitize(company_urls[0] if company_urls else ""),
        "company_name": sanitize(company["name"]),
        "company_logo": sanitize(company["logo"]),
        "company_industry": sanitize(company["industry"]),
        "company_founded": sanitize(company["founded"]),
        "company_type": sanitize(company["type"]),
        "company_website": sanitize(company["website"]),
        "company_address": sanitize(company["address"]),
        "company_details": sanitize(company["details"]),
        "job_url": sanitize(job_url),
        "estimated_deadline": sanitize(estimated_deadline),
        "salary_range": sanitize(job_salary),
    }


def search_company_details_fallback(company_name: str) -> str:
    try:
        url = "https://www.google.com/search?q=" + requests.utils.quote(company_name + " company about")
        soup = get_soup(url)
        snippet = (soup.select_one("div.BNeawe") or
                   soup.select_one("span.aCOpRe") or
                   soup.select_one("div.VwiC3b"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
    except Exception as e:
        logger.error(f"Google fallback failed for {company_name}: {e}")
    try:
        slug = re.sub(r"[^a-z0-9-]", "-", company_name.lower())
        url = f"https://www.linkedin.com/company/{slug}"
        soup = get_soup(url)
        snippet = (soup.select_one("p.core-section-container__info") or
                   soup.select_one("section.summary p"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
        meta = soup.find("meta", {"name": "description"})
        if meta and meta.get("content") and len(meta["content"]) > 20:
            return meta["content"]
    except Exception as e:
        logger.error(f"LinkedIn fallback failed for {company_name}: {e}")
    return ""


# ════════════════════════════════════════════════════════════════════════════
# WORDPRESS
# ════════════════════════════════════════════════════════════════════════════
def wp_headers() -> dict:
    token = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def upload_logo(logo_url: str):
    if not logo_url or not logo_url.startswith("http"):
        return None
    ext = logo_url.lower().rsplit(".", 1)[-1]
    if ext not in ("png", "jpg", "jpeg", "webp"):
        return None
    try:
        img = requests.get(logo_url, timeout=15)
        img.raise_for_status()
        headers = wp_headers()
        headers["Content-Disposition"] = f"attachment; filename={logo_url.split('/')[-1]}"
        headers["Content-Type"] = img.headers.get("content-type", "image/jpeg")
        r = requests.post(WP_MEDIA_URL, headers=headers, data=img.content,
                           auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        logger.error(f"Logo upload error: {e}")
        return None


def get_or_create_term(taxonomy_url: str, name: str):
    if not name or not name.strip():
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    try:
        r = requests.get(f"{taxonomy_url}?slug={slug}", headers=wp_headers(), timeout=10)
        terms = r.json()
        if isinstance(terms, list) and terms:
            return terms[0]["id"]
    except Exception:
        pass
    try:
        r = requests.post(taxonomy_url, json={"name": name, "slug": slug},
                           headers=wp_headers(), auth=(WP_USER, WP_PASSWORD), timeout=10)
        return r.json().get("id")
    except Exception as e:
        logger.error(f"Term create error '{name}': {e}")
        return None


def save_company(job: dict):
    name = job["company_name"]
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    try:
        r = requests.get(f"{WP_COMPANY_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Company exists: {name}")
            return posts[0]["id"]
    except Exception:
        pass

    attachment_id = upload_logo(job["company_logo"])
    payload = {
        "title": name,
        "content": job["company_details"],
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_company_name": name,
            "_company_logo": str(attachment_id) if attachment_id else "",
            "_company_industry": job["company_industry"],
            "_company_website": job["company_website"],
        },
    }
    try:
        r = requests.post(WP_COMPANY_URL, json=payload, headers=wp_headers(),
                           auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        post = r.json()
        logger.info(f"✅ Company posted: {name} → ID {post.get('id')}")
        return post.get("id")
    except Exception as e:
        logger.error(f"Company post error '{name}': {e}")
        return None


def save_job(job: dict):
    for jt_label in ["Full Time", "Part Time", "Contract", "Temporary", "Freelance", "Internship", "Volunteer"]:
        get_or_create_term(f"{WP_BASE}/job_listing_type", jt_label)

    title       = job["job_title"]
    description = job["job_description"]
    location    = job["job_location"] or "Nigeria"
    job_type_s  = normalise_job_type(job["job_type"])
    company     = job["company_name"]
    application = job["application"]
    deadline    = job["deadline"] or job["estimated_deadline"]

    is_email = bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", application))
    is_url_v = bool(re.match(r"^https?://[^\s]+$", application))
    if not (is_email or is_url_v):
        application = ""

    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())[:80]
    try:
        r = requests.get(f"{WP_JOBS_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Job already on WP: {title}")
            return posts[0]["id"], posts[0].get("link")
    except Exception:
        pass

    attachment_id     = upload_logo(job["company_logo"])
    region_term_id    = get_or_create_term(f"{WP_BASE}/job_listing_region", location)
    job_type_term_id  = get_or_create_term(f"{WP_BASE}/job_listing_type", job_type_s.replace("-", " ").title())

    payload = {
        "title": title,
        "content": description,
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_job_title":          title,
            "_job_location":       location,
            "_job_type":           job_type_s,
            "_job_description":    description,
            "_application":        application,
            "_job_expires":        deadline,
            "_company_name":       company,
            "_company_website":    job["company_website"],
            "_company_logo":       str(attachment_id) if attachment_id else "",
            "_company_industry":   job["company_industry"],
            "_company_address":    job["company_address"],
            "_company_founded":    job["company_founded"],
            "_company_type":       job["company_type"],
            "_job_qualifications": job["job_qualifications"],
            "_job_experiences":    job["job_experience"],
            "_job_field":          job["job_field"],
            "_job_source_url":     job["job_url"],
            "_job_salary":         job["salary_range"],
        },
    }
    if region_term_id:
        payload["job_listing_region"] = [region_term_id]
    if job_type_term_id:
        payload["job_listing_type"] = [job_type_term_id]

    for attempt in range(3):
        try:
            r = requests.post(WP_JOBS_URL, json=payload, headers=wp_headers(),
                               auth=(WP_USER, WP_PASSWORD), timeout=25)
            r.raise_for_status()
            post = r.json()
            logger.info(f"✅ Job posted: '{title}' → WP ID {post.get('id')}")
            return post.get("id"), post.get("link")
        except Exception as e:
            logger.error(f"Job post attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, None


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def run():
    require_wp_config()
    processed_ids = load_processed_ids()
    logger.info(f"📋 {len(processed_ids)} jobs already in tracker.")

    posted = skipped = failed = 0

    for page_num in PAGE_RANGE:
        job_urls = scrape_job_list_page(page_num)

        for j, job_url in enumerate(job_urls, start=1):
            job_id = make_job_id(job_url)
            logger.info(f"── Page {page_num} | Job {j}/{len(job_urls)}: {job_url}")

            if job_id in processed_ids:
                logger.info("⏭ SKIP — already processed.")
                skipped += 1
                continue

            try:
                job = scrape_job_details(job_url)
            except Exception as e:
                logger.error(f"Error scraping job {job_url}: {e}")
                mark_processed(job_id, job_url, "", "scrape_failed")
                processed_ids.add(job_id)
                failed += 1
                continue

            if job is None or not job["job_title"] or not job["job_description"]:
                logger.info("⏭ SKIP — missing title/description.")
                mark_processed(job_id, job_url, "", "skipped_incomplete")
                processed_ids.add(job_id)
                skipped += 1
                continue

            if job["company_name"]:
                save_company(job)

            post_id, post_url = save_job(job)
            if post_id:
                mark_processed(job_id, job_url, job["job_title"], f"posted|wp_id={post_id}|{post_url or ''}")
                posted += 1
                logger.info(f"✅ SUCCESS — WP ID={post_id} 🔗 {post_url}")
            else:
                mark_processed(job_id, job_url, job["job_title"], "wp_post_failed")
                failed += 1
                logger.info("❌ WordPress post failed.")

            processed_ids.add(job_id)
            time.sleep(1)  # be polite to the source site

    logger.info(f"\n{'#'*60}")
    logger.info(f" CYCLE COMPLETE ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    logger.info(f" ✅ Posted  : {posted}")
    logger.info(f" ⏭ Skipped : {skipped}")
    logger.info(f" ❌ Failed  : {failed}")
    logger.info(f"{'#'*60}")


if __name__ == "__main__":
    logger.info("🚀 MyjobMag scraper — pages 3 → 1 — starting…")
    run()
    logger.info("✅ Done.")
