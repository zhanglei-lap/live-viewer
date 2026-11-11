from __future__ import annotations

from .mpv_player import MpvPlayer


class PlayerWindow:
    """mpv owns the native window, fullscreen, input and its OSC controls."""

    def __init__(self, parent, *, on_close, on_pause, on_reconnect, on_volume, on_mute, volume=60, mute=False):
        self.on_close = on_close
        self.closed = False
        self.status_text = None
        self.player = MpvPlayer(volume=volume, mute=mute)

    def show(self, room_name):
        self.player.set_title(room_name)

    def toggle_fullscreen(self):
        self.player.command("cycle", "fullscreen")

    def leave_fullscreen(self):
        self.player.set_property("fullscreen", False)

    def update_status(self, text, paused=False):
        if not self.closed and text != self.status_text:
            self.status_text = text
            self.player.set_status(text)

    def close(self, *, notify=True):
        if self.closed:
            return
        self.closed = True
        self.player.close()
        if notify:
            self.on_close()
