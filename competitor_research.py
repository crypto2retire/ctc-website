"""
Competitor Research Service — Mandatory pre-generation competitor analysis.
Searches Tavily, scrapes Firecrawl, analyzes with Claude Haiku.
Returns structured CompetitorReport with generation targets.
"""
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from config import settings

logger = logging.getLogger("hauliq.competitor_research")

# ── Filter lists (from market_agent.py) ───────────────────────────────────────

NATIONAL_CHAINS = [
    "loadup.com", "1800gotjunk.com", "junkluggers.com", "collegehunkshaulingjunk.com",
    "junkking.com", "junk-king.com", "cjunk.com", "justjunk.com", "geojunk.com",
]

DIRECTORY_DOMAINS = [
    "yelp.com", "yellowpages.com", "bbb.org", "angieslist.com", "angi.com",
    "homeadvisor.com", "thumbtack.com", "houzz.com", "porch.com", "bark.com",
    "expertise.com", "homeblue.com", "improvenet.com", "networx.com",
    "tripadvisor.com", "google.com", "facebook.com", "instagram.com",
    "nextdoor.com", "reddit.com", "amazon.com", "checkatrade.com",
    "taskrabbit.com", "mapquest.com", "manta.com", "superpages.com",
    "citysearch.com", "merchantcircle.com", "chamberofcommerce.com",
]


def _get_root_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        parts = parsed.netloc.replace("www.", "").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return parsed.netloc
    except Exception:
        return url


def _is_excluded(url: str) -> bool:
    root = _get_root_domain(url)
    for d in NATIONAL_CHAINS + DIRECTORY_DOMAINS:
        if d in root:
            return True
    return False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CompetitorAnalysis:
    name: str
    url: str
    is_local: bool = True
    is_chain: bool = False
    word_count: int = 0
    faq_count: int = 0
    avg_faq_answer_length: int = 0
    services_listed: List[str] = field(default_factory=list)
    unique_claims: List[str] = field(default_factory=list)
    trust_signals: List[str] = field(default_factory=list)
    has_schema_markup: bool = False
    schema_types: List[str] = field(default_factory=list)
    pricing_transparency: str = "none"
    pricing_details: str = ""
    gaps: List[str] = field(default_factory=list)
    content_sample: str = ""


@dataclass
class CompetitorReport:
    service: str
    city: str
    state: str
    competitors: List[CompetitorAnalysis] = field(default_factory=list)
    max_word_count: int = 0
    max_faq_count: int = 0
    max_services_count: int = 0
    common_gaps: List[str] = field(default_factory=list)
    generation_targets: dict = field(default_factory=dict)


# ── Tavily Search ─────────────────────────────────────────────────────────────

async def search_competitors(
    service: str, city: str, state: str, max_results: int = 15
) -> list:
    """Search Tavily for local competitors. Returns raw result list."""
    query = f"{service} {city} {state}"
    exclude = list(set(NATIONAL_CHAINS + DIRECTORY_DOMAINS))

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                    "exclude_domains": exclude,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            logger.info(f"Tavily returned {len(results)} results for '{query}'")
            return results
    except Exception as e:
        logger.error(f"Tavily search failed: {e}")
        return []


# ── Firecrawl Scrape ──────────────────────────────────────────────────────────

async def scrape_competitor(url: str) -> str:
    """Scrape a competitor URL with Firecrawl. Returns markdown content."""
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}"},
                json={
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "waitFor": 1000,
                },
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            markdown = data.get("markdown", "")
            logger.info(f"Scraped {url}: {len(markdown)} chars")
            return markdown
    except Exception as e:
        logger.error(f"Firecrawl scrape failed for {url}: {e}")
        return ""


# ── Claude Haiku Analysis ─────────────────────────────────────────────────────

