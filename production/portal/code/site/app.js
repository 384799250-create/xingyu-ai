(() => {
  'use strict';
  if (location.hash === '#account') { location.replace('/ai/account#account'); return; }
  const hero = document.querySelector('.hero');
  const slides = [...document.querySelectorAll('.hero-slide')];
  const tabs = [...document.querySelectorAll('.slide-tab')];
  const rotation = document.querySelector('.rotation-toggle');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let active = 0;
  let stopped = reduced.matches;
  let timer;
  const pauses = new Set();
  function schedule() {
    clearTimeout(timer);
    hero.classList.toggle('is-paused', pauses.size > 0);
    hero.classList.toggle('is-stopped', stopped);
    const playing = !stopped && !pauses.size;
    rotation.setAttribute('aria-label', playing ? '暂停自动轮播' : '开启自动轮播');
    rotation.title = playing ? '暂停自动轮播' : '开启自动轮播';
    rotation.querySelector('use').setAttribute('href', playing ? '#i-pause' : '#i-play');
    const progress = tabs[active].querySelector('i').getAnimations()[0];
    if (progress) progress.currentTime = 0;
    if (playing) timer = setTimeout(() => select((active + 1) % slides.length), 5000);
  }
  function select(index, manual = false, focus = false) {
    if (manual) stopped = true;
    active = index;
    slides.forEach((slide, i) => {
      slide.hidden = i !== active;
      slide.inert = i !== active;
      slide.setAttribute('aria-hidden', String(i !== active));
      slide.classList.toggle('is-active', i === active);
      slide.classList.toggle('is-entering', i === active);
      tabs[i].classList.toggle('is-active', i === active);
      tabs[i].setAttribute('aria-selected', String(i === active));
      tabs[i].tabIndex = i === active ? 0 : -1;
    });
    if (focus) tabs[active].focus();
    schedule();
  }
  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(i));
    tab.addEventListener('keydown', event => {
      let next = i;
      if (event.key === 'ArrowRight') next = (i + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      select(next, true, true);
    });
  });
  rotation.addEventListener('click', () => {
    stopped = !stopped && !pauses.size;
    schedule();
  });
  hero.addEventListener('focusin', event => {
    if (!hero.contains(event.relatedTarget) && event.target.matches(':focus-visible')) { stopped = true; schedule(); }
  });
  document.addEventListener('visibilitychange', () => { document.hidden ? pauses.add('hidden') : pauses.delete('hidden'); schedule(); });
  reduced.addEventListener('change', event => { if (event.matches) stopped = true; schedule(); });
  const observer = new IntersectionObserver(entries => { entries[0].isIntersecting ? pauses.delete('offscreen') : pauses.add('offscreen'); schedule(); });
  observer.observe(hero);
  let touchStart;
  hero.addEventListener('touchstart', event => { const t = event.changedTouches[0]; touchStart = {x:t.clientX,y:t.clientY}; }, {passive:true});
  hero.addEventListener('touchend', event => {
    if (!touchStart || event.target.closest('a,button')) return;
    const t = event.changedTouches[0], dx=t.clientX-touchStart.x, dy=t.clientY-touchStart.y;
    if (Math.abs(dx)>60 && Math.abs(dx)>Math.abs(dy)*1.5) select((active+(dx<0?1:slides.length-1))%slides.length);
    touchStart=null;
  }, {passive:true});
  const menu = document.querySelector('.menu-toggle'), nav=document.querySelector('.mobile-nav');
  function closeMenu() { nav.hidden=true;menu.setAttribute('aria-expanded','false');menu.setAttribute('aria-label','打开导航'); }
  menu.addEventListener('click', () => { const open=menu.getAttribute('aria-expanded')==='true'; nav.hidden=open;menu.setAttribute('aria-expanded',String(!open));menu.setAttribute('aria-label',open?'打开导航':'关闭导航'); });
  nav.addEventListener('click',event=>{if(event.target.closest('a'))closeMenu();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!nav.hidden){closeMenu();menu.focus();}});
  document.addEventListener('click',event=>{if(!nav.hidden&&!nav.contains(event.target)&&!menu.contains(event.target))closeMenu();});
  window.matchMedia('(min-width:761px)').addEventListener('change',event=>{if(event.matches)closeMenu();});
  select(0);
})();
