/**
 * Clear the Clutter — AI Estimator Gate
 * Two-layer protection:
 *   1. Silent IP geolocation check (ipapi.co — free, no key needed)
 *   2. ZIP code confirmation from the user
 * Only Fox Valley / service-area ZIPs unlock the widget.
 */

(function () {
  'use strict';

  // ── Allowed ZIP codes (service area + surrounding Fox Valley) ──
  const ALLOWED_ZIPS = new Set([
    // Oshkosh
    '54901','54902','54903','54904',
    // Appleton
    '54911','54912','54913','54914','54915',
    // Neenah / Menasha / Fox Crossing
    '54956','54957','54952',
    // Fond du Lac
    '54935','54936','54937',
    // Berlin
    '54923',
    // Ripon
    '54971',
    // Omro
    '54963',
    // Winneconne
    '54986',
    // Green Lake
    '54941',
    // Waupaca / Wautoma
    '54981','54982',
    // Fox Valley surrounds
    '54130','54140','54113','54136','54942','54169','54944',
    // New London / Pickett / rural Oshkosh area
    '54961','54964','54966',
    // Oshkosh suburban
    '54220',
    // Fond du Lac county extras
    '54932','54979',
  ]);

  // ── Geo: Wisconsin state codes from ipapi ──
  const ALLOWED_STATE = 'WI';

  // ── In-memory flag so gate only runs once per page load ──
  let _allowed = false;

  // ── Find every widget wrapper on the page ──
  const wrappers = document.querySelectorAll('.ai-widget-wrap');
  if (!wrappers.length) return;

  // ── Inject gate styles ──
  const style = document.createElement('style');
  style.textContent = `
    .ctc-gate {
      background: #1a2b3c;
      border-radius: 16px;
      padding: 48px 40px;
      text-align: center;
      font-family: inherit;
    }
    .ctc-gate-icon {
      font-size: 2.5rem;
      margin-bottom: 16px;
    }
    .ctc-gate h3 {
      color: #fff;
      font-size: 1.35rem;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .ctc-gate p {
      color: #9ca3af;
      font-size: 0.95rem;
      margin-bottom: 28px;
      max-width: 380px;
      margin-left: auto;
      margin-right: auto;
    }
    .ctc-gate-row {
      display: flex;
      gap: 10px;
      justify-content: center;
      flex-wrap: wrap;
    }
    .ctc-gate input[type="text"] {
      background: #0f1923;
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 9999px;
      color: #fff;
      font-size: 1rem;
      font-weight: 600;
      padding: 13px 22px;
      width: 160px;
      text-align: center;
      letter-spacing: 0.08em;
      outline: none;
      transition: border-color 0.2s;
    }
    .ctc-gate input[type="text"]:focus {
      border-color: #e8400c;
    }
    .ctc-gate input[type="text"].error {
      border-color: #ef4444;
    }
    .ctc-gate-btn {
      background: #e8400c;
      color: #fff;
      font-weight: 700;
      font-size: 0.95rem;
      padding: 13px 28px;
      border: none;
      border-radius: 9999px;
      cursor: pointer;
      transition: background 0.2s, transform 0.15s;
    }
    .ctc-gate-btn:hover {
      background: #cc3609;
      transform: translateY(-1px);
    }
    .ctc-gate-error {
      color: #ef4444;
      font-size: 0.85rem;
      margin-top: 12px;
      min-height: 20px;
    }
    .ctc-gate-checking {
      color: #9ca3af;
      font-size: 0.9rem;
      margin-top: 8px;
    }
    .ctc-gate-outside {
      display: none;
    }
    .ctc-gate-outside.show {
      display: block;
    }
    @media (max-width: 480px) {
      .ctc-gate { padding: 36px 24px; }
      .ctc-gate input[type="text"] { width: 140px; }
    }
  `;
  document.head.appendChild(style);

  // ── Build the gate HTML ──
  function buildGate(wrapper) {
    wrapper.innerHTML = `
      <div class="ctc-gate" id="ctc-gate-box">
        <div class="ctc-gate-icon">📍</div>
        <h3>Enter Your ZIP Code to Continue</h3>
        <p>This tool is available exclusively to customers in our Fox Valley service area.</p>
        <div class="ctc-gate-row">
          <input type="text" id="ctc-zip-input" placeholder="e.g. 54901" maxlength="5" autocomplete="postal-code" inputmode="numeric">
          <button class="ctc-gate-btn" id="ctc-zip-btn">Check My Area</button>
        </div>
        <div class="ctc-gate-error" id="ctc-zip-error"></div>
        <div class="ctc-gate-outside show" id="ctc-out-of-area" style="display:none; margin-top:20px;">
          <p style="margin-bottom:12px; color:#9ca3af;">We don't currently serve that area.<br>Give us a call — we'd love to help.</p>
          <a href="tel:9204249827" style="color:#e8400c; font-weight:700; font-size:1rem;">📞 920-424-9827</a>
        </div>
      </div>
    `;

    const input = document.getElementById('ctc-zip-input');
    const btn   = document.getElementById('ctc-zip-btn');
    const err   = document.getElementById('ctc-zip-error');
    const ooa   = document.getElementById('ctc-out-of-area');

    // Only allow digits
    input.addEventListener('input', () => {
      input.value = input.value.replace(/\D/g, '');
      input.classList.remove('error');
      err.textContent = '';
      ooa.style.display = 'none';
    });

    // Submit on Enter
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') btn.click();
    });

    btn.addEventListener('click', () => {
      const zip = input.value.trim();
      if (zip.length !== 5) {
        input.classList.add('error');
        err.textContent = 'Please enter a valid 5-digit ZIP code.';
        return;
      }
      if (ALLOWED_ZIPS.has(zip)) {
        _allowed = true;
        unlockWidget(wrapper);
      } else {
        input.classList.add('error');
        err.textContent = 'That ZIP isn\'t in our service area.';
        ooa.style.display = 'block';
      }
    });
  }

  // ── Load the real widget ──
  function unlockWidget(wrapper) {
    wrapper.innerHTML = '';
    const script = document.createElement('script');
    script.src = 'https://whatshouldicharge.app/static/widget.js';
    script.setAttribute('data-slug', 'clear-the-clutter');
    wrapper.appendChild(script);
  }

  // ── Main flow ──
  function init(wrapper) {
    // Already verified this page load
    if (_allowed) {
      unlockWidget(wrapper);
      return;
    }

    // Show gate immediately while geo check runs in background
    buildGate(wrapper);

    // Silent geo check — if they're clearly outside WI, we just let the gate
    // stand. If inside WI, we could auto-pass but we still want the ZIP
    // confirmation so we keep the gate visible regardless.
    // (The geo check is advisory — we log but don't auto-block to avoid
    //  false positives on mobile data / VPN users who ARE local.)
    fetch('https://ipapi.co/json/', { signal: AbortSignal.timeout(4000) })
      .then(r => r.json())
      .then(data => {
        // If clearly not WI and not a known VPN/datacenter, show a softer message
        if (data.region_code && data.region_code !== ALLOWED_STATE && !data.org?.includes('VPN')) {
          const gate = document.getElementById('ctc-gate-box');
          if (gate) {
            const p = gate.querySelector('p');
            if (p) p.textContent = 'This tool is for customers in Oshkosh, Appleton, Neenah & the Fox Valley. Enter your ZIP to confirm.';
          }
        }
      })
      .catch(() => {
        // Geo check failed silently — gate still works via ZIP
      });
  }

  // Run on each wrapper found
  wrappers.forEach(init);

})();