async def analyze_competitor(
    name: str, url: str, markdown: str, service: str, city: str, state: str
) -> CompetitorAnalysis:
    """Deep-analyze a single competitor's scraped content with Claude Haiku."""
    prompt = f"""Analyze this competitor website for a local {service} business in {city}, {state}.
Competitor: {name}
URL: {url}

Content (scraped markdown):
{markdown[:8000]}

Return ONLY valid JSON (no markdown fences, no extra text):
{{
  "is_local": <true if locally owned serving 1-5 cities, false if national/franchise>,
  "is_chain": <true if franchise or national chain>,
  "word_count": <estimated total word count of main content>,
  "faq_count": <number of FAQ questions found, 0 if none>,
  "avg_faq_answer_length": <average words per FAQ answer, 0 if no FAQs>,
  "services_listed": ["service1", "service2", ...],
  "unique_claims": ["claim1", "claim2", ...],
  "trust_signals": ["signal1", "signal2", ...],
  "has_schema_markup": <true if JSON-LD or schema.org markup detected>,
  "schema_types": ["LocalBusiness", "FAQPage", ...],
  "pricing_transparency": "<none|ranges|exact>",
  "pricing_details": "<brief summary of any pricing shown>",
  "gaps": ["gap1", "gap2", ...]
}}

For "gaps", list things this competitor does NOT have that a better site would include:
missing schema markup, no FAQ section, no pricing info, no service area page,
thin content, no customer reviews section, no process explanation, etc."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()

            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            data = json.loads(text)

            return CompetitorAnalysis(
                name=name,
                url=url,
                is_local=data.get("is_local", True),
                is_chain=data.get("is_chain", False),
                word_count=data.get("word_count", 0),
                faq_count=data.get("faq_count", 0),
                avg_faq_answer_length=data.get("avg_faq_answer_length", 0),
                services_listed=data.get("services_listed", []),
                unique_claims=data.get("unique_claims", []),
                trust_signals=data.get("trust_signals", []),
                has_schema_markup=data.get("has_schema_markup", False),
                schema_types=data.get("schema_types", []),
                pricing_transparency=data.get("pricing_transparency", "none"),
                pricing_details=data.get("pricing_details", ""),
                gaps=data.get("gaps", []),
                content_sample=markdown[:2000],
            )
    except Exception as e:
        logger.error(f"Competitor analysis failed for {name} ({url}): {e}")
        return CompetitorAnalysis(
            name=name, url=url, content_sample=markdown[:2000],
            gaps=["Analysis failed — treat as unknown competitor"],
        )


# ── Full Pipeline ─────────────────────────────────────────────────────────────

async def run_competitor_research(
    service: str,
    city: str,
    state: str,
    company_id: Optional[int] = None,
    db=None,
    max_competitors: int = 5,
) -> CompetitorReport:
    """
    Full competitor research pipeline:
    1. Search Tavily for competitors
    2. Dedup by root domain, filter directories/chains
    3. Scrape top N with Firecrawl
    4. Analyze each with Claude Haiku
    5. Compute generation targets
    6. Optionally save to social_competitors table
    """
    report = CompetitorReport(service=service, city=city, state=state)

    # Step 1: Search
    raw_results = await search_competitors(service, city, state)

    # Step 2: Dedup by root domain
    seen_domains = set()
    unique = []
    for r in raw_results:
        url = r.get("url", "")
        if not url or not url.startswith("http"):
            continue
        if _is_excluded(url):
            continue
        root = _get_root_domain(url)
        if root not in seen_domains:
            seen_domains.add(root)
            unique.append(r)

    logger.info(f"Found {len(unique)} unique local candidates after filtering")

    # Steps 3-4: Scrape and analyze top N
    for r in unique[:max_competitors]:
        url = r.get("url", "")
        name = r.get("title", "").split("|")[0].split(" - ")[0].strip()
        if not name:
            name = _get_root_domain(url)

        markdown = await scrape_competitor(url)
        if not markdown or len(markdown) < 100:
            logger.warning(f"Skipping {url}: insufficient content ({len(markdown)} chars)")
            continue

        analysis = await analyze_competitor(name, url, markdown, service, city, state)
        report.competitors.append(analysis)

    # Step 5: Compute generation targets
    if report.competitors:
        word_counts = [c.word_count for c in report.competitors if c.word_count > 0]
        faq_counts = [c.faq_count for c in report.competitors]
        service_counts = [len(c.services_listed) for c in report.competitors]

        report.max_word_count = max(word_counts) if word_counts else 500
        report.max_faq_count = max(faq_counts) if faq_counts else 0
        report.max_services_count = max(service_counts) if service_counts else 0

        # Collect all gaps across competitors
        all_gaps = []
        for c in report.competitors:
            all_gaps.extend(c.gaps)
        # Find gaps that appear in 2+ competitors
        gap_freq = {}
        for g in all_gaps:
            g_lower = g.lower()
            gap_freq[g_lower] = gap_freq.get(g_lower, 0) + 1
        report.common_gaps = [g for g, count in gap_freq.items() if count >= 2]

        report.generation_targets = {
            "min_word_count": int(report.max_word_count * 1.3),
            "min_faq_count": report.max_faq_count + 3,
            "min_services_count": report.max_services_count + 2,
            "must_include": [
                "JSON-LD LocalBusiness schema",
                "JSON-LD FAQPage schema",
                "Transparent pricing ranges",
                "Neighborhood/area-specific content",
                "Customer review section",
                "Process/how-it-works section",
            ],
        }
    else:
        report.generation_targets = {
            "min_word_count": 1500,
            "min_faq_count": 8,
            "min_services_count": 8,
            "must_include": [
                "JSON-LD LocalBusiness schema",
                "JSON-LD FAQPage schema",
                "Transparent pricing ranges",
            ],
        }

    # Step 6: Save to social_competitors if DB provided
    if db and company_id:
        await _save_to_social_competitors(db, company_id, report)

    logger.info(
        f"Research complete: {len(report.competitors)} competitors analyzed. "
        f"Targets: {report.generation_targets}"
    )
    return report


async def _save_to_social_competitors(db, company_id: int, report: CompetitorReport):
    """Upsert competitor analysis into social_competitors table."""
    from sqlalchemy import text as sa_text

    for comp in report.competitors:
        try:
            # Check if exists
            existing = await db.execute(
                sa_text("""
                    SELECT id FROM social_competitors
                    WHERE company_id = :cid AND competitor_name = :name
                """),
                {"cid": company_id, "name": comp.name},
            )
            row = existing.fetchone()

            strengths = comp.trust_signals[:5] if comp.trust_signals else []
            weaknesses = comp.gaps[:5] if comp.gaps else []
            services = comp.services_listed[:10] if comp.services_listed else []
            usps = comp.unique_claims[:5] if comp.unique_claims else []

            pricing_intel = ""
            if comp.pricing_transparency != "none":
                pricing_intel = f"{comp.pricing_transparency}: {comp.pricing_details}"

            if row:
                await db.execute(
                    sa_text("""
                        UPDATE social_competitors SET
                            website = :url,
                            city = :city,
                            state = :state,
                            strengths = :strengths,
                            weaknesses = :weaknesses,
                            services = :services,
                            unique_selling_points = :usps,
                            pricing_intel = :pricing,
                            last_scanned = NOW()
                        WHERE id = :id
                    """),
                    {
                        "id": row[0],
                        "url": comp.url,
                        "city": report.city,
                        "state": report.state,
                        "strengths": strengths,
                        "weaknesses": weaknesses,
                        "services": services,
                        "usps": usps,
                        "pricing": pricing_intel,
                    },
                )
            else:
                await db.execute(
                    sa_text("""
                        INSERT INTO social_competitors
                            (company_id, competitor_name, website, city, state,
                             strengths, weaknesses, services,
                             unique_selling_points, pricing_intel,
                             last_scanned, created_at)
                        VALUES (:cid, :name, :url, :city, :state,
                                :strengths, :weaknesses, :services,
                                :usps, :pricing,
                                NOW(), NOW())
                    """),
                    {
                        "cid": company_id,
                        "name": comp.name,
                        "url": comp.url,
                        "city": report.city,
                        "state": report.state,
                        "strengths": strengths,
                        "weaknesses": weaknesses,
                        "services": services,
                        "usps": usps,
                        "pricing": pricing_intel,
                    },
                )
            await db.commit()
            logger.info(f"Saved competitor to social_competitors: {comp.name}")
        except Exception as e:
            logger.error(f"Failed to save competitor {comp.name}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass


def format_report_for_prompt(report: CompetitorReport) -> str:
    """Format a CompetitorReport into text for injection into a generation prompt."""
    lines = []
    lines.append("## COMPETITOR ANALYSIS — your content MUST exceed every metric:\n")

    for i, comp in enumerate(report.competitors, 1):
        lines.append(f"### {i}. {comp.name} ({comp.url})")
        lines.append(f"- Words: {comp.word_count} | FAQs: {comp.faq_count} | Services: {len(comp.services_listed)}")
        if comp.services_listed:
            lines.append(f"- Services offered: {', '.join(comp.services_listed[:8])}")
        if comp.trust_signals:
            lines.append(f"- Trust signals: {', '.join(comp.trust_signals[:5])}")
        lines.append(f"- Pricing: {comp.pricing_transparency} — {comp.pricing_details or 'not disclosed'}")
        if comp.unique_claims:
            lines.append(f"- Unique claims: {', '.join(comp.unique_claims[:4])}")
        if comp.gaps:
            lines.append(f"- GAPS to exploit: {', '.join(comp.gaps[:5])}")
        lines.append(f"- Schema: {'Yes (' + ', '.join(comp.schema_types) + ')' if comp.has_schema_markup else 'NONE — include this to beat them'}")
        lines.append("")

    targets = report.generation_targets
    lines.append("## MANDATORY GENERATION TARGETS:")
    lines.append(f"- Minimum word count: {targets.get('min_word_count', 1500)}")
    lines.append(f"- Minimum FAQ count: {targets.get('min_faq_count', 8)}")
    lines.append(f"- Minimum services listed: {targets.get('min_services_count', 8)}")
    if targets.get("must_include"):
        lines.append(f"- Must include: {', '.join(targets['must_include'])}")
    if report.common_gaps:
        lines.append(f"- Common competitor gaps to exploit: {', '.join(report.common_gaps[:6])}")

    return "\n".join(lines)
