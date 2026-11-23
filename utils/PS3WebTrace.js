/** Opt-in PS3 server identity and redacted transport tracing for WebView. */
const ps3WebHook = String.raw`
(function () {
  if (window.__jfiPs3TraceInstalled) return;
  window.__jfiPs3TraceInstalled = true;
  var total = 0;
  var originalFetch = window.fetch;
  function route(url) {
    var name = String(url || '').split('?')[0].toLowerCase();
    if (name.indexOf('/api/surf/') >= 0) return 'surf-control';
    if (name.indexOf('/live/') >= 0 && name.indexOf('.ts') >= 0) return 'ps3-continuous-ts';
    if (name.indexOf('/hls/') >= 0 || name.indexOf('.m3u8') >= 0) return 'hls';
    if (name.indexOf('/livetv/') >= 0) return 'livetv';
    if (name.indexOf('/playbackinfo') >= 0) return 'playback-info';
    if (name.indexOf('/videos/') >= 0) return 'jellyfin-media';
    return null;
  }
  function spoofIdentity(value) {
    var input = String(value || '');
    if (!/^MediaBrowser(\s|$)/i.test(input)) return input;
    var result = input;
    [['Client', 'PS3'], ['Device', 'PS3'], ['Version', '0.1']].forEach(function (field) {
      var name = field[0], val = field[1];
      var pattern = new RegExp('(^|[ ,])' + name + '=(?:"[^"]*"|[^, ]+)', 'i');
      if (pattern.test(result)) result = result.replace(pattern, '$1' + name + '="' + val + '"');
      else result += ', ' + name + '="' + val + '"';
    });
    // DeviceId and Token are deliberately preserved from the iPad login.
    return result;
  }
  function patchHeaders(original) {
    if (typeof Headers !== 'function') return original;
    var result = new Headers(original || {});
    ['Authorization', 'X-Emby-Authorization'].forEach(function (name) {
      if (result.has(name)) result.set(name, spoofIdentity(result.get(name)));
    });
    return result;
  }
  function trace(state, data) {
    try {
      if (++total > 250 || !window.ReactNativeWebView) return;
      window.ReactNativeWebView.postMessage(JSON.stringify({
        event: 'PS3Diagnostic.network',
        data: Object.assign({ status: state }, data)
      }));
    } catch (error) {}
  }
  if (typeof originalFetch === 'function') {
    window.fetch = function (input, init) {
      var req = input, opts = init || {};
      var methodHeaders = opts.headers;
      var isRequest = typeof Request === 'function' && input instanceof Request;
      var url = typeof input === 'string' ? input : (input && input.url);
      var match = route(url);
      try {
        if (isRequest) {
          var cloned = new Headers(input.headers);
          if (methodHeaders) new Headers(methodHeaders).forEach(function (val, key) {
            cloned.set(key, val);
          });
          req = new Request(input, Object.assign({}, opts, { headers: patchHeaders(cloned) }));
          opts = undefined;
        } else if (methodHeaders) {
          opts = Object.assign({}, opts, { headers: patchHeaders(methodHeaders) });
        }
      } catch (error) {
        req = input;
        opts = init;
      }
      var started = Date.now();
      return originalFetch.call(this, req, opts).then(function (response) {
        if (match) trace('complete', {
          route: match,
          httpStatus: response.status,
          elapsedMs: Date.now() - started,
          contentType: String(response.headers.get('content-type') || '').split(';')[0]
        });
        return response;
      }, function (error) {
        if (match) trace('failure', { route: match, elapsedMs: Date.now() - started });
        throw error;
      });
    };
  }
  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
    var proto = window.XMLHttpRequest.prototype;
    var nativeOpen = proto.open;
    var nativeSetHeader = proto.setRequestHeader;
    var nativeSend = proto.send;
    proto.open = function (method, url) {
      this.__jfiRoute = route(url);
      return nativeOpen.apply(this, arguments);
    };
    proto.setRequestHeader = function (name, value) {
      var header = String(name).toLowerCase();
      if (header === 'authorization' || header === 'x-emby-authorization') {
        return nativeSetHeader.call(this, name, spoofIdentity(value));
      }
      return nativeSetHeader.apply(this, arguments);
    };
    proto.send = function () {
      if (this.__jfiRoute) {
        var xhr = this;
        var begin = Date.now();
        xhr.addEventListener('loadend', function () {
          var mime = '';
          try { mime = String(xhr.getResponseHeader('content-type') || '').split(';')[0]; }
          catch (error) {}
          trace('complete', {
            route: xhr.__jfiRoute,
            httpStatus: xhr.status,
            elapsedMs: Date.now() - begin, contentType: mime
          });
        }, { once: true });
      }
      return nativeSend.apply(this, arguments);
    };
  }
})();

`;
export default ps3WebHook;
