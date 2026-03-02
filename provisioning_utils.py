"""
Shared provisioning utilities for DNS, nginx, certbot, and quality gate operations.
Used by both wsic_provisioning_service and donelocal_provisioning_service.
"""
import asyncio
import json
import logging
import re
import shutil
import socket
import subprocess
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger("hauliq.provisioning_utils")

# ── Constants ────────────────────────────────────────────────────
NGINX_SITES_DIR = "/etc/nginx/sites-enabled"
SERVER_IP = "138.68.239.233"
CERTBOT_EMAIL = "admin@donelocal.io"


# ── Slug Generation ──────────────────────────────────────────────

def generate_slug(name: str) -> str:
    """Convert a business name to a URL-safe slug. Max 63 chars (DNS label limit)."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    slugified = re.sub(r"[^a-z0-9]+", "-", lowered)
    slugified = re.sub(r"-+", "-", slugified).strip("-")
    return slugified[:63]


def slugify_domain(domain: str) -> str:
    """Convert a domain like 'bobs-plumbing.com' to 'bobs-plumbing-com' for filenames."""
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


# ── Input Validation ─────────────────────────────────────────────

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def validate_subdomain(subdomain: str) -> bool:
    return bool(_SUBDOMAIN_RE.match(subdomain))


def validate_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.match(domain.lower())) and len(domain) <= 253


# ── Cloudflare DNS ───────────────────────────────────────────────

async def cloudflare_add_dns_record(
    zone_id: str,
    name: str,
    ip: str = SERVER_IP,
    record_type: str = "A",
    proxied: bool = False,
    ttl: int = 120,
) -> dict:
    """Create a DNS record via Cloudflare API. Returns full API response."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    headers = {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "type": record_type,
        "name": name,
        "content": ip,
        "ttl": ttl,
        "proxied": proxied,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Cloudflare DNS create failed: {data.get('errors', [])}")
        logger.info(f"Cloudflare DNS record created: {name} -> {ip}, id={data['result']['id']}")
        return data


async def cloudflare_delete_dns_record(zone_id: str, record_id: str) -> dict:
    """Delete a DNS record by Cloudflare record ID."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    headers = {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Cloudflare DNS record deleted: {record_id}")
        return data


async def cloudflare_enable_proxy(zone_id: str, record_id: str) -> dict:
    """Enable Cloudflare proxy (DDoS protection) on an existing DNS record."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    headers = {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(url, json={"proxied": True}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Cloudflare proxy enable failed: {data.get('errors', [])}")
        logger.info(f"Cloudflare proxy enabled for record {record_id}")
        return data


# ── Nginx ────────────────────────────────────────────────────────

def _build_vhost_config(domain: str, site_path: str, ssl: bool = False) -> str:
    """Build nginx vhost config. Domain and site_path MUST be pre-validated."""
    if not validate_domain(domain) and not validate_subdomain(domain.split(".")[0]):
        raise ValueError(f"Invalid domain for nginx config: {domain}")
    if not Path(site_path).is_absolute():
        raise ValueError(f"site_path must be absolute: {site_path}")

    if ssl:
        return (
            f"server {{\n"
            f"    listen 80;\n"
            f"    server_name {domain};\n"
            f"    return 301 https://$host$request_uri;\n"
            f"}}\n\n"
            f"server {{\n"
            f"    listen 443 ssl http2;\n"
            f"    server_name {domain};\n\n"
            f"    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;\n"
            f"    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;\n"
            f"    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
            f"    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n\n"
            f"    root {site_path};\n"
            f"    index index.html;\n\n"
            f"    location / {{\n"
            f"        try_files $uri $uri/ /index.html;\n"
            f"    }}\n\n"
            f"    add_header X-Frame-Options \"SAMEORIGIN\" always;\n"
            f"    add_header X-Content-Type-Options \"nosniff\" always;\n"
            f"    add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;\n"
            f"}}\n"
        )
    else:
        return (
            f"server {{\n"
            f"    listen 80;\n"
            f"    server_name {domain};\n\n"
            f"    root {site_path};\n"
            f"    index index.html;\n\n"
            f"    location / {{\n"
            f"        try_files $uri $uri/ /index.html;\n"
            f"    }}\n"
            f"}}\n"
        )


def nginx_create_vhost(
    domain: str,
    site_path: str,
    conf_filename: str,
    ssl: bool = False,
) -> str:
    """Write nginx vhost config. Returns path to config file."""
    if not re.match(r"^[a-z0-9._-]+$", conf_filename):
        raise ValueError(f"Unsafe nginx config filename: {conf_filename}")

    config_content = _build_vhost_config(domain, site_path, ssl=ssl)
    conf_path = f"{NGINX_SITES_DIR}/{conf_filename}"
    Path(conf_path).write_text(config_content)
    logger.info(f"Nginx vhost written: {conf_path}")
    return conf_path


def nginx_test_config() -> Tuple[bool, str]:
    """Run nginx -t. Returns (success, output)."""
    result = subprocess.run(["/usr/sbin/nginx", "-t"], capture_output=True, text=True, timeout=15)
    success = result.returncode == 0
    output = result.stdout + result.stderr
    if not success:
        logger.error(f"nginx -t failed: {output}")
    return success, output


