#!/usr/bin/env python3
"""Собирает посты публичного Telegram-канала в news.json и пререндерит их в news.html.

Источник — веб-версия канала https://t.me/s/<channel> (без бота и токенов).
Только стандартная библиотека Python.

Защиты:
  * если Telegram вернул 0 постов — файлы не трогаем (exit 0);
  * новые посты сливаются со старыми по id, старые не теряются;
  * в архиве хранится не больше MAX_KEEP постов.
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

CHANNEL = os.environ.get("TG_CHANNEL", "businessVSworld")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_JSON = os.path.join(ROOT, "news.json")
NEWS_HTML = os.path.join(ROOT, "news.html")
INDEX_HTML = os.path.join(ROOT, "index.html")
HOME_COUNT = 3
SITEMAP = os.path.join(ROOT, "sitemap.xml")
PAGES = int(os.environ.get("TG_PAGES", "4"))      # ~20 постов на страницу
MAX_KEEP = 300
PRERENDER = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "ru,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


# ---------- sanitizing ----------
ALLOWED = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "blockquote", "br", "a"}


class Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if tag not in ALLOWED:
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not re.match(r"^https?://", href):
                self.stack.append(None)
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}" '
                            'target="_blank" rel="noopener nofollow">')
            self.stack.append("a")
            return
        self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if tag not in ALLOWED or tag == "br":
            return
        # закрываем ближайший открытый такой же тег
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx] == tag or (tag == "a" and self.stack[idx] is None):
                closing = self.stack.pop(idx)
                if closing:
                    self.out.append(f"</{closing}>")
                return

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(html.escape(data, quote=False))

    def result(self):
        for t in reversed(self.stack):
            if t:
                self.out.append(f"</{t}>")
        return "".join(self.out)


def sanitize(raw):
    # эмодзи Telegram: <i class="emoji" ...><b>🔥</b></i> → 🔥
    raw = re.sub(r'<i class="emoji"[^>]*>\s*<b>(.*?)</b>\s*</i>', r"\1", raw, flags=re.S)
    raw = re.sub(r"</?tg-emoji[^>]*>", "", raw)
    s = Sanitizer()
    s.feed(raw)
    s.close()
    out = s.result()
    out = re.sub(r"(<br>\s*){3,}", "<br><br>", out).strip()
    out = re.sub(r"^(<br>\s*)+|(\s*<br>)+$", "", out)
    return out


def to_text(fragment):
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


# ---------- parsing t.me/s ----------
def balanced_div(src, start):
    """Возвращает внутренность <div>, открытого на позиции start."""
    open_end = src.index(">", start) + 1
    depth, pos = 1, open_end
    tag = re.compile(r"<(/?)div\b", re.I)
    while depth:
        m = tag.search(src, pos)
        if not m:
            return src[open_end:]
        depth += -1 if m.group(1) else 1
        pos = m.end()
    return src[open_end:m.start()]


def bg_url(chunk, cls):
    m = re.search(r'class="[^"]*' + cls + r'[^"]*"[^>]*style="[^"]*background-image:url\(\'([^\']+)\'\)',
                  chunk)
    return m.group(1) if m else None


def parse_page(page):
    posts = []
    parts = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for chunk in parts[1:]:
        m = re.search(r'data-post="([^"/]+)/(\d+)"', chunk)
        if not m:
            continue
        pid = int(m.group(2))
        dt = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        if not dt:
            continue
        text_html = ""
        tm = re.search(r'<div class="tgme_widget_message_text[^"]*"', chunk)
        if tm:
            text_html = sanitize(balanced_div(chunk, tm.start()))
        image = (bg_url(chunk, "tgme_widget_message_photo_wrap")
                 or bg_url(chunk, "tgme_widget_message_video_thumb")
                 or bg_url(chunk, "link_preview_image"))
        if not text_html and not image:
            continue  # служебные сообщения
        views = re.search(r'<span class="tgme_widget_message_views">([^<]+)</span>', chunk)
        text = to_text(text_html)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        title = lines[0] if lines else "Фото"
        if len(title) > 160:
            cut = title[:160].rsplit(" ", 1)[0]
            title = cut + "…"
        posts.append({
            "id": pid,
            "date": dt.group(1),
            "title": title,
            "text": text,
            "html": text_html,
            "image": image,
            "views": views.group(1).strip() if views else None,
            "url": f"https://t.me/{CHANNEL}/{pid}",
        })
    return posts


def scrape():
    found, before = {}, None
    for _ in range(PAGES):
        url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
        posts = parse_page(fetch(url))
        if not posts:
            break
        for p in posts:
            found[p["id"]] = p
        low = min(p["id"] for p in posts)
        if before is not None and low >= before:
            break
        before = low
        time.sleep(1.5)
    return list(found.values())


# ---------- rendering ----------
def ru_date(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    # время показываем по Москве
    from datetime import timedelta
    d = d.astimezone(timezone(timedelta(hours=3)))
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def card(p):
    img = (f'<a class="n-img" href="{p["url"]}" target="_blank" rel="noopener">'
           f'<img src="{html.escape(p["image"])}" alt="" loading="lazy" '
           'onerror="this.parentNode.remove()"></a>') if p.get("image") else ""
    views = f'<span class="n-views">👁 {html.escape(p["views"])}</span>' if p.get("views") else ""
    return (f'<article class="n-card" id="post-{p["id"]}" data-id="{p["id"]}">{img}'
            f'<div class="n-body"><div class="n-meta"><time datetime="{p["date"]}">{ru_date(p["date"])}</time>{views}</div>'
            f'<div class="n-text">{p["html"]}</div>'
            f'<div class="n-foot"><button class="n-more" type="button" hidden>Читать полностью</button>'
            f'<a class="n-tg" href="{p["url"]}" target="_blank" rel="noopener">Открыть в Telegram →</a></div>'
            f'</div></article>')


def prerender(posts):
    if not os.path.exists(NEWS_HTML):
        return
    src = open(NEWS_HTML, encoding="utf-8").read()
    block = "\n".join(card(p) for p in posts[:PRERENDER])
    new = re.sub(r"(<!--NEWS:START-->).*?(<!--NEWS:END-->)",
                 lambda m: m.group(1) + "\n" + block + "\n" + m.group(2), src, flags=re.S)
    if new != src:
        open(NEWS_HTML, "w", encoding="utf-8").write(new)


def home_card(p):
    lines = [l.strip() for l in (p.get("text") or "").split("\n") if l.strip()]
    rest = " ".join(lines[1:]) if len(lines) > 1 else ""
    if len(rest) > 260:
        rest = rest[:260].rsplit(" ", 1)[0] + "…"
    excerpt = f'<div class="hn-excerpt">{html.escape(rest)}</div>' if rest else ""
    return (f'<a class="hn-card" href="/news.html#post-{p["id"]}">'
            f'<time class="hn-date" datetime="{p["date"]}">{ru_date(p["date"])}</time>'
            f'<div class="hn-title">{html.escape(p["title"])}</div>{excerpt}'
            '<span class="hn-more">Читать →</span></a>')


def prerender_home(posts):
    if not os.path.exists(INDEX_HTML):
        return
    src = open(INDEX_HTML, encoding="utf-8").read()
    block = "\n".join(home_card(p) for p in posts[:HOME_COUNT])
    new = re.sub(r"(<!--HOMENEWS:START-->).*?(<!--HOMENEWS:END-->)",
                 lambda m: m.group(1) + "\n" + block + "\n" + m.group(2), src, flags=re.S)
    if new != src:
        open(INDEX_HTML, "w", encoding="utf-8").write(new)


def touch_sitemap():
    if not os.path.exists(SITEMAP):
        return
    s = open(SITEMAP, encoding="utf-8").read()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s2 = re.sub(r"(<loc>https://vedexpo\.ru/news\.html</loc>\s*<lastmod>)[^<]+",
                r"\g<1>" + today, s)
    if s2 != s:
        open(SITEMAP, "w", encoding="utf-8").write(s2)


def main():
    old = []
    if os.path.exists(NEWS_JSON):
        try:
            old = json.load(open(NEWS_JSON, encoding="utf-8")).get("posts", [])
        except Exception:  # noqa: BLE001
            old = []
    if "--render-only" in sys.argv:
        posts = sorted(old, key=lambda p: p["id"], reverse=True)
        prerender(posts)
        prerender_home(posts)
        print(f"перерисовано из news.json: {len(posts)} постов")
        return 0
    fresh = scrape()
    if not fresh:
        print("Telegram вернул 0 постов — ничего не меняю")
        return 0
    merged = {p["id"]: p for p in old}
    new_ids = [p["id"] for p in fresh if p["id"] not in merged]
    for p in fresh:
        merged[p["id"]] = p  # обновляем правки и просмотры
    posts = sorted(merged.values(), key=lambda p: p["id"], reverse=True)[:MAX_KEEP]

    payload = {"channel": CHANNEL,
               "channel_url": f"https://t.me/{CHANNEL}",
               "posts": posts}
    old_payload = {"channel": CHANNEL, "channel_url": f"https://t.me/{CHANNEL}", "posts": old}
    if payload != old_payload:
        payload["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(NEWS_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        prerender(posts)
        prerender_home(posts)
        if new_ids:
            touch_sitemap()
    print(f"собрано {len(fresh)}, новых {len(new_ids)}, всего {len(posts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
