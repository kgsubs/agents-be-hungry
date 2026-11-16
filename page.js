/* Shared page navigation. No report data, fixed section count or page-specific IDs. */
(() => {
  'use strict';
  const sidebar = document.querySelector('.sidebar');
  const menu = document.querySelector('.menu-toggle');
  const locationLabel = document.querySelector('.mobile-location');
  const links = [...document.querySelectorAll('.journey-link')];
  const sections = links.map(link => document.getElementById(link.hash.slice(1)));
  if (!sidebar || !menu || !links.length || sections.some(section => !section)) return;
  const icon = name => '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  function closeMenu() {
    sidebar.classList.remove('open');
    menu.setAttribute('aria-expanded', 'false');
    menu.innerHTML = icon('menu') + '<span>Sections</span>';
  }
  menu.addEventListener('click', () => {
    const open = sidebar.classList.toggle('open');
    menu.setAttribute('aria-expanded', String(open));
    menu.innerHTML = icon(open ? 'close' : 'menu') + '<span>' + (open ? 'Close' : 'Sections') + '</span>';
  });
  links.forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && sidebar.classList.contains('open')) { closeMenu(); menu.focus(); }
  });
  function updateJourney() {
    let current = 0;
    const threshold = window.innerWidth <= 900 ? 150 : 80;
    sections.forEach((section, index) => { if (section.getBoundingClientRect().top <= threshold) current = index; });
    if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 8) current = sections.length - 1;
    links.forEach((link, index) => {
      link.classList.toggle('active', index === current);
      if (index === current) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current');
    });
    if (locationLabel) locationLabel.textContent = links[current].querySelector('.nav-label').textContent;
  }
  let queued = false;
  window.addEventListener('scroll', () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { updateJourney(); queued = false; });
  }, { passive: true });
  window.addEventListener('resize', () => { if (window.innerWidth > 900) closeMenu(); updateJourney(); });
  document.querySelectorAll('details').forEach(detail => detail.addEventListener('toggle', updateJourney));
  updateJourney();
})();