def nginx_reload() -> Tuple[bool, str]:
    """Test config, then reload if valid. Returns (success, output)."""
    test_ok, test_output = nginx_test_config()
    if not test_ok:
        return False, f"nginx config test failed: {test_output}"
    result = subprocess.run(
        ["/usr/bin/systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=15
    )
    success = result.returncode == 0
    output = result.stdout + result.stderr
    if success:
        logger.info("nginx reloaded successfully")
    else:
        logger.error(f"nginx reload failed: {output}")
    return success, output


def nginx_remove_vhost(conf_path: str) -> None:
    """Remove nginx vhost config file."""
    p = Path(conf_path)
    if p.exists():
        p.unlink()
        logger.info(f"Nginx vhost removed: {conf_path}")


# ── Certbot SSL ──────────────────────────────────────────────────

def certbot_provision_ssl(domain: str, email: str = CERTBOT_EMAIL) -> Tuple[bool, str]:
    """Provision SSL via certbot. Uses list-form subprocess for security."""
    if not validate_domain(domain):
        raise ValueError(f"Invalid domain for certbot: {domain}")

    cmd = [
        "/usr/bin/certbot", "certonly",
        "--nginx",
        "-d", domain,
        "--email", email,
        "--non-interactive",
        "--agree-tos",
        "--no-eff-email",
    ]
    logger.info(f"Running certbot for {domain}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    success = result.returncode == 0
    output = result.stdout + result.stderr
    if success:
        logger.info(f"SSL provisioned for {domain}")
    else:
        logger.error(f"Certbot failed for {domain}: {output}")
    return success, output


# ── DNS Polling ──────────────────────────────────────────────────

async def poll_dns_resolution(
    domain: str,
    expected_ip: str = SERVER_IP,
    interval: int = 30,
    timeout: int = 600,
) -> bool:
    """Poll DNS every interval seconds until domain resolves to expected_ip or timeout."""
    elapsed = 0
    loop = asyncio.get_event_loop()
    while elapsed < timeout:
        try:
            result = await loop.run_in_executor(None, socket.gethostbyname, domain)
            if result == expected_ip:
                logger.info(f"DNS resolved: {domain} -> {expected_ip}")
                return True
            else:
                logger.debug(f"DNS {domain} resolved to {result}, expected {expected_ip}")
        except socket.gaierror:
            logger.debug(f"DNS {domain} not yet resolvable")
        await asyncio.sleep(interval)
        elapsed += interval
    logger.warning(f"DNS polling timed out for {domain} after {timeout}s")
    return False


# ── Site File Management ─────────────────────────────────────────

def create_site_directory(site_path: str) -> None:
    """Create site directory with www-data ownership."""
    p = Path(site_path)
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/chown", "-R", "www-data:www-data", site_path], timeout=10)
    logger.info(f"Site directory created: {site_path}")


def write_site_files(site_path: str, html_content: str, filename: str = "index.html") -> None:
    """Write HTML content to site directory."""
    filepath = Path(site_path) / filename
    filepath.write_text(html_content, encoding="utf-8")
    subprocess.run(["/usr/bin/chown", "www-data:www-data", str(filepath)], timeout=10)
    logger.info(f"Site file written: {filepath}")


def remove_site_directory(site_path: str) -> None:
    """Remove site directory. Allowlisted to safe prefixes only."""
    p = Path(site_path)
    if p.exists() and str(p).startswith(("/var/www/wsic-sites/", "/var/www/donelocal-sites/")):
        shutil.rmtree(p)
        logger.info(f"Site directory removed: {site_path}")
    else:
        logger.warning(f"Refusing to remove path: {site_path}")


# ── Quality Gate ─────────────────────────────────────────────────

async def quality_gate(
    html_content: str,
    site_type: str,
    business_context: dict,
) -> dict:
    """
    Score generated website HTML. Returns {score, passed, feedback, issues}.
    Thresholds: wsic >= 60, donelocal >= 75.
    """
    from services.model_router import call_llm

    threshold = 60 if site_type == "wsic" else 75
    biz_name = business_context.get("name", "Unknown")
    industry = business_context.get("industry", "Unknown")
    city = business_context.get("city", "Unknown")

    system_prompt = (
        f"You are a website quality reviewer. Score the following {site_type} website HTML "
        f"on a scale of 0-100 based on these criteria:\n\n"
        f"1. Content Accuracy (0-25): Does it represent the business accurately?\n"
        f"   Business: {biz_name}, Industry: {industry}, City: {city}\n"
        f"2. HTML Quality (0-25): Valid structure, semantic tags, responsive viewport, heading hierarchy.\n"
        f"3. Visual/UX Quality (0-25): Professional styling, readable typography, clear CTAs, mobile-friendly.\n"
        f"4. SEO Basics (0-25): Title tag, meta description, H1, structured data.\n\n"
        f"Respond in EXACTLY this JSON format:\n"
        '{"score": <int 0-100>, "feedback": "<one paragraph>", "issues": ["issue1", "issue2"]}'
    )

    messages = [{"role": "user", "content": f"Review this website HTML:\n\n{html_content[:30000]}"}]

    try:
        response = await call_llm(
            task_type="quality_gate",
            messages=messages,
            system_prompt=system_prompt,
        )
        # call_llm returns dict with "content" key containing the LLM text
        if isinstance(response, dict) and "content" in response:
            resp_text = response["content"]
        elif isinstance(response, dict):
            resp_text = json.dumps(response)
        else:
            resp_text = str(response) if response else ""

        # Parse the JSON from the LLM response text
        try:
            result = json.loads(resp_text)
        except (json.JSONDecodeError, TypeError):
            # Try regex extraction if wrapped in markdown or extra text
            match = re.search(r'\{[^{}]*"score"\s*:\s*\d+[^{}]*\}', resp_text, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                logger.error(f"Quality gate unparseable: {resp_text[:200]}")
                result = {"score": 0, "feedback": "Failed to parse quality gate response", "issues": ["Unparseable LLM output"]}
    except Exception as e:
        logger.error(f"Quality gate LLM call failed: {e}")
        result = {"score": 0, "feedback": f"Quality gate error: {e}", "issues": ["LLM call failed"]}

    result["passed"] = result.get("score", 0) >= threshold
    result["threshold"] = threshold
    return result
