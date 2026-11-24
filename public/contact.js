const form = document.querySelector('form');
if (form) {
  const button = form.querySelector('button');
  const problem = document.querySelector('#problem');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    problem.hidden = true;
    const files = Array.from(form.elements.attachment.files);
    if (files.length > 4 || files.some(file => file.size > 8 * 1024 * 1024)
        || files.reduce((sum, file) => sum + file.size, 0) > 12 * 1024 * 1024) {
      problem.textContent = 'Choose up to 4 files, 8 MB each and 12 MB total.';
      problem.hidden = false;
      form.elements.attachment.focus();
      return;
    }
    button.disabled = true;
    button.textContent = 'Sending…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' }
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Please try again.');
      const title = document.querySelector('h1');
      title.textContent = 'Message received';
      title.tabIndex = -1;
      const receipt = document.createElement('p');
      receipt.textContent = 'Your support request is #' + data.number + '. We’ll reply by email.';
      form.replaceWith(receipt);
      title.focus();
    } catch (error) {
      problem.textContent = error.message || 'Please try again.';
      problem.hidden = false;
      button.disabled = false;
      button.textContent = 'Send message';
    }
  });
}
