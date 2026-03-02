"""
DoneLocal Website Provisioning Service.
Handles customer-domain provisioning with DNS polling, auto-SSL, and directory listing.
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from services.provisioning_utils import (
    generate_slug,
    slugify_domain,
    validate_domain,
    poll_dns_resolution,
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

logger = logging.getLogger("hauliq.donelocal_provisioning")

SITE_BASE_PATH = "/var/www/donelocal-sites"

# Track active DNS polling tasks (in-process only)
_active_polls: dict[str, asyncio.Task] = {}


async def initiate_provisioning(
    db: AsyncSession,
    company_id: int,
    website_id: int,
    domain: str,
    business_name: str,
    city: str,
    state: str,
    services_list: list[str],
    html_content: str,
    business_context: dict,
) -> dict:
    """
    Step 1: validate domain, run quality gate, create DB record,
    return DNS instructions, start background DNS polling.
    """
    domain = domain.lower().strip()
    if not validate_domain(domain):
        raise ValueError(f"Invalid domain: {domain}")

    domain_slug = generate_slug(f"{business_name}-{city}")
    site_path = f"{SITE_BASE_PATH}/{domain}"

    # Check uniqueness
    existing = await db.execute(
        text("SELECT id FROM donelocal_provisioned_sites WHERE domain = :d"),
        {"d": domain},
    )
    if existing.fetchone():
        raise ValueError(f"Domain already registered: {domain}")

    # Quality Gate
    qg_result = await quality_gate(html_content, "donelocal", business_context)
    quality_score = qg_result["score"]
    quality_passed = qg_result["passed"]
    status = "flagged_review" if not quality_passed else "pending_dns"

    # Insert DB record
    services_json = json.dumps(services_list)
    result = await db.execute(
        text("""
            INSERT INTO donelocal_provisioned_sites
            (company_id, website_id, domain, domain_slug, site_path,
             dns_expected_ip, quality_score, quality_passed, flagged_for_review,
             business_name, city, state, services_list, directory_slug, status)
            VALUES (:cid, :wid, :dom, :dslug, :sp,
                    :ip, :qs, :qp, :flag,
                    :bname, :city, :state, :svc::jsonb, :dirslug, :status)
            RETURNING id
        """),
        {
            "cid": company_id, "wid": website_id,
            "dom": domain, "dslug": domain_slug, "sp": site_path,
            "ip": SERVER_IP, "qs": quality_score, "qp": quality_passed,
            "flag": not quality_passed,
            "bname": business_name, "city": city, "state": state,
            "svc": services_json, "dirslug": domain_slug, "status": status,
        },
    )
    row = result.fetchone()
    site_id = row[0]
    await db.commit()

    if not quality_passed:
        return {
            "status": "flagged_review",
            "site_id": site_id,
            "quality_score": quality_score,
            "quality_feedback": qg_result["feedback"],
            "quality_issues": qg_result["issues"],
        }

    # Start background DNS polling
    task = asyncio.create_task(
        _dns_poll_and_provision(site_id, domain, site_path, html_content)
    )
    _active_polls[str(site_id)] = task

    return {
        "status": "pending_dns",
        "site_id": site_id,
        "domain": domain,
        "dns_instructions": {
            "record_type": "A",
            "host": "@",
            "value": SERVER_IP,
            "note": (
                f"Add an A record for {domain} pointing to {SERVER_IP}. "
                f"We will automatically detect when DNS propagates and provision your site."
            ),
        },
        "quality_score": quality_score,
    }


async def _dns_poll_and_provision(
    site_id: int,
    domain: str,
    site_path: str,
    html_content: str,
    poll_timeout: int = 3600,
) -> None:
    """
    Background task: poll DNS every 30s, then auto-provision when resolved.
    Uses its own DB sessions since the request session is closed.
    """
    from database import async_session_factory

    logger.info(f"Starting DNS poll for {domain} (site_id={site_id})")
    sid = str(site_id)

    try:
        # Update to polling_dns
        async with async_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE donelocal_provisioned_sites
                    SET status = 'polling_dns', dns_poll_started_at = now(), updated_at = now()
                    WHERE id = :id
                """),
                {"id": site_id},
            )
            await db.commit()

        # Poll DNS
        resolved = await poll_dns_resolution(
            domain, expected_ip=SERVER_IP, interval=30, timeout=poll_timeout
        )

        if not resolved:
            async with async_session_factory() as db:
                await db.execute(
                    text("""
                        UPDATE donelocal_provisioned_sites
                        SET status = 'dns_timeout', error_message = 'DNS polling timed out',
                            updated_at = now()
                        WHERE id = :id
                    """),
                    {"id": site_id},
                )
                await db.commit()
            logger.warning(f"DNS polling timed out for {domain}")
            return

        # DNS resolved — proceed with provisioning
        async with async_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE donelocal_provisioned_sites
                    SET status = 'dns_verified', dns_verified = TRUE,
                        dns_verified_at = now(), updated_at = now()
                    WHERE id = :id
                """),
                {"id": site_id},
            )
            await db.commit()

        # Create site directory and write files
        create_site_directory(site_path)
        write_site_files(site_path, html_content)

        # Inject "Powered by DoneLocal" footer
        _inject_footer(site_path)

        # Nginx HTTP vhost
        slug = slugify_domain(domain)
        conf_filename = f"donelocal-{slug}.conf"
        conf_path = nginx_create_vhost(domain, site_path, conf_filename, ssl=False)

        ok, out = nginx_reload()
        if not ok:
            raise RuntimeError(f"nginx reload failed: {out}")

        # SSL
        ssl_ok, ssl_out = certbot_provision_ssl(domain)
        ssl_provisioned = False
        if ssl_ok:
            ssl_provisioned = True
            nginx_create_vhost(domain, site_path, conf_filename, ssl=True)
            ok2, out2 = nginx_reload()
            if not ok2:
                logger.error(f"nginx reload failed after SSL: {out2}")

        # Update DB to live
        async with async_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE donelocal_provisioned_sites
                    SET status = 'live', ssl_provisioned = :ssl,
                        nginx_conf_path = :nc, provisioned_at = now(), updated_at = now()
                    WHERE id = :id
                """),
                {"ssl": ssl_provisioned, "nc": conf_path, "id": site_id},
            )
            await db.commit()

        # Generate directory listing
        await _generate_directory_entry(site_id)

        logger.info(f"DoneLocal site provisioned: {domain} (ssl={ssl_provisioned})")

    except Exception as e:
        logger.error(f"DoneLocal provisioning failed for {domain}: {e}", exc_info=True)
        async with async_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE donelocal_provisioned_sites
                    SET status = 'error', error_message = :err, updated_at = now()
                    WHERE id = :id
                """),
                {"err": str(e)[:1000], "id": site_id},
            )
            await db.commit()
    finally:
        _active_polls.pop(sid, None)


def _inject_footer(site_path: str) -> None:
    """Inject 'Powered by DoneLocal' footer before </body>."""
    index_file = Path(site_path) / "index.html"
    if not index_file.exists():
        return

    content = index_file.read_text(encoding="utf-8")
    footer_html = (
        '\n<footer style="text-align:center;padding:20px;font-size:13px;color:#888;'
        'border-top:1px solid #eee;margin-top:40px;">'
        '  Powered by <a href="https://donelocal.io" style="color:#2563eb;'
        'text-decoration:none;">DoneLocal</a>'
        '</footer>\n'
    )
    if "Powered by" not in content and "</body>" in content:
        content = content.replace("</body>", f"{footer_html}</body>")
        index_file.write_text(content, encoding="utf-8")
        logger.info(f"DoneLocal footer injected into {index_file}")


async def _generate_directory_entry(site_id: int) -> None:
    """Generate directory listing HTML for donelocal.io/directory/{slug}."""
    from database import async_session_factory

    async with async_session_factory() as db:
        row = await db.execute(
            text("SELECT * FROM donelocal_provisioned_sites WHERE id = :id"),
            {"id": site_id},
        )
        site = row.mappings().fetchone()
        if not site:
            return

        business_name = site["business_name"] or "Business"
        city = site["city"] or ""
        state = site["state"] or ""
        raw_services = site["services_list"]
        if isinstance(raw_services, str):
            services_list = json.loads(raw_services)
        elif isinstance(raw_services, list):
            services_list = raw_services
        else:
            services_list = []
        domain = site["domain"]
        directory_slug = site["directory_slug"]
        protocol = "https" if site["ssl_provisioned"] else "http"

        services_html = "".join(f"<li>{svc}</li>" for svc in services_list)
        services_schema = ", ".join(services_list)

        seo_title = f"{business_name} - {city}, {state}"
        seo_description = (
            f"{business_name} in {city}, {state} offers {services_schema}. "
            f"Visit their website at {domain}."
        )

        # Escape for HTML/JSON safety
        import html as html_mod
        safe_name = html_mod.escape(business_name)
        safe_city = html_mod.escape(city)
        safe_state = html_mod.escape(state)
        safe_title = html_mod.escape(seo_title)
        safe_desc = html_mod.escape(seo_description)

        knows_about = ", ".join(f'"{html_mod.escape(s)}"' for s in services_list)

        directory_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title} | DoneLocal Directory</title>
    <meta name="description" content="{safe_desc}">
    <script type="application/ld+json">
    {{
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": "{safe_name}",
        "address": {{
            "@type": "PostalAddress",
            "addressLocality": "{safe_city}",
            "addressRegion": "{safe_state}"
        }},
        "sameAs": ["{protocol}://{domain}"],
        "description": "{safe_desc}",
        "knowsAbout": [{knows_about}]
    }}
    </script>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 800px;
               margin: 0 auto; padding: 40px 20px; color: #1a1a1a; }}
        h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        .location {{ color: #666; font-size: 1.1rem; margin-bottom: 2rem; }}
        .services {{ list-style: none; padding: 0; }}
        .services li {{ padding: 8px 0; border-bottom: 1px solid #eee; }}
        .visit-btn {{ display: inline-block; margin-top: 2rem; padding: 12px 32px;
                      background: #2563eb; color: white; text-decoration: none;
                      border-radius: 8px; font-size: 1rem; }}
        .visit-btn:hover {{ background: #1d4ed8; }}
        footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #eee;
                  color: #999; font-size: 0.85rem; }}
    </style>
</head>
<body>
    <h1>{safe_name}</h1>
    <p class="location">{safe_city}, {safe_state}</p>
    <h2>Services</h2>
    <ul class="services">{services_html}</ul>
    <a class="visit-btn" href="{protocol}://{domain}" target="_blank" rel="noopener">
        Visit Website &rarr;
    </a>
    <footer>Listed on <a href="https://donelocal.io">DoneLocal</a> directory.</footer>
</body>
</html>"""

        await db.execute(
            text("""
                UPDATE donelocal_provisioned_sites
                SET directory_html = :html, directory_published = TRUE, updated_at = now()
                WHERE id = :id
            """),
            {"html": directory_html, "id": site_id},
        )
        await db.commit()
        logger.info(f"Directory entry generated for {business_name} at /directory/{directory_slug}")


