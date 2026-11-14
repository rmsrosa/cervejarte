// Dropdown submenus: keep only one open, close on outside click or Escape.
(function () {
  function closeAll(except) {
    document.querySelectorAll("details.sub[open]").forEach(function (d) {
      if (d !== except) d.removeAttribute("open");
    });
  }
  document.addEventListener("click", function (e) {
    closeAll(e.target.closest && e.target.closest("details.sub"));
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll(null);
  });
})();
