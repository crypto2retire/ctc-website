"""
WSIC Landing Page Provisioning Service.
Handles subdomain provisioning at {slug}.whatshouldicharge.app
and optional custom domain binding.
"""
import logging
import socket
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from services.provisioning_utils import (
    generate_slug,
    validate_subdomain,
    validate_domain,
    cloudflare_add_dns_record,
    cloudflare_delete_dns_record,
    cloudflare_enable_proxy,
    nginx_create_vhost,
    nginx_reload,
    nginx_remove_vhost,
    certbot_provision_ssl,
    create_site_directory,
    write_site_files,
    remove_site_directory,
    quality_gate,
    SERVER_IP,
)

logger = logging.getLogger("hauliq.wsic_provisioning")

WSIC_DOMAIN = "whatshouldicharge.app"
SITE_BASE_PATH = "/var/www/wsic-sites"


async def provision_subdomain(
    db: AsyncSession,
    company_id: int,
    landing_page_id: int,
    business_name: str,
    html_content: str,
    business_context: dict,
) -> dict:
    """
    Full provisioning flow for a WSIC landing page subdomain:
    1. Generate slug, validate uniqueness
    2. Run quality gate
    3. Create site directory, write HTML files
    4. Create Cloudflare DNS record (proxied=false for certbot)
    5. Create nginx vhost (HTTP), reload
    6. Provision SSL via certbot
    7. Upgrade nginx vhost to HTTPS, reload
    8. Enable Cloudflare proxy (DDoS protection)
    9. Insert DB record with status='live'

    Returns dict with status, subdomain, full_url, quality_score.
    """
    subdomain = generate_slug(business_name)
    if not validate_subdomain(subdomain):
        raise ValueError(f"Generated subdomain is invalid: {subdomain}")

    full_domain = f"{subdomain}.{WSIC_DOMAIN}"
    site_path = f"{SITE_BASE_PATH}/{subdomain}"
    conf_filename = f"wsic-{subdomain}.conf"

    # Check uniqueness — append short suffix if taken
    existing = await db.execute(
        text("SELECT id FROM wsic_provisioned_sites WHERE subdomain = :s"),
        {"s": subdomain},
    )
    if existing.fetchone():
        subdomain = f"{subdomain}-{company_id}"
        full_domain = f"{subdomain}.{WSIC_DOMAIN}"
        site_path = f"{SITE_BASE_PATH}/{subdomain}"
        conf_filename = f"wsic-{subdomain}.conf"

    # ── Quality Gate ──
    qg_result = await quality_gate(html_content, "wsic", business_context)
    quality_score = qg_result["score"]
    quality_passed = qg_result["passed"]

    if not quality_passed:
        logger.warning(f"WSIC quality gate failed for {subdomain}: score={quality_score}")
        await db.execute(
            text("""
                INSERT INTO wsic_provisioned_sites
                (company_id, landing_page_id, subdomain, full_subdomain,
                 site_path, quality_score, quality_passed, flagged_for_review, status)
                VALUES (:cid, :lpid, :sub, :full, :sp, :qs, :qp, TRUE, 'flagged_review')
            """),
            {
                "cid": company_id, "lpid": landing_page_id,
                "sub": subdomain, "full": full_domain, "sp": site_path,
                "qs": quality_score, "qp": False,
            },
        )
        await db.commit()
        return {
            "status": "flagged_review",
            "subdomain": subdomain,
            "quality_score": quality_score,
            "quality_feedback": qg_result["feedback"],
            "quality_issues": qg_result["issues"],
        }

    # ── Provision Infrastructure ──
    rollback_steps = []
    try:
        # Step 1: Site directory + files
        create_site_directory(site_path)
        rollback_steps.append(("site_dir", site_path))
        write_site_files(site_path, html_content)

        # Step 2: Cloudflare DNS (proxied=false for certbot)
        zone_id = settings.CLOUDFLARE_ZONE_ID_WSIC
        dns_resp = await cloudflare_add_dns_record(zone_id, full_domain, proxied=False)
        cf_record_id = dns_resp["result"]["id"]
        rollback_steps.append(("dns", zone_id, cf_record_id))

        # Step 3: Nginx HTTP vhost
        conf_path = nginx_create_vhost(full_domain, site_path, conf_filename, ssl=False)
        rollback_steps.append(("nginx", conf_path))

        ok, output = nginx_reload()
        if not ok:
            raise RuntimeError(f"nginx reload failed after HTTP vhost: {output}")

        # Step 4: SSL
        ssl_ok, ssl_output = certbot_provision_ssl(full_domain)
        ssl_provisioned = False
        if not ssl_ok:
            logger.error(f"SSL failed for {full_domain}, site will be HTTP-only: {ssl_output}")
        else:
            ssl_provisioned = True
            # Step 5: Upgrade to HTTPS vhost
            nginx_create_vhost(full_domain, site_path, conf_filename, ssl=True)
            ok2, out2 = nginx_reload()
            if not ok2:
                logger.error(f"nginx reload failed after SSL upgrade: {out2}")

            # Step 6: Enable Cloudflare proxy
            try:
                await cloudflare_enable_proxy(zone_id, cf_record_id)
            except Exception as e:
                logger.error(f"Failed to enable Cloudflare proxy: {e}")

        # Step 7: Insert DB record
        await db.execute(
            text("""
                INSERT INTO wsic_provisioned_sites
                (company_id, landing_page_id, subdomain, full_subdomain,
                 cloudflare_record_id, site_path, nginx_conf_subdomain,
                 ssl_provisioned, quality_score, quality_passed,
                 flagged_for_review, status, provisioned_at)
                VALUES (:cid, :lpid, :sub, :full, :cfid, :sp, :nc,
                        :ssl, :qs, :qp, FALSE, 'live', now())
            """),
            {
                "cid": company_id, "lpid": landing_page_id,
                "sub": subdomain, "full": full_domain,
                "cfid": cf_record_id, "sp": site_path, "nc": conf_path,
                "ssl": ssl_provisioned, "qs": quality_score, "qp": True,
            },
        )
        await db.commit()

        return {
            "status": "live",
            "subdomain": subdomain,
            "full_domain": full_domain,
            "url": f"https://{full_domain}" if ssl_provisioned else f"http://{full_domain}",
            "quality_score": quality_score,
            "ssl": ssl_provisioned,
        }

    except Exception as e:
        logger.error(f"Provisioning failed for {subdomain}, rolling back: {e}")
        await _rollback(rollback_steps)
        await db.execute(
            text("""
                INSERT INTO wsic_provisioned_sites
                (company_id, landing_page_id, subdomain, full_subdomain,
                 site_path, status, error_message)
                VALUES (:cid, :lpid, :sub, :full, :sp, 'error', :err)
                ON CONFLICT (subdomain) DO UPDATE SET
                    status='error', error_message=:err, updated_at=now()
            """),
            {
                "cid": company_id, "lpid": landing_page_id,
                "sub": subdomain, "full": full_domain,
                "sp": site_path, "err": str(e)[:1000],
            },
        )
        await db.commit()
        raise


