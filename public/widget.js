// Mount outside the host site's layout. The customer enters their details on the support origin.
(() => {
  const source = document.currentScript?.src;
  if (!source) return;
  const mount = () => {
    if (document.querySelector('[data-pixelgrade-contact]')) return;
    const host = document.createElement('div');
    host.dataset.pixelgradeContact = '';
    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = new URL('/contact/widget.css', source).href;
    const link = document.createElement('a');
    link.textContent = 'Contact support';
    link.href = new URL('/contact', source).href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.setAttribute('aria-label', 'Contact support (opens in a new tab)');
    shadow.append(style, link);
    document.body.append(host);
  };
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount, { once: true });
})();
