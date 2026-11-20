const visible = e => e && e.offsetParent !== null;

if (p.action === 'click') {
  const target = [...document.querySelectorAll(p.editor)].filter(visible).find(b => !b.disabled);
  if (!target) return false;
  target.click();
  return true;
}

const editors = p.editor ? [...document.querySelectorAll(p.editor)].filter(visible) : [];
const editor = editors.length ? editors[editors.length - 1] : null;
if (!editor) return false;

if (p.action === 'empty') {
  const value = editor.value !== undefined ? editor.value : editor.innerText;
  return ((value || '').trim()).length === 0;
}

editor.focus();

if (p.action === 'clear') {
  if (editor.tagName === 'TEXTAREA') { editor.select(); document.execCommand('delete'); return true; }
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editor);
  selection.removeAllRanges();
  selection.addRange(range);
  document.execCommand('delete');
  return true;
}

const selection = window.getSelection();
const range = document.createRange();
range.selectNodeContents(editor);
selection.removeAllRanges();
selection.addRange(range);
return true;
