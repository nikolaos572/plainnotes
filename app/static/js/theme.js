// Classic script loaded in <head> without defer, so the theme is set before the first paint.
(() => {
  const KEY = "plainnotes.theme";
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function setting() {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "auto";
    } catch {
      return "auto";
    }
  }

  function apply() {
    const theme = setting();
    const dark = theme === "dark" || (theme === "auto" && darkQuery.matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }

  apply();
  // Auto mode follows the operating system live; main.js fires the event after a change.
  darkQuery.addEventListener("change", apply);
  document.addEventListener("plainnotes:theme", apply);
})();
