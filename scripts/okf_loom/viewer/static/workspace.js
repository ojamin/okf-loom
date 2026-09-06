/* Progressive workspace behavior. Native links and reading work without JS. */
(function () {
  "use strict";
  const nav = document.querySelector(".okf-workspace-nav");
  if (nav) {
    const current = location.pathname.replace(/index\.html$/, "/");
    nav.querySelectorAll("a").forEach((link) => {
      const path = new URL(link.href).pathname.replace(/index\.html$/, "/");
      if (path === current) link.setAttribute("aria-current", "page");
    });
  }

  const filter = document.getElementById("okf-catalog-filter");
  const status = document.getElementById("okf-catalog-status");
  const clear = document.getElementById("okf-catalog-clear");
  if (filter && status) {
    const groups = Array.from(document.querySelectorAll(".okf-index .okf-section"));
    const cards = groups.flatMap((group) => Array.from(group.querySelectorAll(".okf-card")));
    const entries = cards.map((node) => ({ node, text: node.textContent.toLocaleLowerCase() }));
    const update = () => {
      const terms = filter.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
      let count = 0;
      entries.forEach(({ node, text }) => {
        node.hidden = !terms.every((term) => text.includes(term));
        if (!node.hidden) count += 1;
      });
      groups.forEach((group) => {
        group.hidden = !Array.from(group.querySelectorAll(".okf-card")).some((node) => !node.hidden);
      });
      status.textContent = count ? `${count} of ${cards.length} concepts` : "No concepts match. Try fewer words or clear the filter.";
      clear.hidden = !filter.value;
    };
    filter.addEventListener("input", update);
    clear.addEventListener("click", () => { filter.value = ""; update(); filter.focus(); });
    filter.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { filter.value = ""; update(); }
    });
    update();
    document.getElementById("okf-catalog-tools").hidden = false;
  }

  // Static exports are successful read-only products, not failed live sessions.
  if (document.body.dataset.okfMode === "static") {
    document.querySelectorAll(".okf-studio-fallback-banner").forEach((node) => { node.hidden = true; });
  }
}());
