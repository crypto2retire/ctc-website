"""
Fix VERIFY replacement artifacts: doubled text, bad grammar, remaining freon issues.
"""
import glob
import os
import re

SITE_DIR = "/var/www/ctc-main"


REPLACEMENTS = [
    # 1. "we offer For commercial accounts..." → clean sentence
    (
        "we offer For commercial accounts, we can arrange flexible payment terms by prior arrangement. for established clients",
        "we can arrange flexible payment terms for established commercial clients"
    ),
    (
        "we offer For commercial accounts, we can arrange flexible payment terms by prior arrangement.",
        "we can arrange flexible payment terms for established commercial clients."
    ),

    # 2. "documentation A certificate..." → clean sentence
    (
        "we can provide documentation A certificate of insurance is available upon request for property managers and commercial clients.",
        "we can provide a certificate of insurance upon request."
    ),
    (
        "documentation A certificate of insurance is available upon request for property managers and commercial clients.",
        "a certificate of insurance, available upon request."
    ),

    # 3. "insurance We carry..." → clean sentence
    (
        "carries insurance We carry full general liability insurance that covers",
        "carries full general liability insurance that covers"
    ),
    (
        "insurance We carry full general liability insurance",
        "full general liability insurance"
    ),

    # 4. "offers on-site estimates are always free" → grammar fix
    (
        "offers on-site estimates are always free where",
        "offers free on-site estimates where"
    ),
    (
        "provides on-site estimates are always free for",
        "provides free on-site estimates for"
    ),
    (
        "on-site estimates are always free where he visits",
        "free on-site estimates where he visits"
    ),
    (
        "on-site estimates are always free for all estate",
        "free on-site estimates for all estate"
    ),
    # Keep standalone "are always free" as OK — "All estimates are always free" is fine

    # 5. services.html: appliance "no additional surcharge" for freon items
    (
        "Standard appliance removal is included in our regular volume-based pricing with no additional surcharge. A single appliance",
        "Standard appliance removal is priced by volume. Appliances containing refrigerant (refrigerators, freezers, air conditioners, dehumidifiers) carry a $25 to $50 refrigerant recovery surcharge per unit. A single appliance"
    ),

    # 6. Also fix any "with no additional surcharge" near refrigerant in JSON-LD
    (
        "in compliance with EPA regulations. Standard appliance removal is included in our regular volume-based pricing with no additional surcharge.",
        "in compliance with EPA regulations. Appliances containing refrigerant carry a $25 to $50 recovery surcharge per unit."
    ),
]


def main():
    html_files = sorted(glob.glob(os.path.join(SITE_DIR, "*.html")))

    for filepath in html_files:
        filename = os.path.basename(filepath)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        original = content
        changes = []

        for old, new in REPLACEMENTS:
            if old in content:
                content = content.replace(old, new)
                changes.append(old[:60] + "...")

        if content != original:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  {filename}: FIXED {len(changes)} artifact(s)")
            for c in changes:
                print(f"    - {c}")
        else:
            print(f"  {filename}: clean")

    # Final verification
    print("\n--- Final checks ---")
    import subprocess

    checks = [
        ("'we offer For'", "we offer For"),
        ("'documentation A'", "documentation A"),
        ("'insurance We'", "insurance We"),
        ("'no additional surcharge' (freon)", "no additional surcharge"),
    ]
    for label, pattern in checks:
        r = subprocess.run(['grep', '-rl', pattern] + glob.glob(os.path.join(SITE_DIR, '*.html')),
                          capture_output=True, text=True)
        if r.stdout.strip():
            files = [os.path.basename(f) for f in r.stdout.strip().split('\n')]
            print(f"  {label}: REMAINING in {', '.join(files)}")
        else:
            print(f"  {label}: CLEAN")


if __name__ == "__main__":
    main()
