(() => {
  'use strict';
  const input = document.querySelector('#help-search');
  const form = document.querySelector('.search-form');
  const reset = document.querySelector('.search-reset');
  const status = document.querySelector('.search-status');
  const empty = document.querySelector('.empty-state');
  const sections = [...document.querySelectorAll('.guide-section')];
  const items = [...document.querySelectorAll('.help-item')];
  const chapter = document.querySelector('#chapter');
  const normalize = text => text.toLowerCase().replace(/角色卡/g, '人物卡').replace(/\s+/g, ' ').trim();
  const index = items.map(element => ({element, text: normalize(element.closest('section').querySelector('h2').textContent + ' ' + element.textContent)}));
  let beforeSearch = null;
  function filter() {
    const query = normalize(input.value);
    if (query && !beforeSearch) beforeSearch = new Map(items.filter(e => e.tagName === 'DETAILS').map(e => [e, e.open]));
    const terms = query.split(' ').filter(Boolean);
    let count = 0;
    index.forEach(({element, text}) => {
      const matched = terms.every(term => text.includes(term));
      element.hidden = !matched;
      if (matched) count++;
      if (query && matched && element.tagName === 'DETAILS') element.open = true;
    });
    sections.forEach(section => section.hidden = ![...section.querySelectorAll('.help-item')].some(e => !e.hidden));
    if (!query && beforeSearch) {
      beforeSearch.forEach((open, element) => element.open = open);
      beforeSearch = null;
    }
    status.textContent = query ? `找到 ${count} 条相关说明` : '';
    empty.hidden = !query || count > 0;
    reset.hidden = !query;
    updateChapter();
  }
  function clearSearch(focus = false) { input.value = ''; filter(); if (focus) input.focus(); }
  input.addEventListener('input', filter);
  input.addEventListener('keydown', event => { if (event.key === 'Escape' && input.value) { event.preventDefault(); clearSearch(true); } });
  form.addEventListener('submit', event => { event.preventDefault(); filter(); });
  reset.addEventListener('click', () => clearSearch(true));
  document.querySelectorAll('[data-clear]').forEach(button => button.addEventListener('click', () => clearSearch(true)));
  document.querySelectorAll('[data-query]').forEach(button => button.addEventListener('click', () => { input.value = button.dataset.query; filter(); input.focus(); }));
  document.querySelectorAll('.sidebar nav a,[data-browse],.existing-user>a').forEach(link => link.addEventListener('click', () => clearSearch()));
  chapter.addEventListener('change', () => { clearSearch(); location.hash = chapter.value; });
  function revealHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    if (!id) return;
    const target = document.getElementById(id);
    if (!target) return;
    if (target.hidden || target.closest('[hidden]')) clearSearch();
    if (target.tagName === 'DETAILS') target.open = true;
    const section = target.closest('.guide-section');
    if (section) setCurrent(section.id);
    requestAnimationFrame(() => target.scrollIntoView({block: 'start'}));
  }
  function setCurrent(id) {
    document.querySelectorAll('.sidebar nav a').forEach(link => {
      if (link.hash === '#' + id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    chapter.value = '#' + id;
  }
  let scheduled = false;
  function updateChapter() {
    scheduled = false;
    let current = sections.find(section => !section.hidden);
    for (const section of sections) if (!section.hidden && section.getBoundingClientRect().top <= 150) current = section;
    if (current) setCurrent(current.id);
  }
  window.addEventListener('scroll', () => { if (!scheduled) { scheduled = true; requestAnimationFrame(updateChapter); } }, {passive: true});
  window.addEventListener('hashchange', revealHash);
  setCurrent('start');
  revealHash();
  const copyStatus = document.querySelector('#copy-status');
  document.querySelectorAll('[data-copy]').forEach(button => {
    const label = button.querySelector('span');
    const original = label.textContent;
    let timer;
    button.addEventListener('click', async () => {
      let copied = false;
      try {
        if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(button.dataset.copy); copied = true; }
      } catch { /* Use the local fallback when clipboard permission is unavailable. */ }
      if (!copied) {
        const buffer = document.createElement('textarea');
        buffer.className = 'clipboard-buffer'; buffer.value = button.dataset.copy; buffer.setAttribute('readonly', '');
        document.body.append(buffer); buffer.select();
        try { copied = document.execCommand('copy'); } catch { copied = false; }
        buffer.remove(); button.focus({preventScroll: true});
      }
      clearTimeout(timer);
      label.textContent = copied ? '已复制' : '请长按指令复制';
      copyStatus.textContent = copied ? `已复制 ${button.dataset.copy}，回到微信发送即可。` : '未能自动复制，请长按页面上的指令手动复制。';
      timer = setTimeout(() => { label.textContent = original; }, 3000);
    });
  });
})();
