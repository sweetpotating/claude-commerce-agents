// Shopping assistant bubble for a Shopify theme. Add before </body> in layout/theme.liquid:
//   <script src="https://YOUR-APP.onrender.com/widget.js" defer></script>
// It opens the app's chat page in a frame, so the theme's CSS and the chat's never mix.
(() => {
  if (window.__shoppingAssistant) return;
  window.__shoppingAssistant = true;
  const origin = new URL(document.currentScript.src).origin;
  const KEY = "shopping-assistant-open";
  const store = {
    get: () => { try { return sessionStorage.getItem(KEY) === "1"; } catch { return false; } },
    set: (v) => { try { sessionStorage.setItem(KEY, v ? "1" : "0"); } catch {} },
  };

  const css = document.createElement("style");
  css.textContent = `
    .sa-bubble { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; width: 56px; height: 56px;
      border-radius: 50%; border: 0; cursor: pointer; background: #1f5f4a; color: #fff;
      box-shadow: 0 6px 20px rgba(0,0,0,.25); display: flex; align-items: center; justify-content: center; }
    .sa-bubble svg { width: 26px; height: 26px; }
    .sa-frame { position: fixed; right: 20px; bottom: 88px; z-index: 2147483000; width: 380px; height: 600px;
      max-height: calc(100vh - 110px); border: 0; border-radius: 16px; background: #fff;
      box-shadow: 0 12px 40px rgba(0,0,0,.3); display: none; }
    .sa-frame.open { display: block; }
    @media (max-width: 480px) {
      .sa-frame { right: 0; bottom: 0; width: 100vw; height: 100%; max-height: none; border-radius: 0; }
      .sa-frame.open + .sa-bubble { display: none; }
    }`;
  document.head.appendChild(css);

  const frame = document.createElement("iframe");
  frame.className = "sa-frame";
  frame.title = "Shopping assistant";
  frame.allow = "clipboard-write";
  let loaded = false;

  const bubble = document.createElement("button");
  bubble.className = "sa-bubble";
  bubble.type = "button";
  bubble.setAttribute("aria-label", "Open shopping assistant");
  bubble.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

  function setOpen(open) {
    if (open && !loaded) { frame.src = `${origin}/?embed=1`; loaded = true; }
    frame.classList.toggle("open", open);
    bubble.setAttribute("aria-expanded", String(open));
    store.set(open);
  }
  bubble.addEventListener("click", () => setOpen(!frame.classList.contains("open")));
  window.addEventListener("message", (e) => {
    if (e.origin === origin && e.data && e.data.type === "shopping-assistant:close") setOpen(false);
  });

  document.body.appendChild(frame);
  document.body.appendChild(bubble);
  if (store.get()) setOpen(true);
})();
