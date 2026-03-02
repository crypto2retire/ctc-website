"""
Pre-launch audit for /var/www/ctc-main/
7 checks across all 10 HTML pages.
"""
import json
import os
import re
import glob
import sys
from collections import defaultdict
from html.parser import HTMLParser

SITE_DIR = "/var/www/ctc-main"
DOMAIN = "https://cleartheclutterjunkremoval.com"
KNOWN_PAGES = {
    "index.html", "services.html", "what-we-take.html", "pricing.html",
    "contact.html", "junk-removal-oshkosh.html", "junk-removal-appleton.html",
    "junk-removal-neenah.html", "junk-removal-fond-du-lac.html",
    "junk-removal-winneconne.html",
}
# Map of clean URL paths to actual files
PATH_TO_FILE = {
    "/": "index.html",
    "/index.html": "index.html",
    "/services": "services.html",
    "/services.html": "services.html",
    "/what-we-take": "what-we-take.html",
    "/what-we-take.html": "what-we-take.html",
    "/pricing": "pricing.html",
    "/pricing.html": "pricing.html",
    "/contact": "contact.html",
    "/contact.html": "contact.html",
    "/junk-removal-oshkosh": "junk-removal-oshkosh.html",
    "/junk-removal-oshkosh.html": "junk-removal-oshkosh.html",
    "/junk-removal-appleton": "junk-removal-appleton.html",
    "/junk-removal-appleton.html": "junk-removal-appleton.html",
    "/junk-removal-neenah": "junk-removal-neenah.html",
    "/junk-removal-neenah.html": "junk-removal-neenah.html",
    "/junk-removal-fond-du-lac": "junk-removal-fond-du-lac.html",
    "/junk-removal-fond-du-lac.html": "junk-removal-fond-du-lac.html",
    "/junk-removal-winneconne": "junk-removal-winneconne.html",
    "/junk-removal-winneconne.html": "junk-removal-winneconne.html",
}

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

total_pass = 0
total_fail = 0
total_warn = 0


def status(ok, msg, warn=False):
    global total_pass, total_fail, total_warn
    if warn:
        total_warn += 1
        print(f"    {WARN}  {msg}")
    elif ok:
        total_pass += 1
        print(f"    {PASS}  {msg}")
    else:
        total_fail += 1
        print(f"    {FAIL}  {msg}")


# ── Helpers ───────────────────────────────────────────────────────────────────

class LDJSONExtractor(HTMLParser):
    """Extract all <script type="application/ld+json"> blocks."""
    def __init__(self):
        super().__init__()
        self._in_ldjson = False
        self._current = ""
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attr_dict = dict(attrs)
            if attr_dict.get("type") == "application/ld+json":
                self._in_ldjson = True
                self._current = ""

    def handle_data(self, data):
        if self._in_ldjson:
            self._current += data

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ldjson:
            self.blocks.append(self._current)
            self._in_ldjson = False