async def check_polling_status(db: AsyncSession, site_id: int) -> dict:
    """Check current provisioning status. Frontend polls this."""
    row = await db.execute(
        text("""
            SELECT id, domain, status, dns_verified, ssl_provisioned,
                   error_message, provisioned_at, quality_score
            FROM donelocal_provisioned_sites WHERE id = :id
        """),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")

    result = dict(site)
    if site["status"] == "live":
        protocol = "https" if site["ssl_provisioned"] else "http"
        result["url"] = f"{protocol}://{site['domain']}"
    return result


async def retry_provisioning(db: AsyncSession, site_id: int) -> dict:
    """Retry a failed or timed-out provisioning."""
    row = await db.execute(
        text("SELECT * FROM donelocal_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")
    if site["status"] not in ("error", "dns_timeout"):
        raise ValueError(f"Can only retry error or dns_timeout. Current: {site['status']}")

    # Re-read HTML from generated_websites
    html_row = await db.execute(
        text("SELECT homepage_html FROM generated_websites WHERE id = :wid"),
        {"wid": site["website_id"]},
    )
    html_data = html_row.fetchone()
    html_content = html_data[0] if html_data else ""

    await db.execute(
        text("""
            UPDATE donelocal_provisioned_sites
            SET status = 'pending_dns', error_message = NULL, updated_at = now()
            WHERE id = :id
        """),
        {"id": site_id},
    )
    await db.commit()

    task = asyncio.create_task(
        _dns_poll_and_provision(site_id, site["domain"], site["site_path"], html_content)
    )
    _active_polls[str(site_id)] = task

    return {"status": "polling_restarted", "site_id": site_id}


async def deprovision(db: AsyncSession, site_id: int) -> dict:
    """Full teardown of a DoneLocal site."""
    row = await db.execute(
        text("SELECT * FROM donelocal_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise ValueError(f"Site not found: {site_id}")

    # Cancel active polling
    sid = str(site_id)
    if sid in _active_polls:
        _active_polls[sid].cancel()
        _active_polls.pop(sid, None)

    # Remove nginx
    if site["nginx_conf_path"]:
        nginx_remove_vhost(site["nginx_conf_path"])
        nginx_reload()

    # Remove site files
    if site["site_path"]:
        remove_site_directory(site["site_path"])

    await db.execute(
        text("""
            UPDATE donelocal_provisioned_sites
            SET status = 'deprovisioned', directory_published = FALSE,
                deprovisioned_at = now(), updated_at = now()
            WHERE id = :id
        """),
        {"id": site_id},
    )
    await db.commit()
    return {"status": "deprovisioned", "site_id": site_id}


async def restart_pending_polls() -> None:
    """Restart DNS polling for sites that were mid-poll when process stopped."""
    from database import async_session_factory

    async with async_session_factory() as db:
        rows = await db.execute(
            text("""
                SELECT dp.id, dp.domain, dp.site_path, gw.homepage_html
                FROM donelocal_provisioned_sites dp
                LEFT JOIN generated_websites gw ON gw.id = dp.website_id
                WHERE dp.status IN ('pending_dns', 'polling_dns')
            """)
        )
        for row in rows.mappings().all():
            html = row["homepage_html"] or ""
            task = asyncio.create_task(
                _dns_poll_and_provision(
                    row["id"], row["domain"], row["site_path"], html
                )
            )
            _active_polls[str(row["id"])] = task
            logger.info(f"Restarted DNS polling for {row['domain']}")
