// Shared websocket client for both index.html and settings.html. Loaded via
// GET /ws-client.js; expects window.WS_TOKEN to already be set by the page
// (rendered in server-side by web.py, see templates/*.html) before connect()
// is called.
window.BarkWs = (function () {
  let ws = null;
  let nextId = 1;
  const pending = new Map();
  const listeners = { open: [], close: [], message: [] };

  function on(event, fn) {
    listeners[event].push(fn);
  }

  function emit(event, arg) {
    for (const fn of listeners[event]) fn(arg);
  }

  function connect() {
    const url = new URL('/ws', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', window.WS_TOKEN || '');
    ws = new WebSocket(url);

    ws.onopen = () => emit('open');
    ws.onclose = () => {
      emit('close');
      // best-effort auto-reconnect - a restart of the backend or a brief
      // network blip shouldn't require a manual page reload
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (msg.id !== undefined && msg.id !== null && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.ok === false) reject(new Error(msg.error || 'request failed'));
        else resolve(msg.data);
      }
      emit('message', msg);
    };
  }

  // waits (up to 5s) for the socket to be open before sending, so callers
  // made right after connect() (e.g. an initial loadMqtt()) don't have to
  // race the handshake themselves
  function send(type, payload, extra) {
    const deadline = Date.now() + 5000;
    return new Promise((resolve, reject) => {
      const attempt = () => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          const id = nextId++;
          pending.set(id, { resolve, reject });
          ws.send(JSON.stringify({ type, id, payload, ...extra }));
        } else if (Date.now() < deadline) {
          setTimeout(attempt, 100);
        } else {
          reject(new Error('not connected'));
        }
      };
      attempt();
    });
  }

  return { connect, on, send };
})();
