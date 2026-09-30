document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.header .nav-links').forEach((nav, index) => {
    const header = nav.closest('.header');
    if (!header) return;

    const button = document.createElement('button');
    const navId = nav.id || `mobile-navigation-${index + 1}`;
    nav.id = navId;
    button.type = 'button';
    button.className = 'mobile-nav-toggle';
    button.setAttribute('aria-controls', navId);
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-label', 'Open navigation');
    button.innerHTML = '<span></span><span></span><span></span>';
    button.addEventListener('click', () => {
      const isOpen = nav.classList.toggle('mobile-open');
      button.setAttribute('aria-expanded', String(isOpen));
      button.setAttribute('aria-label', isOpen ? 'Close navigation' : 'Open navigation');
    });

    nav.addEventListener('click', event => {
      if (event.target.closest('a, button')) {
        nav.classList.remove('mobile-open');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-label', 'Open navigation');
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && nav.classList.contains('mobile-open')) {
        nav.classList.remove('mobile-open');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-label', 'Open navigation');
        button.focus();
      }
    });

    header.insertBefore(button, nav);
  });
});
