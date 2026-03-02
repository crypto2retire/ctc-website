"""
Fix pricing inconsistencies and freon/recycling fee language across CTC site.
"""
import re
import os
import glob

SITE_DIR = "/var/www/ctc-main"

def fix_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    original = content
    filename = os.path.basename(filepath)

    # ── Fix 1: Wrong half-truck price ($200 → $250) ──
    # Only in pricing.html FAQ about price matching
    content = content.replace(
        "$100 minimum, $200 half-truck, $350 full truck",
        "$100 minimum, $250 half-truck, $500 full truck"
    )

    # ── Fix 2: "$100 to $100 range" → "$100 to $150 range" ──
    content = content.replace(
        "$100 to $100 range",
        "$100 to $150 range"
    )

    # ── Fix 3: Freon "no charge" → add proper surcharge ──

    # pricing.html specific: full freon section rewrite
    content = content.replace(
        "<strong>Refrigerators, freezers, AC units:</strong> Freon/refrigerant recovery included at no charge.",
        "<strong>Refrigerators, freezers, air conditioners, dehumidifiers:</strong> Freon/refrigerant recovery surcharge of $25 to $50 per unit."
    )

    # "Refrigerant recovery for appliances like refrigerators and air conditioners is included at no extra charge"
    content = content.replace(
        "Refrigerant recovery for appliances like refrigerators and air conditioners is included at no extra charge",
        "Refrigerant recovery for appliances like refrigerators, freezers, air conditioners, and dehumidifiers carries a surcharge of $25 to $50 per unit"
    )

    # "Freon-containing appliances are properly recycled at no extra charge"
    content = content.replace(
        "Freon-containing appliances are properly recycled at no extra charge",
        "Freon-containing appliances are properly recycled with a $25 to $50 refrigerant recovery fee per unit"
    )

    # "refrigerant recovery is included in our standard volume-based pricing — there is no additional surcharge for refrigerant-containing appliances"
    content = content.replace(
        "refrigerant recovery is included in our standard volume-based pricing \u2014 there is no additional surcharge for refrigerant-containing appliances",
        "refrigerant recovery carries a small surcharge of $25 to $50 per unit for refrigerant-containing appliances including refrigerators, freezers, air conditioners, and dehumidifiers"
    )
    # Also handle en-dash variant
    content = content.replace(
        "refrigerant recovery is included in our standard volume-based pricing &mdash; there is no additional surcharge for refrigerant-containing appliances",
        "refrigerant recovery carries a small surcharge of $25 to $50 per unit for refrigerant-containing appliances including refrigerators, freezers, air conditioners, and dehumidifiers"
    )

    # "refrigerant-containing appliances and CRT televisions, though the surcharge for those items is typically built into our standard pricing"
    content = content.replace(
        "though the surcharge for those items is typically built into our standard pricing",
        "with a surcharge of $25 to $50 per freon unit and $10 to $25 per CRT television"
    )

    # Any remaining "included at no charge" for freon/refrigerant
    content = re.sub(
        r'(?:Freon|refrigerant|Refrigerant)\s+(?:recovery\s+)?included at no charge',
        'refrigerant recovery fee of $25 to $50',
        content
    )

    # "at no extra charge" near freon/refrigerant context
    # Be careful not to touch stair carry "no extra charge" statements
    # Only replace when near freon/refrigerant words
    def fix_no_charge_freon(match):
        ctx = match.group(0)
        if any(kw in ctx.lower() for kw in ['freon', 'refrigerant', 'refrigerator', 'freezer', 'ac unit', 'air condition', 'dehumidifier']):
            return ctx.replace('at no extra charge', 'with a $25 to $50 refrigerant recovery surcharge').replace('at no charge', 'with a $25 to $50 refrigerant recovery surcharge').replace('no additional surcharge', 'a $25 to $50 refrigerant recovery surcharge')
        return ctx

    # Apply to sentences containing both freon keywords and "no charge"
    content = re.sub(
        r'[^.]*(?:freon|refrigerant|Freon|Refrigerant)[^.]*(?:no (?:extra |additional )?(?:charge|surcharge))[^.]*\.',
        fix_no_charge_freon,
        content
    )

    # ── Fix 4: Add missing CRT TV fee to pricing.html ──
    if filename == "pricing.html":
        # Add CRT TV fee line after the freon line if not already present
        if "CRT" not in content and "crt" not in content.lower():
            content = content.replace(
                "Freon/refrigerant recovery surcharge of $25 to $50 per unit.",
                "Freon/refrigerant recovery surcharge of $25 to $50 per unit.<br><br><strong>CRT TVs and monitors:</strong> E-waste recycling fee of $10 to $25 per unit."
            )

    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        changes = sum(1 for a, b in zip(original, content) if a != b)
        print(f"  {filename}: FIXED ({changes} chars changed)")
    else:
        print(f"  {filename}: no changes needed")


def main():
    html_files = sorted(glob.glob(os.path.join(SITE_DIR, "*.html")))
    for filepath in html_files:
        fix_file(filepath)

    # Verify
    print("\n--- Verification ---")
    import subprocess

    # Check for remaining "no charge" near freon
    r1 = subprocess.run(['grep', '-rl', 'included at no charge', SITE_DIR], capture_output=True, text=True)
    if r1.stdout.strip():
        for f in r1.stdout.strip().split('\n'):
            if f.endswith('.html'):
                print(f"  REMAINING 'included at no charge': {os.path.basename(f)}")
    else:
        print("  No 'included at no charge' remaining")

    r2 = subprocess.run(['grep', '-rl', 'at no extra charge', SITE_DIR], capture_output=True, text=True)
    if r2.stdout.strip():
        for f in r2.stdout.strip().split('\n'):
            if f.endswith('.html'):
                # Check if it's freon-related or stair-related
                with open(f) as fh:
                    lines = [l for l in fh if 'at no extra charge' in l]
                for l in lines:
                    if any(kw in l.lower() for kw in ['freon', 'refrigerant', 'refrigerator', 'freezer']):
                        print(f"  REMAINING freon 'no extra charge': {os.path.basename(f)}")
                    # stair carries are OK

    r3 = subprocess.run(['grep', '-c', '200 half', *glob.glob(os.path.join(SITE_DIR, '*.html'))], capture_output=True, text=True)
    wrong_half = [l for l in r3.stdout.strip().split('\n') if not l.endswith(':0')]
    if wrong_half:
        for w in wrong_half:
            print(f"  REMAINING $200 half-truck: {w}")
    else:
        print("  No '$200 half-truck' remaining")

    r4 = subprocess.run(['grep', '-c', '350 full', *glob.glob(os.path.join(SITE_DIR, '*.html'))], capture_output=True, text=True)
    wrong_full = [l for l in r4.stdout.strip().split('\n') if not l.endswith(':0')]
    if wrong_full:
        for w in wrong_full:
            print(f"  REMAINING $350 full-truck: {w}")
    else:
        print("  No '$350 full-truck' remaining")

    r5 = subprocess.run(['grep', '-c', '100 to .100', *glob.glob(os.path.join(SITE_DIR, '*.html'))], capture_output=True, text=True)
    wrong_range = [l for l in r5.stdout.strip().split('\n') if not l.endswith(':0')]
    if wrong_range:
        for w in wrong_range:
            print(f"  REMAINING $100-to-$100: {w}")
    else:
        print("  No '$100 to $100' remaining")


if __name__ == "__main__":
    main()
