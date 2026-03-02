"""
API router for DoneLocal Website provisioning + public directory pages.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, AuthContext
from database import get_db
from services import donelocal_provisioning_service as dl_svc

logger = logging.getLogger("hauliq.donelocal_provisioning_router")

router = APIRouter(prefix="/donelocal/provisioning", tags=["DoneLocal Provisioning"])


class InitiateProvisioningRequest(BaseModel):
    website_id: int
    domain: str = Field(..., min_length=4, max_length=253)
    business_name: str = Field(..., min_length=2, max_length=255)
    city: str = Field(..., min_length=1, max_length=128)
    state: str = Field(..., min_length=1, max_length=64)
    services_list: List[str] = Field(default_factory=list)


@router.post("/provision")
async def initiate_provisioning(
    req: InitiateProvisioningRequest,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiate DoneLocal website provisioning. Returns DNS instructions."""
    # Fetch the website HTML
    row = await db.execute(
        text("""
            SELECT homepage_html FROM generated_websites
            WHERE id = :id AND company_id = :cid
        """),
        {"id": req.website_id, "cid": auth.company_id},
    )
    website = row.mappings().fetchone()
    if not website:
        raise HTTPException(404, "Website not found")

    # Fetch company context
    co_row = await db.execute(
        text("SELECT name, industry, city, state FROM companies WHERE id = :cid"),
        {"cid": auth.company_id},
    )
    company = co_row.mappings().fetchone()
    business_context = dict(company) if company else {}

    try:
        result = await dl_svc.initiate_provisioning(
            db=db,
            company_id=auth.company_id,
            website_id=req.website_id,
            domain=req.domain,
            business_name=req.business_name,
            city=req.city,
            state=req.state,
            services_list=req.services_list,
            html_content=website["homepage_html"] or "",
            business_context=business_context,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/status/{site_id}")
async def check_status(
    site_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check provisioning status. Frontend polls this every few seconds."""
    row = await db.execute(
        text("SELECT company_id FROM donelocal_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    owner = row.fetchone()
    if not owner or owner[0] != auth.company_id:
        raise HTTPException(404, "Site not found")

    result = await dl_svc.check_polling_status(db, site_id)
    return result


@router.post("/retry/{site_id}")
async def retry_provisioning(
    site_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retry a failed or timed-out provisioning."""
    row = await db.execute(
        text("SELECT company_id FROM donelocal_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    owner = row.fetchone()
    if not owner or owner[0] != auth.company_id:
        raise HTTPException(404, "Site not found")

    try:
        result = await dl_svc.retry_provisioning(db, site_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/sites")
async def list_sites(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all DoneLocal provisioned sites for the current company."""
    rows = await db.execute(
        text("""
            SELECT id, domain, domain_slug, status, dns_verified,
                   ssl_provisioned, quality_score, quality_passed,
                   flagged_for_review, directory_published,
                   provisioned_at, created_at, error_message
            FROM donelocal_provisioned_sites
            WHERE company_id = :cid
            ORDER BY created_at DESC
        """),
        {"cid": auth.company_id},
    )
    return [dict(r) for r in rows.mappings().all()]


@router.delete("/sites/{site_id}")
async def deprovision_site(
    site_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deprovision a DoneLocal site."""
    row = await db.execute(
        text("SELECT company_id FROM donelocal_provisioned_sites WHERE id = :id"),
        {"id": site_id},
    )
    owner = row.fetchone()
    if not owner or owner[0] != auth.company_id:
        raise HTTPException(404, "Site not found")

    result = await dl_svc.deprovision(db, site_id)
    return result


# ── Directory Pages (public, no auth) ──────────────────────────

directory_router = APIRouter(tags=["DoneLocal Directory"])


@directory_router.get("/directory/{slug}", response_class=HTMLResponse)
async def serve_directory_page(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve public directory listing at donelocal.io/directory/{slug}."""
    row = await db.execute(
        text("""
            SELECT directory_html FROM donelocal_provisioned_sites
            WHERE directory_slug = :slug AND directory_published = TRUE AND status = 'live'
        """),
        {"slug": slug},
    )
    site = row.fetchone()
    if not site or not site[0]:
        raise HTTPException(404, "Directory listing not found")
    return HTMLResponse(content=site[0])


@directory_router.get("/directory", response_class=HTMLResponse)
async def directory_index(
    db: AsyncSession = Depends(get_db),
):
    """Directory index listing all live DoneLocal businesses."""
    rows = await db.execute(
        text("""
            SELECT directory_slug, business_name, city, state, domain
            FROM donelocal_provisioned_sites
            WHERE directory_published = TRUE AND status = 'live'
            ORDER BY business_name
        """)
    )
    businesses = rows.mappings().all()

    listings_html = ""
    for biz in businesses:
        listings_html += (
            f'<div class="listing">'
            f'<h3><a href="/directory/{biz["directory_slug"]}">{biz["business_name"]}</a></h3>'
            f'<p>{biz["city"]}, {biz["state"]} &mdash; '
            f'<a href="https://{biz["domain"]}" target="_blank">{biz["domain"]}</a></p>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DoneLocal Business Directory</title>
    <meta name="description" content="Find local businesses powered by DoneLocal.">
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 40px 20px; }}
        .listing {{ padding: 16px 0; border-bottom: 1px solid #eee; }}
        .listing h3 a {{ color: #2563eb; text-decoration: none; }}
        .listing h3 a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>DoneLocal Business Directory</h1>
    <p>Local businesses with websites powered by DoneLocal.</p>
    {listings_html if listings_html else '<p>No businesses listed yet.</p>'}
</body>
</html>"""
    return HTMLResponse(content=html)
