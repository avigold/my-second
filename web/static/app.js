/**
 * mysecond web UI – shared client-side logic.
 *
 * Handles form submission for both the fetch and search pages:
 *   1. Intercepts the submit event.
 *   2. Validates usernames against Lichess / Chess.com before submitting.
 *   3. POSTs form data as JSON to the form's data-api endpoint.
 *   4. Redirects to the job page on success.
 *   5. Shows an inline error message on failure.
 *
 * Username validation:
 *   Add  data-validate-user="usernameField:platformField"  to a form to
 *   validate one username before submit.  Separate multiple pairs with "|":
 *     data-validate-user="player:player_platform|opponent:opponent_platform"
 *   Pairs where the username field is empty are skipped (optional fields).
 */

/**
 * Check a username against Lichess or Chess.com.
 * Returns { valid, username } on success or { valid: false, error } on failure.
 */
async function validateUsername(username, platform) {
  const res = await fetch(
    `/api/validate-user?username=${encodeURIComponent(username)}&platform=${encodeURIComponent(platform)}`
  );
  return res.json();
}

// ---------------------------------------------------------------------------
// Usage meters — injected before the submit button on gated form pages.
// ---------------------------------------------------------------------------

(async function renderUsageMeters() {
  const forms = document.querySelectorAll('form[data-command]');
  if (!forms.length) return;

  let data;
  try {
    const res = await fetch('/api/usage');
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }

  const { plan, usage } = data;
  if (plan === 'pro') return;  // pro users see no meter

  forms.forEach(form => {
    const cmd = form.dataset.command;
    const info = usage[cmd];
    if (!info || info.limit === null) return;

    const { used, limit } = info;
    const pct = Math.min(100, Math.round((used / limit) * 100));
    const atLimit = used >= limit;
    const nearLimit = pct >= 67;

    const label = limit === 1
      ? (atLimit ? 'Monthly limit reached (1/1)' : `${used} of 1 used this month`)
      : (atLimit ? `Monthly limit reached (${used}/${limit})` : `${used} of ${limit} used this month`);

    const noun = { search: 'novelty searches', habits: 'habits analyses',
                   repertoire: 'repertoire extractions', strategise: 'strategy briefs',
                   'train-bot': 'bots trained' }[cmd] || 'analyses';

    let html;
    if (atLimit) {
      html = `
        <div id="usage-meter" class="mb-4 flex items-center justify-between
             bg-red-950/60 border border-red-800/60 rounded-lg px-3 py-2 text-xs">
          <span class="text-red-400 font-medium">${label}</span>
          <a href="/pricing" class="text-amber-400 hover:text-amber-300 font-semibold ml-4 shrink-0">Upgrade to Pro →</a>
        </div>`;
    } else {
      const barColor = nearLimit ? 'bg-amber-400' : 'bg-amber-500';
      const textColor = nearLimit ? 'text-amber-300' : 'text-gray-400';
      html = `
        <div id="usage-meter" class="mb-4">
          <div class="flex items-center justify-between text-xs mb-1.5">
            <span class="${textColor}">${label} · ${noun}</span>
            <a href="/pricing" class="text-amber-400 hover:text-amber-300 font-medium ml-4 shrink-0">Upgrade →</a>
          </div>
          <div class="h-1 bg-gray-800 rounded-full overflow-hidden">
            <div class="${barColor} h-1 rounded-full" style="width:${pct}%"></div>
          </div>
        </div>`;
    }

    const btn = form.querySelector('#submit-btn');
    if (btn) btn.insertAdjacentHTML('beforebegin', html);
  });
})();

document.querySelectorAll('form[data-api]').forEach(form => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const endpoint  = form.dataset.api;
    const submitBtn = form.querySelector('#submit-btn');
    const errorEl   = form.querySelector('#error-msg');

    // Collect all named inputs.
    const raw = Object.fromEntries(new FormData(form));

    // Strip empty strings so the server sees clean params.
    const params = Object.fromEntries(
      Object.entries(raw).filter(([, v]) => v !== '')
    );

    submitBtn.disabled = true;
    submitBtn.textContent = 'Checking…';
    if (errorEl) errorEl.classList.add('hidden');

    // Validate usernames if the form declares which fields to check.
    const validateSpec = form.dataset.validateUser;
    if (validateSpec) {
      try {
        for (const pair of validateSpec.split('|')) {
          const [userField, platformField] = pair.split(':');
          const username = (params[userField] || '').trim();
          if (!username) continue;  // optional field — skip
          const platform = params[platformField] || 'lichess';
          const result = await validateUsername(username, platform);
          if (!result.valid) {
            throw new Error(result.error);
          }
          // Use the canonical casing returned by the platform API.
          params[userField] = result.username;
        }
      } catch (err) {
        if (errorEl) {
          errorEl.textContent = err.message;
          errorEl.classList.remove('hidden');
        }
        submitBtn.disabled = false;
        submitBtn.textContent = submitBtn.dataset.label || 'Submit';
        return;
      }
    }

    submitBtn.textContent = 'Starting…';

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });

      const data = await res.json();

      if (!res.ok) {
        const err = new Error(data.error || `Server error ${res.status}`);
        err.upgradeUrl = data.upgrade_url || null;
        throw err;
      }

      window.location.href = `/jobs/${data.job_id}`;
    } catch (err) {
      if (errorEl) {
        if (err.upgradeUrl) {
          errorEl.innerHTML =
            `${err.message} <a href="${err.upgradeUrl}" style="color:#f59e0b;text-decoration:underline;">Upgrade →</a>`;
        } else {
          errorEl.textContent = err.message;
        }
        errorEl.classList.remove('hidden');
      }
      submitBtn.disabled = false;
      submitBtn.textContent = submitBtn.dataset.label || 'Submit';
    }
  });

  // Remember original button label for error recovery.
  const btn = form.querySelector('#submit-btn');
  if (btn) btn.dataset.label = btn.textContent;
});
