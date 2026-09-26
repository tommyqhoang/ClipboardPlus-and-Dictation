"""A minimal X11 clipboard owner for tests: serves TARGETS, plain data and INCR transfers.

It lets tests copy to the real (virtual) X server the way an application would, so the
watcher is exercised against the actual protocol instead of a mock.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from Xlib import X, Xatom, display
from Xlib.protocol import event as xevent


class XOwner(threading.Thread):
    """Owns CLIPBOARD on its own connection. `chunk` sets the INCR chunk size."""

    def __init__(self, chunk: int = 32_768) -> None:
        super().__init__(daemon=True)
        self.chunk = chunk
        self.requests: list[str] = []  # Target names the requestor asked for.
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._ready = threading.Event()
        self._closing = threading.Event()
        self.start()
        self._ready.wait(5)

    # -- called from the test thread ---------------------------------------
    def copy(self, targets: dict[str, bytes]) -> None:
        """Become the owner offering these targets (name -> bytes)."""
        self._commands.put(("copy", targets))
        self._sync()

    def disown(self) -> None:
        """Give up the selection, as when the copying application exits."""
        self._commands.put(("disown", None))
        self._sync()

    def close(self) -> None:
        self._closing.set()
        self.join(5)

    def _sync(self) -> None:
        done = threading.Event()
        self._commands.put(("sync", done))
        done.wait(5)

    # -- owner thread ------------------------------------------------------
    def run(self) -> None:
        self.display = display.Display()
        self.window = self._new_window()
        self.clipboard = self.display.intern_atom("CLIPBOARD")
        self.targets_atom = self.display.intern_atom("TARGETS")
        self.incr = self.display.intern_atom("INCR")
        self.targets: dict[str, bytes] = {}
        self.transfers: dict[tuple[int, int], tuple[int, list[bytes]]] = {}
        self._ready.set()
        while not self._closing.is_set():
            try:
                command, argument = self._commands.get_nowait()
            except queue.Empty:
                command, argument = "", None
            if command == "copy":
                self.targets = argument
                self.window.set_selection_owner(self.clipboard, X.CurrentTime)
                self.display.flush()
            elif command == "disown":
                # What happens when the copying application exits.
                self.targets = {}
                self.window.destroy()
                self.window = self._new_window()
                self.display.flush()
            elif command == "sync":
                self.display.sync()
                argument.set()
            if self.display.pending_events():
                self._handle(self.display.next_event())
            else:
                time.sleep(0.005)
        self.display.close()

    def _new_window(self) -> Any:
        screen = self.display.screen()
        return screen.root.create_window(0, 0, 1, 1, 0, screen.root_depth)

    def _handle(self, event: Any) -> None:
        if event.type == X.SelectionRequest:
            self._serve(event)
        elif event.type == X.PropertyNotify and event.state == X.PropertyDelete:
            self._next_chunk(event.window, event.atom)

    def _serve(self, event: Any) -> None:
        display_ = self.display
        target_name = display_.get_atom_name(event.target)
        self.requests.append(target_name)
        prop = event.property or event.target
        reply = event.property
        if event.target == self.targets_atom:
            names = [display_.intern_atom(name) for name in self.targets]
            event.requestor.change_property(prop, Xatom.ATOM, 32, [self.targets_atom, *names])
        elif target_name in self.targets:
            data = self.targets[target_name]
            target_type = event.target
            if len(data) > self.chunk:
                event.requestor.change_attributes(event_mask=X.PropertyChangeMask)
                event.requestor.change_property(prop, self.incr, 32, [len(data)])
                chunks = [data[i : i + self.chunk] for i in range(0, len(data), self.chunk)]
                self.transfers[(event.requestor.id, prop)] = (target_type, chunks + [b""])
            else:
                event.requestor.change_property(prop, target_type, 8, data)
        else:
            reply = X.NONE
        notify = xevent.SelectionNotify(
            time=event.time,
            requestor=event.requestor,
            selection=event.selection,
            target=event.target,
            property=reply,
        )
        event.requestor.send_event(notify)
        display_.flush()

    def _next_chunk(self, window: Any, prop: int) -> None:
        state = self.transfers.get((window.id, prop))
        if state is None:
            return
        target_type, chunks = state
        window.change_property(prop, target_type, 8, chunks.pop(0))
        self.display.flush()
        if not chunks:
            del self.transfers[(window.id, prop)]
