import { useEffect, useRef, useState } from "react";

/**
 * Auto-reconnecting WebSocket hook.
 * Returns { connected, lastMessage, lastEvent, send }.
 */
export function useWebSocket(url, { reconnectMs = 3000 } = {}) {
  const [connected, setConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState(null);
  const [lastEvent, setLastEvent] = useState(null);
  const ref = useRef(null);
  const reconnectTimer = useRef(null);

  useEffect(() => {
    let stopped = false;

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const full = url.startsWith("ws") ? url : `${proto}//${window.location.host}${url}`;
      const ws = new WebSocket(full);
      ref.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!stopped) {
          reconnectTimer.current = setTimeout(connect, reconnectMs);
        }
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          setLastMessage(data);
          if (data.event) setLastEvent(data);
        } catch {
          // ignore non-JSON
        }
      };
    }

    connect();
    return () => {
      stopped = true;
      clearTimeout(reconnectTimer.current);
      ref.current?.close();
    };
  }, [url, reconnectMs]);

  return {
    connected,
    lastMessage,
    lastEvent,
    send: (data) =>
      ref.current?.readyState === WebSocket.OPEN &&
      ref.current.send(typeof data === "string" ? data : JSON.stringify(data)),
  };
}
