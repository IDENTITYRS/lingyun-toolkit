/* Keep the official information container, but compact it only for this iframe. */
(() => {
  'use strict';
  try {
    const frame = window.frameElement;
    const wrapper = frame && frame.closest('.plugin-iframe-wrapper');
    if (!wrapper || !wrapper.closest('.sidebar-project-plugin')) return;
    const info = wrapper.previousElementSibling;
    if (!info || !info.classList.contains('plugin-info')) return;

    const marker = 'data-lingyun-compact-host';
    frame.setAttribute(marker, '');
    const style = frame.ownerDocument.createElement('style');
    // :has scopes the rule to our live iframe, including when Vue reuses the info node.
    const selector = `.sidebar-project-plugin .plugin-info:has(+ .plugin-iframe-wrapper > iframe[${marker}])`;
    style.textContent = `
      ${selector} {
        box-sizing: border-box !important;
        height: 2px !important;
        min-height: 2px !important;
        max-height: 2px !important;
        padding: 0 !important;
        margin: 0 !important;
        gap: 0 !important;
        border: 0 !important;
        background: transparent !important;
        overflow: hidden !important;
        flex: 0 0 2px !important;
      }
      ${selector} > * { display: none !important; }
    `;
    frame.ownerDocument.head.appendChild(style);
    window.addEventListener('pagehide', () => {
      frame.removeAttribute(marker);
      style.remove();
    }, { once: true });
  } catch (_) {
    // Hosts that isolate iframe origins cannot expose their layout to plugins.
    // Leave their official UI untouched and let the plugin load normally.
  }
})();