async def add_custom_domain(
    db: AsyncSession,
    site_id: int,
    custom_domain: str,
) -> dict:
    """Request adding a custom domain. Returns DNS instructions."""
    if not validate_domain(custom_domain):
        raise ValueError(f"Invalid custom domain: {custom_domain}")

    row = await db.execute(
        text("SELECT id, status, site_path FROM wsic_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")
    if site["status"] != "live":
        raise ValueError(f"Site must be live before adding custom domain. Current: {site['status']}")

    return {
        "action": "configure_dns",
        "instructions": f"Add an A record for {custom_domain} pointing to {SERVER_IP}",
        "domain": custom_domain,
        "site_id": site_id,
    }


async def verify_and_provision_custom_domain(
    db: AsyncSession,
    site_id: int,
    custom_domain: str,
) -> dict:
    """Verify DNS and provision nginx/SSL for custom domain."""
    if not validate_domain(custom_domain):
        raise ValueError(f"Invalid domain: {custom_domain}")

    row = await db.execute(
        text("SELECT * FROM wsic_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")

    # Check DNS resolution
    try:
        resolved = socket.gethostbyname(custom_domain)
    except socket.gaierror:
        return {"status": "dns_not_resolved", "domain": custom_domain}

    if resolved != SERVER_IP:
        return {"status": "dns_wrong_ip", "resolved": resolved, "expected": SERVER_IP}

    # DNS verified — provision
    site_path = site["site_path"]
    from services.provisioning_utils import slugify_domain
    slug = slugify_domain(custom_domain)
    conf_filename = f"wsic-custom-{slug}.conf"

    conf_path = nginx_create_vhost(custom_domain, site_path, conf_filename, ssl=False)
    ok, out = nginx_reload()
    if not ok:
        nginx_remove_vhost(conf_path)
        raise RuntimeError(f"nginx reload failed: {out}")

    ssl_ok, ssl_out = certbot_provision_ssl(custom_domain)
    if ssl_ok:
        nginx_create_vhost(custom_domain, site_path, conf_filename, ssl=True)
        nginx_reload()

    await db.execute(
        text("""
            UPDATE wsic_provisioned_sites
            SET custom_domain = :cd, custom_domain_dns_verified = TRUE,
                custom_domain_ssl_provisioned = :ssl, nginx_conf_custom = :nc,
                updated_at = now()
            WHERE id = :id
        """),
        {"cd": custom_domain, "ssl": ssl_ok, "nc": conf_path, "id": site_id},
    )
    await db.commit()

    return {
        "status": "live",
        "custom_domain": custom_domain,
        "ssl": ssl_ok,
        "url": f"https://{custom_domain}" if ssl_ok else f"http://{custom_domain}",
    }


async def deprovision(db: AsyncSession, site_id: int) -> dict:
    """Full teardown: remove nginx, DNS, site files."""
    row = await db.execute(
        text("SELECT * FROM wsic_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")

    # Remove nginx configs
    if site["nginx_conf_subdomain"]:
        nginx_remove_vhost(site["nginx_conf_subdomain"])
    if site["nginx_conf_custom"]:
        nginx_remove_vhost(site["nginx_conf_custom"])
    nginx_reload()

    # Remove Cloudflare DNS
    if site["cloudflare_record_id"]:
        zone_id = settings.CLOUDFLARE_ZONE_ID_WSIC
        try:
            await cloudflare_delete_dns_record(zone_id, site["cloudflare_record_id"])
        except Exception as e:
            logger.error(f"Failed to delete Cloudflare record: {e}")

    # Remove site files
    if site["site_path"]:
        remove_site_directory(site["site_path"])

    await db.execute(
        text("""
            UPDATE wsic_provisioned_sites
            SET status = 'deprovisioned', deprovisioned_at = now(), updated_at = now()
            WHERE id = :id
        """),
        {"id": site_id},
    )
    await db.commit()
    return {"status": "deprovisioned", "site_id": site_id}


async def _rollback(steps: list) -> None:
    """Rollback provisioning steps in reverse order."""
    for step in reversed(steps):
        try:
            if step[0] == "site_dir":
                remove_site_directory(step[1])
            elif step[0] == "dns":
                await cloudflare_delete_dns_record(step[1], step[2])
            elif step[0] == "nginx":
                nginx_remove_vhost(step[1])
                nginx_reload()
        except Exception as e:
            logger.error(f"Rollback step {step[0]} failed: {e}")
