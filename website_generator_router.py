"""
Website Generator Router
POST /api/v1/website/generate  — generate full site using mandatory competitor research
GET  /api/v1/website/status    — check if site exists, get download URL
GET  /api/v1/website/download  — download ZIP of generated site
"""
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, AuthContext
from database import get_db
from config import settings
from services.competitor_research import (
    run_competitor_research,
    format_report_for_prompt,
    CompetitorReport,
)

logger = logging.getLogger("hauliq.website")
router = APIRouter(prefix="/website", tags=["Website Generator"])

# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_company_data(company_id: int, db: AsyncSession) -> dict:
    result = await db.execute(text("""
        SELECT name, city, state, industry, phone, email, brand_voice
        FROM companies WHERE id = :cid
    """), {"cid": company_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(400, "Company not found")

    return {
        "name":        row[0] or "",
        "city":        row[1] or "",
        "state":       row[2] or "",
        "industry":    row[3] or "junk removal",
        "phone":       row[4] or "",
        "email":       row[5] or "",
        "brand_voice": row[6] or "",
    }


def build_prompt(company: dict, report: CompetitorReport) -> str:
    competitor_section = format_report_for_prompt(report)

    phone = company['phone'] or "PHONE_NOT_SET"
    email = company['email'] or "EMAIL_NOT_SET"

    return f"""You are an expert local SEO web developer. Generate a complete, production-ready, visually stunning HTML homepage for this local business. This must look like a professionally designed $5,000 website — not a template.

CRITICAL — USE THESE EXACT DETAILS, NO PLACEHOLDERS EVER:
- Business Name: {company['name']}
- Phone: {phone} ← USE THIS EXACT NUMBER EVERYWHERE. NEVER use (555) numbers or fake numbers.
- Email: {email}
- City: {company['city']}, {company['state']}
- Industry: {company['industry']}
- Brand voice: {company['brand_voice'] or 'friendly, professional, locally owned and operated'}

VERIFICATION RULE: If you include ANY specific number, statistic, price, year, award,
certification, staff count, or factual claim that is NOT explicitly provided in the
business data above and NOT directly sourced from the competitor analysis below,
you MUST wrap it in [VERIFY: explanation of what needs checking].
Examples: [VERIFY: years in business], [VERIFY: exact pricing], [VERIFY: staff count].
Do NOT fabricate review counts, star ratings, response times, or completion stats.
For customer reviews: do NOT generate fake reviews. Instead include a "Review Us on Google"
CTA section with a link to the Google Business Profile.

{competitor_section}

COMPETITOR GAPS (include all of these to beat them):
- JSON-LD Schema markup (LocalBusiness + FAQPage + BreadcrumbList)
- Neighborhood / area-specific content section
- Prominent customer reviews CTA section
- Transparent pricing guide with ranges
- Trust signals: awards, review count, years in business
- More FAQs with longer, more helpful answers than any competitor
- More services listed with deeper descriptions

DESIGN REQUIREMENTS — follow exactly:
- Fonts: Archivo Black (headings) + Lato (body) from Google Fonts
- Colors: forest green #1c4a28, amber #f0b429, cream #f8f5ef, white, near-black #111810
- Sticky header: logo left, nav center, phone number as amber CTA button right
- Mobile hamburger menu with working JS toggle
- Hero: full-width dark green background, large h1, subheadline, TWO CTA buttons (call + learn more), trust stats row
- Services: grid of 8+ cards with emoji icons, title, 2-sentence description
- How It Works: 4-step numbered process section
- Review CTA: "See Our Reviews on Google" section with link, NOT fake reviews
- Service Areas: grid of 10+ local city pills
- FAQ: accordion with {report.generation_targets.get('min_faq_count', 8)}+ questions and working JS, JSON-LD FAQPage schema
- Final CTA: full-width amber strip with large phone number
- Footer: 4 columns — about, services, areas, contact+hours

CSS QUALITY REQUIREMENTS:
- Smooth hover effects (transform, box-shadow transitions) on all cards and buttons
- CSS keyframe animations on hero stats (fadeInUp)
- CSS Grid for service cards and review grid
- Subtle gradients on hero and CTA sections
- Professional spacing — generous padding, clear visual hierarchy
- Mobile responsive with breakpoints at 768px and 480px
- All buttons min-height 44px for touch targets
- Form inputs font-size 16px (prevents iOS zoom)
- overflow-x:hidden on html,body

WORD COUNT TARGET: Minimum {report.generation_targets.get('min_word_count', 1500)} words of content to outrank competitors.

Return ONLY the complete HTML document. Start with <!DOCTYPE html>. No markdown. No explanation. No code fences."""


def generate_style_css() -> str:
    return """@import url('https://fonts.googleapis.com/css2?family=Archivo+Black&family=Lato:wght@300;400;700;900&display=swap');
:root{--ink:#111810;--forest:#1c4a28;--green:#27662f;--leaf:#38883f;--sage:#6ab370;--mint:#e8f5ea;--cream:#f8f5ef;--warm:#ede8df;--amber:#f0b429;--gold:#c9920f;--white:#ffffff;--gray:#6b7265;--border:#e2e8df;--shadow:rgba(17,24,16,0.08)}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;overflow-x:hidden}
body{font-family:'Lato',sans-serif;color:var(--ink);background:var(--white);line-height:1.6;overflow-x:hidden}
a,button,input,select,textarea{touch-action:manipulation}
img{max-width:100%;display:block}
a{text-decoration:none;color:inherit}
h1,h2,h3,h4,h5{font-family:'Archivo Black',sans-serif;line-height:1.05}
h1{font-size:clamp(2.4rem,5vw,4.8rem)}
h2{font-size:clamp(1.8rem,3.5vw,3rem)}
h3{font-size:clamp(1.2rem,2vw,1.6rem)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;border-radius:8px;font-family:'Lato',sans-serif;font-weight:900;font-size:0.95rem;cursor:pointer;transition:transform 0.15s,box-shadow 0.15s;text-decoration:none;border:none;min-height:44px}
.btn:hover{transform:translateY(-2px)}
.btn-primary{background:var(--amber);color:var(--ink);box-shadow:0 4px 20px rgba(240,180,41,0.3)}
.btn-primary:hover{background:var(--gold)}
.btn-dark{background:var(--ink);color:var(--white)}
.wrap{max-width:1280px;margin:0 auto;padding:0 28px}
.section{padding:88px 28px}
input,textarea,select{font-size:16px}
@media(max-width:768px){.section{padding:56px 20px}}"""


def generate_site_js() -> str:
    return """function toggleMenu(){document.getElementById('mobileNav').classList.toggle('open')}
function toggleFaq(btn){const ans=btn.nextElementSibling;const open=ans.classList.contains('open');document.querySelectorAll('.faq-ans').forEach(a=>a.classList.remove('open'));document.querySelectorAll('.faq-btn').forEach(b=>b.classList.remove('open'));if(!open){ans.classList.add('open');btn.classList.add('open')}}
document.addEventListener('DOMContentLoaded',()=>{const first=document.querySelector('.faq-btn');if(first)first.click()})"""


def build_zip(pages: dict) -> bytes:
    """Build ZIP bytes from a dict of {filename: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in pages.items():
            zf.writestr(filename, content)
    buf.seek(0)
    return buf.read()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_website(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    company = await get_company_data(auth.company_id, db)

    if not company["name"] or not company["city"]:
        raise HTTPException(400, "Company name and city are required. Please update Settings first.")

    # Mandatory competitor research before generation
    logger.info(f"Running competitor research for {company['industry']} in {company['city']}, {company['state']}")
    report = await run_competitor_research(
        service=company["industry"],
        city=company["city"],
        state=company["state"],
        company_id=auth.company_id,
        db=db,
    )
    logger.info(f"Competitor research complete: {len(report.competitors)} competitors analyzed")

    prompt = build_prompt(company, report)

    # Use Sonnet for higher quality output
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}]
        )
        homepage_html = message.content[0].text.strip()

        # Strip markdown fences if model wrapped in them
        if homepage_html.startswith("```"):
            homepage_html = homepage_html.split("\n", 1)[1]
            if homepage_html.endswith("```"):
                homepage_html = homepage_html.rsplit("```", 1)[0]
        homepage_html = homepage_html.strip()

    except Exception as e:
        logger.error(f"Claude generation failed: {e}")
        raise HTTPException(500, f"AI generation failed: {str(e)}")

    # Build ZIP
    pages = {
        "index.html": homepage_html,
        "style.css":  generate_style_css(),
        "site.js":    generate_site_js(),
    }
    zip_bytes = build_zip(pages)

    # Store in DB
    try:
        await db.rollback()  # Clear any aborted transaction state
        existing = await db.execute(text(
            "SELECT id FROM generated_websites WHERE company_id = :cid LIMIT 1"
        ), {"cid": auth.company_id})
        row = existing.fetchone()

        if row:
            await db.execute(text("""
                UPDATE generated_websites
                SET homepage_html = :html, zip_data = :zip,
                    competitor_count = :cc, generated_at = NOW()
                WHERE company_id = :cid
            """), {
                "cid": auth.company_id,
                "html": homepage_html,
                "zip": zip_bytes,
                "cc": len(report.competitors),
            })
        else:
            await db.execute(text("""
                INSERT INTO generated_websites
                    (company_id, homepage_html, zip_data, competitor_count, generated_at)
                VALUES (:cid, :html, :zip, :cc, NOW())
            """), {
                "cid": auth.company_id,
                "html": homepage_html,
                "zip": zip_bytes,
                "cc": len(report.competitors),
            })
        await db.commit()
    except Exception as e:
        logger.warning(f"Could not save to DB: {e}")

    return {
        "ok": True,
        "preview_html": homepage_html,
        "competitor_count": len(report.competitors),
        "competitors_used": [c.name for c in report.competitors],
        "generation_targets": report.generation_targets,
        "company": company["name"],
        "city": company["city"],
    }


@router.get("/status")
async def website_status(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(text("""
            SELECT generated_at, competitor_count
            FROM generated_websites WHERE company_id = :cid LIMIT 1
        """), {"cid": auth.company_id})
        row = result.fetchone()
        if row:
            return {
                "exists": True,
                "generated_at": str(row[0]),
                "competitor_count": row[1],
            }
    except Exception:
        pass
    return {"exists": False}


@router.get("/download")
async def download_website(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT zip_data, generated_at
        FROM generated_websites WHERE company_id = :cid LIMIT 1
    """), {"cid": auth.company_id})
    row = result.fetchone()

    if not row or not row[0]:
        raise HTTPException(404, "No generated website found. Please generate one first.")

    zip_bytes = bytes(row[0])
    filename  = f"website-{auth.company_id}.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
        }
    )
