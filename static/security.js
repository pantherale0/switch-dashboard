(() => {
  window.escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
  window.escapeJsString = (value) => String(value ?? '').replace(/[\\'"\n\r\u2028\u2029<>&]/g, (character) => ({
    '\\': '\\\\',
    "'": "\\'",
    '"': '\\x22',
    '\n': '\\n',
    '\r': '\\r',
    '\u2028': '\\u2028',
    '\u2029': '\\u2029',
    '<': '\\x3C',
    '>': '\\x3E',
    '&': '\\x26'
  })[character]);
  window.safeCssToken = (value) => String(value ?? '').replace(/[^a-zA-Z0-9_-]/g, '');
  const meta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = meta ? meta.content : '';
  const originalFetch = window.fetch.bind(window);

  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? new URL(input, window.location.href) : new URL(input.url);
    const method = (init.method || (typeof input !== 'string' && input.method) || 'GET').toUpperCase();
    if (url.origin === window.location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(init.headers || (typeof input !== 'string' ? input.headers : undefined));
      headers.set('X-CSRF-Token', csrfToken);
      init = { ...init, headers };
    }
    return originalFetch(input, init);
  };

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
      if (!form.querySelector('input[name="csrf_token"]')) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = csrfToken;
        form.appendChild(input);
      }
    });
  });
})();
