/** TGDrive uses one shared dark appearance; legacy per-browser choices are ignored. */
export function initializeTheme() {
  document.documentElement.dataset.theme = "dark";
  document.documentElement.style.colorScheme = "dark";
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", "#15171c");
}
