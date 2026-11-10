// Cat cam: the live frame refreshes itself, and a flash shows which moment is playing.
// (Tapping a moment to jump there is lab.js's: any button.seek inside a .flash.)
(function () {
  // The camera now: a new picture every 30 s while the page is in view, swapped in only once it has loaded.
  const lives = [...document.querySelectorAll("img[data-live]")];
  const stamp = (img) => {
    const box = img.closest("[data-live-box]");
    const t = box && box.querySelector("[data-live-time]");
    if (t) t.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  };
  function refresh() {
    if (document.hidden) return;
    lives.forEach((img) => {
      const next = new Image();
      next.onload = () => { img.src = next.src; stamp(img); };
      next.src = img.dataset.live + "?t=" + Date.now();
    });
  }
  if (lives.length) {
    setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }

  // A flash playing: mark its current moment (chip or row) and keep the chip in view in its strip.
  document.querySelectorAll(".flash video").forEach((video) => {
    const box = video.closest(".flash");
    const marks = [...box.querySelectorAll("[data-seek]")].sort((a, b) => a.dataset.seek - b.dataset.seek);
    if (!marks.length) return;
    let current = null;
    const row = (m) => m.closest(".item") || m;      // a list row lights up whole, a chip by itself
    video.addEventListener("timeupdate", () => {
      let on = null;
      for (const m of marks) if (parseFloat(m.dataset.seek) <= video.currentTime + 0.05) on = m;
      if (on === current) return;
      if (current) row(current).classList.remove("on");
      current = on;
      if (!on) return;
      row(on).classList.add("on");
      const strip = on.closest(".strip");
      if (strip) strip.scrollTo({ left: on.offsetLeft - strip.clientWidth / 2 + on.clientWidth / 2, behavior: "smooth" });
    });
  });
})();
