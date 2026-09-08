(() => {
  const form = document.getElementById('download-form');
  const button = form.querySelector('button[type=submit]');
  const status = document.getElementById('form-status');
  let pending = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending || !form.reportValidity()) return;
    pending = true;
    button.disabled = true;
    button.textContent = 'Preparing your download…';
    status.dataset.error = 'false';
    status.textContent = '';
    try {
      const response = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not save your details. Please try again.');
      status.textContent = 'Your details are saved. Your download is starting.';
      const link = document.getElementById('download-link');
      link.href = result.download_url;
      document.getElementById('download-retry').hidden = false;
      link.click();
      button.textContent = 'Download ready';
    } catch (error) {
      status.dataset.error = 'true';
      status.textContent = error instanceof TypeError
        ? 'Could not connect. Check your connection and try again.'
        : error.message;
      button.disabled = false;
      button.textContent = 'Download free for macOS';
    } finally { pending = false; }
  });
})();
