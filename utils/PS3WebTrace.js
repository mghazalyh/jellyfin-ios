/**
 * Injected only when PS3 diagnostics are enabled. Records HTTP response
 * metadata for media and Surf routes; never forwards URLs, headers or bodies.
 * Original fetch/XHR behavior is preserved, including rejected promises.
 */
const ps3WebHook = `
(function () {
  if (window.__jfiPs3TraceInstalled) return;
  window.__jfiPs3TraceInstalled = true;
  var emitted = 0;
  function route(url) {
    var s = String(url || '').split('?')[0].toLowerCase();
    if (/\\/api\\/surf\\//.test(s)) return 'surf-control';
    if (/\\/live\\/(all|sports|movies-series)\\.ts/.test(s)) return 'ps3-continuous-ts';
    if (/\\/hls\\//.test(s) || /\\.m3u8$/.test(s)) return 'hls';
    if (/\\/livetv\\//.test(s)) return 'livetv';
    if (/\\/playbackinfo/.test(s)) return 'playback-info';
    if (/\\/videos\\//.test(s)) return 'jellyfin-media';
    return null;
  }
  function send(type, data) {
    try {
      if (++emitted > 250 || !window.ReactNativeWebView) return;
      window.ReactNativeWebView.postMessage(JSON.stringify({
        event: 'PS3Diagnostic.network', data: Object.assign({ status: type }, data)
      }));
    } catch (_) {}
  }
  if (typeof window.fetch === 'function') {
    var originalFetch = window.fetch;
    window.fetch = function () {
      var arg = arguments[0];
      var url = typeof arg === 'string' ? arg : (arg && arg.url);
      var selected = route(url);
      if (!selected) return originalFetch.apply(this, arguments);
      var start = Date.now();
      return originalFetch.apply(this, arguments).then(function (response) {
        send('complete', { route: selected, httpStatus: response.status,
          elapsedMs: Date.now() - start, contentType: String(response.headers.get('content-type') || '').split(';')[0] });
        return response;
      }, function (error) {
        send('failure', { route: selected, elapsedMs: Date.now() - start });
        throw error;
      });
    };
  }
  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
    var originalOpen = window.XMLHttpRequest.prototype.open;
    var originalSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      this.__jfiPs3Route = route(url);
      this.__jfiPs3Start = 0;
      return originalOpen.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.send = function () {
      if (this.__jfiPs3Route) {
        var xhr = this;
        xhr.__jfiPs3Start = Date.now();
        xhr.addEventListener('loadend', function () {
          var mime = '';
          try { mime = String(xhr.getResponseHeader('content-type') || '').split(';')[0]; } catch (_) {}
          send('complete', { route: xhr.__jfiPs3Route, httpStatus: xhr.status,
            elapsedMs: Date.now() - xhr.__jfiPs3Start, contentType: mime });
        }, { once: true });
      }
      return originalSend.apply(this, arguments);
    };
  }
})();
`;
export default ps3WebHook;
