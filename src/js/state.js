const visible = e => {
  if (!e) return false;
  const r = e.getBoundingClientRect();
  return e.offsetParent !== null && r.width > 0 && r.height > 0;
};

const query = s => {
  if (!s) return [];
  try { return [...document.querySelectorAll(s)].filter(visible); } catch { return []; }
};

const label = e => ((e.getAttribute('aria-label') || '') + ' ' + (e.title || '') + ' ' + (e.innerText || '')).toLowerCase();
const hit = (e, list) => list.some(n => n && label(e).includes(String(n).toLowerCase()));

const editors = query(p.editor);
const editor = editors.length ? editors[editors.length - 1] : null;
const body = document.body ? (document.body.innerText || '') : '';
const value = editor ? (editor.value !== undefined ? editor.value : editor.innerText) : '';

return {
  hasEditor: !!editor,
  editorDisabled: !!editor && (editor.disabled === true || editor.getAttribute('aria-disabled') === 'true'),
  editorText: (value || '').trim().slice(0, 200),
  busy: query(p.busyButton).some(b => hit(b, p.busyText)),
  card: query(p.continueCard).some(b => hit(b, p.continueText)),
  captcha: p.captcha.some(s => query(s).length > 0),
  bodyLength: body.length,
  tail: body.slice(-20000)
};
