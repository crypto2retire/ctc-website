/**
 * Clear the Clutter — AI Estimator Widget Loader
 * Loads the whatshouldicharge.app widget directly into each .ai-widget-wrap element.
 */

(function () {
  'use strict';

  const wrappers = document.querySelectorAll('.ai-widget-wrap');
  if (!wrappers.length) return;

  wrappers.forEach(function (wrapper) {
    var script = document.createElement('script');
    script.src = 'https://whatshouldicharge.app/static/widget.js';
    script.setAttribute('data-slug', 'clear-the-clutter');
    wrapper.appendChild(script);
  });
})();
