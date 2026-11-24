// The structured bridge. In the desktop app: Qt WebChannel (object "bridge": signal message(str), slot command(str)).
// In a plain browser (development, automated performance tests): a fixture player that replays recorded messages.
import type { Command, InMsg } from "./types";

type Listener = (m: InMsg) => void;

export interface Bridge {
  send(c: Command): void;
  onMessage(l: Listener): void;
  kind: "qt" | "fixture";
}

declare global {
  interface Window {
    qt?: { webChannelTransport: unknown };
    QWebChannel?: new (t: unknown, cb: (ch: { objects: Record<string, QtBridge> }) => void) => unknown;
    __FIXTURE?: InMsg[];
    __bridgeSent?: Command[];
  }
}

interface QtBridge {
  message: { connect(cb: (s: string) => void): void };
  command(s: string): void;
}

function loadScript(src: string): Promise<void> {
  return new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => ok();
    s.onerror = () => fail(new Error(`cannot load ${src}`));
    document.head.appendChild(s);
  });
}

export async function connect(): Promise<Bridge> {
  const listeners: Listener[] = [];
  const emit = (m: InMsg) => listeners.forEach((l) => l(m));
  if (window.qt?.webChannelTransport) {
    if (!window.QWebChannel) await loadScript("qrc:///qtwebchannel/qwebchannel.js");
    const qt = await new Promise<QtBridge>((ok) => {
      new window.QWebChannel!(window.qt!.webChannelTransport, (ch) => ok(ch.objects.bridge));
    });
    qt.message.connect((s) => {
      try {
        emit(JSON.parse(s) as InMsg);
      } catch (e) {
        console.error("bad bridge message", e);
      }
    });
    const b: Bridge = { kind: "qt", send: (c) => qt.command(JSON.stringify(c)), onMessage: (l) => listeners.push(l) };
    setTimeout(() => b.send({ cmd: "hello" }), 0);
    return b;
  }
  // fixture mode: messages recorded from the Python engine (window.__FIXTURE), one per animation frame
  window.__bridgeSent = [];
  const b: Bridge = {
    kind: "fixture",
    send: (c) => window.__bridgeSent!.push(c),
    onMessage: (l) => listeners.push(l),
  };
  const play = () => {
    const msgs = window.__FIXTURE ?? [];
    let k = 0;
    const tick = () => {
      if (k < msgs.length) emit(msgs[k++]);
      if (k < msgs.length) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  setTimeout(play, 0);
  return b;
}
