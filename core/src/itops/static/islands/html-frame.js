// Sizes each locked-down HTML frame (components.html `html_frame`) to its content, so a
// document reads as one page. The frame itself runs no scripts; this runs on the page.
(() => {
  function fit(frame) {
    const doc = frame.contentDocument;
    if (!doc || !doc.body) return;
    // The body, not the document: the document is at least as tall as the frame, so it
    // could only ever grow.
    frame.style.height = Math.ceil(doc.body.getBoundingClientRect().height) + "px";
  }
  function watch(frame) {
    if (frame.dataset.watched) return;
    frame.dataset.watched = "1";
    const start = () => {
      fit(frame);
      const doc = frame.contentDocument;
      if (!doc || !doc.body) return;
      // Content changes size as images load, <details> open, or the window resizes.
      new ResizeObserver(() => fit(frame)).observe(doc.body);
      doc.querySelectorAll("img").forEach((img) => img.addEventListener("load", () => fit(frame)));
    };
    frame.addEventListener("load", start);
    if (frame.contentDocument && frame.contentDocument.readyState === "complete") start();
  }
  const all = () => document.querySelectorAll("iframe[data-html-frame]").forEach(watch);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all);
  else all();
  document.addEventListener("htmx:afterSettle", all);
})();