class LinkExtractor(HTMLParser):
    """Extract all href attributes from <a> tags."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val:
                    self.links.append(val)


def extract_text(html):
    """Strip HTML tags, return plain text."""
    return re.sub(r'<[^>]+>', ' ', html)


def find_prices(text):
    """Find all dollar amounts in text."""
    return re.findall(r'\$[\d,]+(?:\.\d{2})?', text)


# ── Check 1: JSON-LD Schema Validation ───────────────────────────────────────

def check_jsonld(filename, content):
    print(f"  [1] JSON-LD Schema Validation")
    parser = LDJSONExtractor()
    parser.feed(content)

    if not parser.blocks:
        status(False, "No JSON-LD blocks found")
        return

    status(True, f"Found {len(parser.blocks)} JSON-LD block(s)")

    found_types = set()
    for i, block in enumerate(parser.blocks, 1):
        try:
            data = json.loads(block)
            schema_type = data.get("@type", "unknown")
            found_types.add(schema_type)
            status(True, f"Block {i}: @type={schema_type} — valid JSON")

            # Extra checks per type
            if schema_type == "LocalBusiness":
                for key in ["name", "telephone", "address", "areaServed"]:
                    if key not in data:
                        status(False, f"  LocalBusiness missing '{key}'")
            elif schema_type == "FAQPage":
                entities = data.get("mainEntity", [])
                status(True, f"  FAQPage has {len(entities)} question(s)")
                if len(entities) < 5:
                    status(False, f"  FAQPage has fewer than 5 questions", warn=True)
            elif schema_type == "BreadcrumbList":
                items = data.get("itemListElement", [])
                status(True, f"  BreadcrumbList has {len(items)} item(s)")
        except json.JSONDecodeError as e:
            status(False, f"Block {i}: INVALID JSON — {e}")

    for expected in ["LocalBusiness", "FAQPage", "BreadcrumbList"]:
        if expected not in found_types:
            status(False, f"Missing {expected} schema")


# ── Check 2: Internal Links ──────────────────────────────────────────────────

def check_links(filename, content):
    print(f"  [2] Internal Link Check")
    parser = LinkExtractor()
    parser.feed(content)

    internal_count = 0
    broken = []
    placeholder_hashes = []

    for href in parser.links:
        # Skip external links, mailto, tel, sms
        if href.startswith(("http://", "https://", "mailto:", "tel:", "sms:", "javascript:")):
            # Check if it's a link to our own domain
            if href.startswith(DOMAIN):
                path = href[len(DOMAIN):]
                if path and path not in PATH_TO_FILE:
                    if not path.startswith("/css/") and not path.startswith("/images/"):
                        broken.append(href)
            continue

        # Pure hash links
        if href == "#":
            placeholder_hashes.append(href)
            continue
        if href.startswith("#"):
            continue  # Anchor links are fine

        # Internal path links
        internal_count += 1
        path = href.split("?")[0].split("#")[0]
        if path not in PATH_TO_FILE:
            # Check if it's a static resource
            full_path = os.path.join(SITE_DIR, path.lstrip("/"))
            if not os.path.exists(full_path):
                broken.append(href)

    status(True, f"{internal_count} internal links found")

    if broken:
        for b in broken:
            status(False, f"Broken link: {b}")
    else:
        status(True, "No broken internal links")

    if placeholder_hashes:
        # Check if they're on buttons/CTAs (acceptable) or on nav links (not acceptable)
        # Count how many # links are NOT on tel/sms/CTA buttons
        hash_contexts = re.findall(r'href="#"[^>]*>([^<]{0,60})', content)
        non_cta_hashes = [ctx for ctx in hash_contexts if not any(
            kw in ctx.lower() for kw in ["call", "text", "estimate", "phone", "menu", "toggle"]
        )]
        if non_cta_hashes:
            for ctx in non_cta_hashes:
                status(False, f'Placeholder # link: "{ctx.strip()}"', warn=True)
        else:
            status(True, f"{len(placeholder_hashes)} # link(s) — all on CTAs/buttons (OK)")
    else:
        status(True, "No placeholder # links")


# ── Check 3: Pricing Consistency ─────────────────────────────────────────────

def check_pricing(filename, content):
    print(f"  [3] Pricing Consistency")
    text = extract_text(content)
    issues = []

    # Look for explicit "Half Truck" price table entries: "Half Truck$250" or "Half-truck: $250"
    half_explicit = re.findall(r'Half[\s-]*[Tt]ruck[\s:$]*\$(\d+)', text)
    for m in half_explicit:
        val = int(m)
        if val < 200 or val > 350:
            issues.append(f"Half truck price table shows ${val} — expected $250-$300")

    # Look for explicit "Full Truck" price table entries
    full_explicit = re.findall(r'Full[\s-]*[Tt]ruck[\s:$]*\$(\d+)', text)
    for m in full_explicit:
        val = int(m)
        if val < 400 or val > 600:
            issues.append(f"Full truck price table shows ${val} — expected $500-$550")

    # Check "half-truck load starts at $X" or "half-truck loads from $X"
    half_starts = re.findall(r'half[\s-]*truck\s+load[s]?\s+(?:start[s]?\s+at|from)\s+\$(\d+)', text, re.IGNORECASE)
    for m in half_starts:
        val = int(m)
        if val < 200 or val > 350:
            issues.append(f"Half truck 'starts at ${val}' — expected $250-$300")

    # Check "full truck load starts at $X"
    full_starts = re.findall(r'full\s+truck\s+load[s]?\s+(?:start[s]?\s+at|from)\s+\$(\d+)', text, re.IGNORECASE)
    for m in full_starts:
        val = int(m)
        if val < 400 or val > 600:
            issues.append(f"Full truck 'starts at ${val}' — expected $500-$550")

    # Check "$X minimum" pattern
    min_explicit = re.findall(r'\$(\d+)\s+minimum\b', text, re.IGNORECASE)
    for m in min_explicit:
        val = int(m)
        if val < 75 or val > 125:
            issues.append(f"Minimum charge ${val} — expected $100")

    # Check "minimum charge.*$X" or "minimum pickup.*$X"
    min_charge = re.findall(r'minimum\s+(?:charge|pickup)[^.]{0,30}\$(\d+)', text, re.IGNORECASE)
    for m in min_charge:
        val = int(m)
        if val < 75 or val > 125:
            issues.append(f"Minimum charge ${val} — expected $100")

    # Check for wrong prices: "$200 half" or "$350 full"
    if re.search(r'\$200\s+half', text, re.IGNORECASE):
        issues.append("Found '$200 half-truck' — should be $250")
    if re.search(r'\$350\s+full', text, re.IGNORECASE):
        issues.append("Found '$350 full-truck' — should be $500")

    # Check "$100 to $100"
    if "$100 to $100" in text:
        issues.append("Found '$100 to $100' — likely typo, should be '$100 to $150'")

    if issues:
        for issue in issues:
            status(False, issue)
    else:
        all_prices = find_prices(text)
        status(True, f"Pricing consistent ({len(all_prices)} price references found)")


# ── Check 4: Recycling Fee Items ─────────────────────────────────────────────

RECYCLING_FEE_ITEMS = [
    ("tv", r'\b(?:tv|television|CRT)\b'),
    ("air conditioner", r'\bair\s+condition(?:er|ing)\b'),
    ("refrigerator", r'\b(?:refrigerator|fridge)\b'),
    ("freezer", r'\bfreezer\b'),
    ("dehumidifier", r'\bdehumidifier\b'),
    ("tire", r'\btire\b'),
]

def check_recycling_fees(filename, content):
    print(f"  [4] Recycling Fee Items")
    text = extract_text(content).lower()

    # Only check pages that discuss recycling/disposal fees in detail
    mentions_fees = any(kw in text for kw in [
        "recycling fee", "freon", "refrigerant", "refrigerant recovery",
    ])

    if not mentions_fees:
        status(True, "Page does not discuss recycling fees (N/A)")
        return

    # Check for "no charge" / "no extra charge" / "included" near freon/refrigerant items
    freon_no_charge = re.findall(
        r'[^.]*(?:freon|refrigerant|refrigerator|freezer|air\s+condition|dehumidifier)[^.]*(?:no\s+(?:extra\s+|additional\s+)?(?:charge|surcharge|fee)|included\s+at\s+no)[^.]*\.',
        text
    )
    if freon_no_charge:
        for match in freon_no_charge:
            snippet = match.strip()[:120]
            status(False, f"Freon item described as no charge: '{snippet}...'")
    else:
        status(True, "No freon/refrigerant items described as 'no charge'")

    # On what-we-take and pricing, check all fee items are mentioned
    if filename in ("what-we-take.html", "pricing.html"):
        for item_name, pattern in RECYCLING_FEE_ITEMS:
            if re.search(pattern, text, re.IGNORECASE):
                status(True, f"'{item_name}' mentioned")
            else:
                status(False, f"'{item_name}' NOT mentioned on {filename}", warn=True)


# ── Check 5: HTTP 200 from server ────────────────────────────────────────────

def check_http_status(filename):
    print(f"  [5] HTTP Status")
    import subprocess

    # Map filename to URL path
    path = "/" + filename.replace(".html", "") if filename != "index.html" else "/"

    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://127.0.0.1{path}",
             "-H", "Host: cleartheclutterjunkremoval.com"],
            capture_output=True, text=True, timeout=10
        )
        code = result.stdout.strip()
        if code == "200":
            status(True, f"HTTP {code} for {path}")
        elif code == "301" or code == "302":
            status(True, f"HTTP {code} redirect for {path}", warn=True)
        else:
            status(False, f"HTTP {code} for {path}")
    except Exception as e:
        status(False, f"HTTP check failed: {e}")


# ── Check 6: Rybbit Analytics ────────────────────────────────────────────────

def check_analytics(filename, content):
    print(f"  [6] Rybbit Analytics Snippet")
    site_id = "f6ef73460382"

    if "analytics.donelocal.io/script.js" in content and site_id in content:
        status(True, f"Rybbit snippet present (site ID {site_id})")
    elif "analytics.donelocal.io" in content:
        status(False, f"Analytics script found but wrong site ID")
    else:
        status(False, f"Rybbit analytics snippet MISSING")


# ── Check 7: Remaining Placeholders ──────────────────────────────────────────

def check_placeholders(filename, content):
    print(f"  [7] Remaining Placeholders")

    verify_tags = re.findall(r'\[VERIFY[^\]]*\]', content)
    insert_tags = re.findall(r'\[INSERT[^\]]*\]', content)
    placeholder_tags = re.findall(r'\[(?:PHONE|EMAIL|ADDRESS|NAME|YEARS|REVIEW_COUNT|TODO)[^\]]*\]', content)

    all_tags = verify_tags + insert_tags + placeholder_tags

    if all_tags:
        for tag in all_tags:
            status(False, f"Placeholder found: {tag}")
    else:
        status(True, "No [VERIFY], [INSERT], or other placeholders found")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global total_pass, total_fail, total_warn

    html_files = sorted(glob.glob(os.path.join(SITE_DIR, "*.html")))

    print("=" * 70)
    print("CTC PRE-LAUNCH AUDIT — /var/www/ctc-main/")
    print("=" * 70)

    for filepath in html_files:
        filename = os.path.basename(filepath)
        if filename not in KNOWN_PAGES:
            continue

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        print(f"\n{'─' * 70}")
        print(f"  {filename}")
        print(f"{'─' * 70}")

        check_jsonld(filename, content)
        check_links(filename, content)
        check_pricing(filename, content)
        check_recycling_fees(filename, content)
        check_http_status(filename)
        check_analytics(filename, content)
        check_placeholders(filename, content)

    print(f"\n{'=' * 70}")
    print(f"AUDIT SUMMARY")
    print(f"{'=' * 70}")
    print(f"  {PASS}: {total_pass}")
    print(f"  {FAIL}: {total_fail}")
    print(f"  {WARN}: {total_warn}")
    print(f"  Total checks: {total_pass + total_fail + total_warn}")

    if total_fail == 0:
        print(f"\n  \033[92mALL CHECKS PASSED\033[0m")
    else:
        print(f"\n  \033[91m{total_fail} FAILURE(S) NEED ATTENTION\033[0m")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
