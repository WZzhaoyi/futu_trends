// 共享主题切换（auto/dark/light 三态）。
// 需要页面里存在 #theme-btn；主题变化后会调用 window.onThemeChange（若已定义）。
const THEME_KEY = 'detect-theme';
const themeBtn = document.getElementById('theme-btn');
let themePref = localStorage.getItem(THEME_KEY) || 'auto';

function resolvedTheme() {
  const sys = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  return themePref === 'auto' ? sys : themePref;
}

function applyTheme() {
  document.documentElement.setAttribute('data-theme', resolvedTheme());
  const icons = { auto: '◑', light: '☀', dark: '☾' };
  themeBtn.textContent = icons[themePref];
  themeBtn.title = `Theme: ${themePref}`;
  if (typeof window.onThemeChange === 'function') window.onThemeChange();
}

function cycleTheme() {
  const order = ['auto', 'dark', 'light'];
  themePref = order[(order.indexOf(themePref) + 1) % 3];
  localStorage.setItem(THEME_KEY, themePref);
  applyTheme();
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (themePref === 'auto') applyTheme();
});

applyTheme();
