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
