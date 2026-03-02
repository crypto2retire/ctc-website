"""
E2E API Provisioning Test: Full flow through the API with quality gate.
Tests: POST /provision -> verify live -> GET /sites -> DELETE /sites/{id} -> verify cleanup.
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
    import httpx
    from pathlib import Path
    from auth.jwt import create_access_token
    from config import settings
    from database import async_session_factory
    from sqlalchemy import text

    # Generate a fresh auth token for company 6 (Agent Tester Co)
    token = create_access_token(user_id=5, company_id=6, role="owner")
    cookies = {"access_token": token}

    BASE_URL = "http://127.0.0.1:8000/api/v1"
    ZONE_ID = settings.CLOUDFLARE_ZONE_ID_WSIC

    print("=" * 60)
    print("E2E API PROVISIONING TEST (full flow with quality gate)")
    print("=" * 60)

    site_id = None

    try:
        # -- Step 1: Call the provisioning API endpoint --
        print(f"\n[1] POST /wsic/provisioning/provision")
        print(f"    landing_page_id=3, business_name='Junk King Austin'")
        async with httpx.AsyncClient(timeout=300, cookies=cookies) as client:
            resp = await client.post(
                f"{BASE_URL}/wsic/provisioning/provision",
                json={
                    "landing_page_id": 3,
                    "business_name": "Junk King Austin",
                },
            )
            print(f"    HTTP status: {resp.status_code}")
            result = resp.json()
            print(f"    Response: {json.dumps(result, indent=2)}")

            if resp.status_code != 200:
                print(f"\n!!! Provisioning API returned {resp.status_code}")
                print(f"    Detail: {result}")
                return

        status = result.get("status")
        subdomain = result.get("subdomain")
        full_domain = result.get("full_domain")
        quality_score = result.get("quality_score")

        print(f"\n    Status: {status}")
        print(f"    Subdomain: {subdomain}")
        print(f"    Domain: {full_domain}")
        print(f"    Quality score: {quality_score}")
        print(f"    SSL: {result.get('ssl')}")

        if status == "flagged_review":
            print(f"\n    Quality gate BLOCKED provisioning (score={quality_score})")
            print(f"    Feedback: {result.get('quality_feedback')}")
            print(f"    Issues: {result.get('quality_issues')}")
            print(f"\n    This is expected behavior if score < 60.")
            print(f"    Cleaning up flagged record...")
            # Clean up the flagged record
            async with async_session_factory() as db:
                await db.execute(
                    text("DELETE FROM wsic_provisioned_sites WHERE subdomain = :s"),
                    {"s": subdomain},
                )
                await db.commit()
            print(f"    Cleaned up.")
            print(f"\n{'=' * 60}")
            print(f"E2E TEST PASSED (quality gate correctly blocked low-quality page)")
            print(f"{'=' * 60}")
            return

        if status != "live":
            print(f"\n!!! Unexpected status: {status}")
            return

        # -- Step 2: Verify via GET /sites --
        print(f"\n[2] GET /wsic/provisioning/sites")
        async with httpx.AsyncClient(timeout=30, cookies=cookies) as client:
            resp = await client.get(f"{BASE_URL}/wsic/provisioning/sites")
            sites = resp.json()
            print(f"    Found {len(sites)} site(s)")
            live_site = next((s for s in sites if s.get("subdomain") == subdomain), None)
            if live_site:
                site_id = live_site["id"]
                print(f"    Site ID: {site_id}")
                print(f"    Status: {live_site['status']}")
                print(f"    SSL: {live_site['ssl_provisioned']}")
                print(f"    Quality: {live_site['quality_score']} (passed={live_site['quality_passed']})")
            else:
                print(f"    !!! Site not found in listing")
                return

        # -- Step 3: Verify Cloudflare DNS record --
        print(f"\n[3] Verifying Cloudflare DNS record")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records",
                params={"name": full_domain},
                headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"},
            )
            records = resp.json().get("result", [])
            if records:
                rec = records[0]
                print(f"    {rec['name']} -> {rec['content']} (proxied={rec['proxied']})")
            else:
                print(f"    !!! No DNS record found")

        # -- Step 4: Verify nginx config exists --
        print(f"\n[4] Verifying nginx config")
        conf_path = f"/etc/nginx/sites-enabled/wsic-{subdomain}.conf"
        if Path(conf_path).exists():
            print(f"    OK: {conf_path} exists")
        else:
            print(f"    !!! Config not found: {conf_path}")

        # -- Step 5: Verify site files exist --
        print(f"\n[5] Verifying site directory")
        site_path = f"/var/www/wsic-sites/{subdomain}"
        index_path = Path(site_path) / "index.html"
        if index_path.exists():
            print(f"    OK: index.html = {index_path.stat().st_size} bytes")
        else:
            print(f"    !!! index.html not found")

        # -- Step 6: Test HTTPS serving --
        print(f"\n[6] Testing HTTPS serving via localhost")
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(
                f"https://127.0.0.1/",
                headers={"Host": full_domain},
            )
            print(f"    HTTPS status: {resp.status_code}")
            if resp.status_code == 200:
                print(f"    Content length: {len(resp.text)} chars")

        # -- Step 7: Test via GET /sites/{id} --
        print(f"\n[7] GET /wsic/provisioning/sites/{site_id}")
        async with httpx.AsyncClient(timeout=30, cookies=cookies) as client:
            resp = await client.get(f"{BASE_URL}/wsic/provisioning/sites/{site_id}")
            detail = resp.json()
            print(f"    Status: {detail.get('status')}")
            print(f"    SSL: {detail.get('ssl_provisioned')}")
            print(f"    Cloudflare record: {detail.get('cloudflare_record_id')}")

        # -- Step 8: Deprovision via DELETE /sites/{id} --
        print(f"\n[8] DELETE /wsic/provisioning/sites/{site_id}")
        async with httpx.AsyncClient(timeout=60, cookies=cookies) as client:
            resp = await client.delete(f"{BASE_URL}/wsic/provisioning/sites/{site_id}")
            print(f"    HTTP status: {resp.status_code}")
            deprov = resp.json()
            print(f"    Result: {json.dumps(deprov)}")

        # -- Step 9: Verify cleanup --
        print(f"\n[9] Verifying full cleanup...")

        # DNS
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records",
                params={"name": full_domain},
                headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"},
            )
            records = resp.json().get("result", [])
            dns_clean = len(records) == 0
            print(f"    DNS records: {len(records)} {'CLEAN' if dns_clean else 'LEAKED!'}")

        # Nginx
        nginx_clean = not Path(conf_path).exists()
        print(f"    Nginx config: {'CLEAN' if nginx_clean else 'LEAKED!'}")

        # Site files
        files_clean = not Path(site_path).exists()
        print(f"    Site directory: {'CLEAN' if files_clean else 'LEAKED!'}")

        # DB
        async with async_session_factory() as db:
            row = await db.execute(
                text("SELECT status FROM wsic_provisioned_sites WHERE id = :id"),
                {"id": site_id},
            )
            site = row.fetchone()
            db_status = site[0] if site else "NOT FOUND"
            print(f"    DB status: {db_status}")

        all_clean = dns_clean and nginx_clean and files_clean and db_status == "deprovisioned"
        print(f"\n{'=' * 60}")
        print(f"E2E API TEST {'PASSED' if all_clean else 'FAILED'}")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n!!! EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

        # Emergency cleanup via API if we have a site_id
        if site_id:
            print(f"\n[CLEANUP] Attempting API deprovision...")
            try:
                async with httpx.AsyncClient(timeout=60, cookies=cookies) as client:
                    resp = await client.delete(f"{BASE_URL}/wsic/provisioning/sites/{site_id}")
                    print(f"    Deprovision: {resp.status_code}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
