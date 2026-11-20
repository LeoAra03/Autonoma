const visible = e => {
  if (!e) return false;
  const r = e.getBoundingClientRect();
  return e.offsetParent !== null && r.width > 0 && r.height > 0;
};

const label = e => ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')).toLowerCase();
const needles = (p.continueText || []).map(t => String(t).toLowerCase());

// Pick the lowest "continue" control on the page, so an agent card above it is not clicked.
const buttons = (p.continueCard ? [...document.querySelectorAll(p.continueCard)] : [])
  .filter(visible)
  .filter(b => needles.some(n => label(b).includes(n)))
  .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);

if (!buttons.length) return p.action === 'detect' ? false : null;

const target = buttons[0];
target.scrollIntoView({ block: 'center' });
if (p.action === 'detect') return true;
if (p.action === 'focus') { target.focus(); return true; }

const r = target.getBoundingClientRect();
return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
