/**
 * mysecond web UI – shared client-side logic.
 *
 * Handles form submission for both the fetch and search pages:
 *   1. Intercepts the submit event.
 *   2. POSTs form data as JSON to the form's data-api endpoint.
 *   3. Redirects to the job page on success.
 *   4. Shows an inline error message on failure.
 */

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
    submitBtn.textContent = 'Starting…';
    if (errorEl) errorEl.classList.add('hidden');

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
          errorEl.textContent = `Error: ${err.message}`;
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
