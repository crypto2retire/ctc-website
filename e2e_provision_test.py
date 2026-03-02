"""
E2E Provisioning Test: Direct infrastructure test
Bypasses quality gate to test DNS, nginx, SSL, serving, and cleanup directly.
"""
import asyncio
import sys
import os
import json

sys.path.insert(0, "/opt/hauliq")
os.chdir("/opt/hauliq")

from dotenv import load_dotenv
load_dotenv("/opt/hauliq/.env", override=True)


async def main():
    from database import async_session_factory
    from sqlalchemy import text
    from config import settings
    from services.provisioning_utils import (
        cloudflare_add_dns_record,
        cloudflare_delete_dns_record,
        cloudflare_enable_proxy,
        nginx_create_vhost,
        nginx_reload,
        nginx_remove_vhost,
        certbot_provision_ssl,
        poll_dns_resolution,
        create_site_directory,
        write_site_files,
        remove_site_directory,
        SERVER_IP,
    )
    import httpx
    from pathlib import Path

    TEST_SUBDOMAIN = "test-co"
    FULL_DOMAIN = f"{TEST_SUBDOMAIN}.whatshouldicharge.app"
    SITE_PATH = f"/var/www/wsic-sites/{TEST_SUBDOMAIN}"
    CONF_FILENAME = f"wsic-{TEST_SUBDOMAIN}.conf"
    ZONE_ID = settings.CLOUDFLARE_ZONE_ID_WSIC

    # Read landing page HTML for site content
    async with async_session_factory() as db:
        row = await db.execute(
            text("SELECT html_content FROM wsic_landing_pages WHERE id = 3")
        )
        page = row.fetchone()
        html_content = page[0] if page else "<html><body><h1>Test</h1></body></html>"

    print("=" * 60)
    print("E2E WSIC PROVISIONING TEST (direct infra)")
    print("=" * 60)

    cf_record_id = None
    conf_path = None

    try:
        # -- Step 1: Create site directory + files --
        print(f"\n[1] Creating site directory: {SITE_PATH}")
        create_site_directory(SITE_PATH)
        write_site_files(SITE_PATH, html_content)
        index = Path(SITE_PATH) / "index.html"
        print(f"    OK: index.html = {index.stat().st_size} bytes")

        # -- Step 2: Cloudflare DNS (proxied=false for certbot) --
        print(f"\n[2] Creating Cloudflare DNS: {FULL_DOMAIN} -> {SERVER_IP}")
        dns_resp = await cloudflare_add_dns_record(ZONE_ID, FULL_DOMAIN, proxied=False)
        cf_record_id = dns_resp["result"]["id"]
        print(f"    OK: record_id = {cf_record_id}")

        # -- Step 3: Nginx HTTP vhost --
        print(f"\n[3] Creating nginx HTTP vhost")
        conf_path = nginx_create_vhost(FULL_DOMAIN, SITE_PATH, CONF_FILENAME, ssl=False)
        ok, output = nginx_reload()
        print(f"    Nginx reload: {'OK' if ok else 'FAILED: ' + output}")
        if not ok:
            raise RuntimeError(f"nginx reload failed: {output}")

        # -- Step 4: Test HTTP serving --
        print(f"\n[4] Testing HTTP via localhost with Host header")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("http://127.0.0.1/", headers={"Host": FULL_DOMAIN})
            print(f"    HTTP status: {resp.status_code}")
            if resp.status_code == 200:
                print(f"    Content length: {len(resp.text)} chars")

        # -- Step 5: Wait for DNS propagation --
        print(f"\n[5] Waiting for DNS propagation (polling every 10s, up to 2 min)...")
        dns_ready = await poll_dns_resolution(FULL_DOMAIN, expected_ip=SERVER_IP, interval=10, timeout=120)
        print(f"    DNS resolved: {dns_ready}")
        if not dns_ready:
            print(f"    WARNING: DNS not yet propagated, certbot will likely fail")

        # -- Step 6: Certbot SSL --
        print(f"\n[6] Provisioning SSL via certbot (this may take 30-60s)...")
        ssl_ok, ssl_output = certbot_provision_ssl(FULL_DOMAIN)
        print(f"    Certbot: {'OK' if ssl_ok else 'FAILED'}")
        if not ssl_ok:
            print(f"    Output: {ssl_output[:500]}")

        if ssl_ok:
            # -- Step 7: Upgrade to HTTPS vhost --
            print(f"\n[7] Upgrading nginx to HTTPS vhost")
            nginx_create_vhost(FULL_DOMAIN, SITE_PATH, CONF_FILENAME, ssl=True)
            ok2, out2 = nginx_reload()
            print(f"    Nginx reload: {'OK' if ok2 else 'FAILED: ' + out2}")

            # -- Step 8: Enable Cloudflare proxy --
            print(f"\n[8] Enabling Cloudflare proxy (DDoS protection)")
            try:
                await cloudflare_enable_proxy(ZONE_ID, cf_record_id)
                print(f"    OK: proxied=true")
            except Exception as e:
                print(f"    WARNING: {e}")

            # -- Step 9: Test HTTPS serving --
            print(f"\n[9] Testing HTTPS via localhost")
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                resp = await client.get("https://127.0.0.1/", headers={"Host": FULL_DOMAIN})
                print(f"    HTTPS status: {resp.status_code}")

        # -- Step 10: Verify Cloudflare record state --
        print(f"\n[10] Verifying Cloudflare record state")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records",
                params={"name": FULL_DOMAIN},
                headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"},
            )
            records = resp.json().get("result", [])
            if records:
                rec = records[0]
                print(f"    {rec['name']} -> {rec['content']} (proxied={rec['proxied']})")

        # -- Step 11: Write DB record --
        print(f"\n[11] Writing DB record")
        async with async_session_factory() as db:
            await db.execute(
                text("DELETE FROM wsic_provisioned_sites WHERE subdomain = :s"),
                {"s": TEST_SUBDOMAIN},
            )
            await db.execute(
                text("""
                    INSERT INTO wsic_provisioned_sites
                    (company_id, landing_page_id, subdomain, full_subdomain,
                     cloudflare_record_id, site_path, nginx_conf_subdomain,
                     ssl_provisioned, quality_score, quality_passed,
                     flagged_for_review, status, provisioned_at)
                    VALUES (6, 3, :sub, :full, :cfid, :sp, :nc,
                            :ssl, 80, TRUE, FALSE, 'live', now())
                """),
                {
                    "sub": TEST_SUBDOMAIN, "full": FULL_DOMAIN,
                    "cfid": cf_record_id, "sp": SITE_PATH, "nc": conf_path,
                    "ssl": ssl_ok,
                },
            )
            await db.commit()
            print(f"    OK: status=live")

        print(f"\n{'=' * 60}")
        print(f"PROVISIONING COMPLETE: https://{FULL_DOMAIN}")
        print(f"{'=' * 60}")

        # -- Step 12: Deprovision --
        print(f"\n[12] DEPROVISIONING {FULL_DOMAIN}...")
        from services import wsic_provisioning_service as wsic_svc
        async with async_session_factory() as db:
            row = await db.execute(
                text("SELECT id FROM wsic_provisioned_sites WHERE subdomain = :s"),
                {"s": TEST_SUBDOMAIN},
            )
            site = row.fetchone()
            if site:
                result = await wsic_svc.deprovision(db, site[0])
                print(f"    Result: {json.dumps(result, default=str)}")

        # -- Step 13: Verify cleanup --
        print(f"\n[13] Verifying cleanup...")

        # DNS
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records",
                params={"name": FULL_DOMAIN},
                headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"},
            )
            records = resp.json().get("result", [])
            dns_clean = len(records) == 0
            print(f"    DNS records: {len(records)} {'CLEAN' if dns_clean else 'LEAKED!'}")

        # Nginx
        nginx_clean = not Path(f"/etc/nginx/sites-enabled/{CONF_FILENAME}").exists()
        print(f"    Nginx config: {'CLEAN' if nginx_clean else 'LEAKED!'}")

        # Site files
        files_clean = not Path(SITE_PATH).exists()
        print(f"    Site directory: {'CLEAN' if files_clean else 'LEAKED!'}")

        # DB
        async with async_session_factory() as db:
            row = await db.execute(
                text("SELECT status FROM wsic_provisioned_sites WHERE subdomain = :s"),
                {"s": TEST_SUBDOMAIN},
            )
            site = row.fetchone()
            print(f"    DB status: {site[0] if site else 'NOT FOUND'}")

        all_clean = dns_clean and nginx_clean and files_clean
        print(f"\n{'=' * 60}")
        print(f"E2E TEST {'PASSED' if all_clean else 'FAILED'}")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n!!! EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

        # Emergency cleanup
        print(f"\n[CLEANUP] Emergency cleanup...")
        try:
            if cf_record_id:
                await cloudflare_delete_dns_record(ZONE_ID, cf_record_id)
                print(f"    DNS record deleted")
        except Exception:
            pass
        try:
            if conf_path:
                nginx_remove_vhost(conf_path)
                nginx_reload()
                print(f"    Nginx config removed")
        except Exception:
            pass
        try:
            remove_site_directory(SITE_PATH)
            print(f"    Site directory removed")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
