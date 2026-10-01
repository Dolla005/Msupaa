document.querySelectorAll("[data-confirm]").forEach((el) => {
  el.addEventListener("click", (event) => {
    if (!window.confirm(el.dataset.confirm)) event.preventDefault();
  });
});
setTimeout(() => {
  document.querySelectorAll(".flash").forEach((el) => el.classList.add("fade"));
}, 4500);


function setupCinematicHero(){
  const hero = document.querySelector('.hero-copy');
  const lines = Array.from(document.querySelectorAll('.cinematic-title .type-line'));
  if (!hero || !lines.length) return;

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) {
    lines.forEach((line) => {
      line.textContent = line.dataset.typeText || '';
      line.classList.add('ready');
    });
    hero.classList.add('cinematic-done');
    return;
  }

  lines.forEach((line) => { line.textContent = ''; });

  const typeLine = (index) => {
    const line = lines[index];
    if (!line) {
      hero.classList.add('cinematic-done');
      return;
    }
    const text = line.dataset.typeText || '';
    let i = 0;
    line.classList.add('ready', 'typing');

    const tick = () => {
      line.textContent = text.slice(0, i + 1);
      i += 1;
      if (i < text.length) {
        setTimeout(tick, index === 0 ? 38 : 34);
      } else {
        line.classList.remove('typing');
        line.classList.add('typed');
        setTimeout(() => {
          line.classList.remove('typed');
          typeLine(index + 1);
        }, 260);
      }
    };

    setTimeout(tick, index === 0 ? 260 : 120);
  };

  typeLine(0);
}

document.addEventListener('DOMContentLoaded', setupCinematicHero);
