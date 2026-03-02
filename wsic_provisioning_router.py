"""
API router for WSIC Landing Page provisioning.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, AuthContext
from database import get_db
from services import wsic_provisioning_service as wsic_svc

logger = logging.getLogger("hauliq.wsic_provisioning_router")

router = APIRouter(prefix="/wsic/provisioning", tags=["WSIC Provisioning"])


class ProvisionSubdomainRequest(BaseModel):
    landing_page_id: int
    business_name: str = Field(..., min_length=2, max_length=100)


class AddCustomDomainRequest(BaseModel):
    site_id: int
    custom_domain: str = Field(..., min_length=4, max_length=253)


class VerifyCustomDomainRequest(BaseModel):
    site_id: int
    custom_domain: str


@router.post("/provision")
async def provision_subdomain(
    req: ProvisionSubdomainRequest,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Provision a WSIC landing page at {slug}.whatshouldicharge.app."""
    # Fetch the landing page HTML
    row = await db.execute(
        text("""
            SELECT html_content, company_id FROM wsic_landing_pages
            WHERE id = :id AND company_id = :cid
        """),
        {"id": req.landing_page_id, "cid": auth.company_id},
    )
    page = row.mappings().fetchone()
    if not page:
        raise HTTPException(404, "Landing page not found")

    # Fetch company context for quality gate
    co_row = await db.execute(
        text("SELECT name, industry, city, state FROM companies WHERE id = :cid"),
        {"cid": auth.company_id},
    )
    company = co_row.mappings().fetchone()
    business_context = dict(company) if company else {}

    try:
        result = await wsic_svc.provision_subdomain(
            db=db,
            company_id=auth.company_id,
            landing_page_id=req.landing_page_id,
            business_name=req.business_name,
            html_content=page["html_content"],
            business_context=business_context,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, f"Provisioning error: {e}")


@router.post("/custom-domain")
async def add_custom_domain(
    req: AddCustomDomainRequest,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Request custom domain addition. Returns DNS instructions."""
    # Verify ownership
    row = await db.execute(
        text("SELECT id FROM wsic_provisioned_sites WHERE id = :id AND company_id = :cid"),
        {"id": req.site_id, "cid": auth.company_id},
    )
    if not row.fetchone():
        raise HTTPException(404, "Site not found")

    try:
        result = await wsic_svc.add_custom_domain(db, req.site_id, req.custom_domain)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/custom-domain/verify")
async def verify_custom_domain(
    req: VerifyCustomDomainRequest,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify DNS and provision custom domain nginx/SSL."""
    row = await db.execute(
        text("SELECT id FROM wsic_provisioned_sites WHERE id = :id AND company_id = :cid"),
        {"id": req.site_id, "cid": auth.company_id},
    )
    if not row.fetchone():
        raise HTTPException(404, "Site not found")

    try:
        result = await wsic_svc.verify_and_provision_custom_domain(
            db, req.site_id, req.custom_domain
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, f"Provisioning error: {e}")


@router.get("/sites")
async def list_sites(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all provisioned WSIC sites for the current company."""
    rows = await db.execute(
        text("""
            SELECT id, subdomain, full_subdomain, custom_domain, status,
                   ssl_provisioned, quality_score, quality_passed, flagged_for_review,
                   provisioned_at, created_at
            FROM wsic_provisioned_sites
            WHERE company_id = :cid
            ORDER BY created_at DESC
        """),
        {"cid": auth.company_id},
    )
    return [dict(r) for r in rows.mappings().all()]


@router.get("/sites/{site_id}")
async def get_site(
    site_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details of a specific provisioned site."""
    row = await db.execute(
        text("""
            SELECT * FROM wsic_provisioned_sites
            WHERE id = :id AND company_id = :cid
        """),
        {"id": site_id, "cid": auth.company_id},
    )
    site = row.mappings().fetchone()
    if not site:
        raise HTTPException(404, "Site not found")
    return dict(site)


@router.delete("/sites/{site_id}")
async def deprovision_site(
    site_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deprovision a WSIC site (remove DNS, nginx, SSL, files)."""
    row = await db.execute(
        text("SELECT id FROM wsic_provisioned_sites WHERE id = :id AND company_id = :cid"),
        {"id": site_id, "cid": auth.company_id},
    )
    if not row.fetchone():
        raise HTTPException(404, "Site not found")

    result = await wsic_svc.deprovision(db, site_id)
    return result
