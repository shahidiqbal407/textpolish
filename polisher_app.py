#!/usr/bin/python3
"""Writing Polisher desktop app (native GTK).

    polisher_app.py          polish the highlighted (or copied) text; used by the Ctrl+Alt+P shortcut
    polisher_app.py --new    open with an empty box to type into; used by the app-menu launcher

Uses only what ships with Linux Mint: system Python, GTK 3 and xclip. Talks to Groq directly.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.expanduser("~/.config/textpolish/api_key")
# Older installs saved the key here; still read it so they keep working.
OLD_KEY_FILE = os.path.expanduser("~/.config/writing-polisher/api_key")
API_URL = "https://api.groq.com/openai/v1/chat/completions"


with open(os.path.join(HERE, "prompts.json")) as f:
    PROMPTS = json.load(f)

# Model and default tone live in settings.json (shared with the `polish` command); env vars override.
# Lines starting with // are comments, so alternative models can be kept there commented out.
with open(os.path.join(HERE, "settings.json")) as f:
    SETTINGS = json.loads("".join(line for line in f if not line.lstrip().startswith("//")))
MODEL = os.environ.get("POLISH_MODEL") or SETTINGS.get("model") or "openai/gpt-oss-120b"

TONE_LABELS = {
    "natural": "Natural",
    "professional": "Professional",
    "friendly": "Friendly",
    "firm": "Firm",
    "concise": "Concise",
    "grammar": "Grammar only",
}


def load_api_key():
    if os.environ.get("GROQ_API_KEY"):
        return os.environ["GROQ_API_KEY"]
    for path in (KEY_FILE, OLD_KEY_FILE):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            pass
    return None


def read_selection():
    """Highlighted text first, then whatever was copied with Ctrl+C."""
    for selection in ("primary", "clipboard"):
        try:
            out = subprocess.run(
                ["xclip", "-o", "-selection", selection], capture_output=True, text=True, timeout=2
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out.strip():
            return out
    return ""


def write_clipboard(text):
    # xclip keeps running to serve the clipboard after this app closes.
    p = subprocess.Popen(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    p.stdin.write(text.encode())
    p.stdin.close()


def clean_output(text):
    """Open models sometimes wrap the answer ("Here's the polished version:", quotes, <draft> tags)."""
    import re

    out = text.strip()
    out = re.sub(
        r"^(here(?:'s| is) (?:the |a |your )?(?:polished|rewritten|revised|corrected|improved)[^\n]*?:)\s*",
        "",
        out,
        flags=re.I,
    )
    out = re.sub(r"^<draft>\s*|\s*</draft>$", "", out)
    if len(out) > 1 and out[0] == out[-1] == '"' and '"' not in out[1:-1]:
        out = out[1:-1]
    return out.strip()


def stream_polish(text, tone):
    """Yields the rewrite piece by piece. Raises RuntimeError with a readable message on failure."""
    api_key = load_api_key()
    if not api_key:
        raise RuntimeError("No Groq API key found. Run: polish set-key")
    tone_line = PROMPTS["tones"].get(tone, PROMPTS["tones"]["natural"])
    body = {
        "model": MODEL,
        "max_completion_tokens": 4000,
        "stream": True,
        "messages": [
            {"role": "system", "content": PROMPTS["system"]},
            {"role": "user", "content": f"{tone_line}\n\n<draft>\n{text}\n</draft>"},
        ],
    }
    # Low reasoning keeps it fast and saves the free daily token allowance; hidden keeps it out of the reply.
    if MODEL.startswith("openai/gpt-oss"):
        body.update(reasoning_effort="low", include_reasoning=False)
    elif MODEL.startswith("qwen/"):
        body.update(reasoning_effort="low", reasoning_format="hidden")
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "writing-polisher/1.0",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Invalid Groq API key. Run: polish set-key")
        if e.code == 429:
            raise RuntimeError("Free-tier limit reached. Wait a minute (or until tomorrow if the daily cap is used up).")
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"Groq API error ({e.code}) {detail}".strip())
    except urllib.error.URLError:
        raise RuntimeError("Could not reach Groq. Check your internet connection.")

    with resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"].get("message", "Groq returned an error."))
            choices = chunk.get("choices") or [{}]
            delta = choices[0].get("delta", {}).get("content")
            if delta:
                yield delta


CSS = b"""
.polisher-title { font-weight: bold; }
.polisher-hint { opacity: 0.6; font-size: smaller; }
.polisher-error { color: #c0392b; }
.polisher-done { color: #2e8b57; }
textview.polisher-output { font-size: 12pt; }
textview, textview text { padding: 6px; }
"""


class PolisherWindow(Gtk.Window):
    def __init__(self, initial_text, auto_run):
        super().__init__(title="Writing Polisher")
        self.set_default_size(640, 480)
        self.set_icon_name("accessories-text-editor")
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.run_id = 0
        self.busy = False
        self.timer_source = None

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Header: title, live status, spinner and timer.
        header = Gtk.HeaderBar(show_close_button=True, title="Writing Polisher", subtitle="Ready")
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        self.timer_label = Gtk.Label()
        self.timer_label.get_style_context().add_class("polisher-hint")
        header.pack_end(self.timer_label)
        self.header = header
        self.set_titlebar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        self.add(box)

        box.pack_start(self._label("Your text", "Ctrl+Enter to polish again"), False, False, 0)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.input.get_buffer().set_text(initial_text)
        box.pack_start(self._scrolled(self.input, 90), False, True, 0)

        box.pack_start(self._label("Polished", "Edit it here if you like"), False, False, 6)
        self.output = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.get_style_context().add_class("polisher-output")
        box.pack_start(self._scrolled(self.output, 140), True, True, 0)

        self.progress = Gtk.ProgressBar()
        box.pack_start(self.progress, False, False, 2)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.status, False, False, 0)

        row = Gtk.Box(spacing=8)
        box.pack_start(row, False, False, 4)
        row.pack_start(Gtk.Label(label="Tone"), False, False, 0)
        self.tone = Gtk.ComboBoxText()
        for key in PROMPTS["tones"]:
            self.tone.append(key, TONE_LABELS.get(key, key.title()))
        self.tone.set_active_id(sys_tone())
        self.tone.connect("changed", lambda *_: self.polish())
        row.pack_start(self.tone, False, False, 0)

        self.copy_btn = Gtk.Button(label="Copy & close")
        self.copy_btn.get_style_context().add_class("suggested-action")
        self.copy_btn.set_can_default(True)
        self.copy_btn.connect("clicked", lambda *_: self.copy_and_close())
        row.pack_end(self.copy_btn, False, False, 0)
        self.polish_btn = Gtk.Button(label="Polish")
        self.polish_btn.connect("clicked", lambda *_: self.polish())
        row.pack_end(self.polish_btn, False, False, 0)

        self.connect("key-press-event", self.on_key)
        self.connect("destroy", Gtk.main_quit)
        self.copy_btn.set_sensitive(False)

        self.show_all()
        self.move_near_pointer()
        self.present_with_time(Gdk.CURRENT_TIME)

        if auto_run and initial_text.strip():
            self.polish()
        else:
            self.input.grab_focus()
            self.set_status("Type or paste your text, then press Ctrl+Enter.")

    def _label(self, title, hint):
        row = Gtk.Box(spacing=8)
        t = Gtk.Label(label=title, xalign=0)
        t.get_style_context().add_class("polisher-title")
        h = Gtk.Label(label=hint, xalign=0)
        h.get_style_context().add_class("polisher-hint")
        row.pack_start(t, False, False, 0)
        row.pack_start(h, False, False, 0)
        return row

    def _scrolled(self, child, min_height):
        sw = Gtk.ScrolledWindow(min_content_height=min_height)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        sw.add(child)
        return sw

    def move_near_pointer(self):
        display = Gdk.Display.get_default()
        _, x, y = display.get_default_seat().get_pointer().get_position()
        geo = display.get_monitor_at_point(x, y).get_geometry()
        w, h = self.get_size()
        nx = min(max(geo.x, x - w // 2), geo.x + geo.width - w)
        ny = min(max(geo.y, y - 80), geo.y + geo.height - h - 40)
        self.move(nx, ny)

    def set_status(self, text, kind=None):
        ctx = self.status.get_style_context()
        for c in ("polisher-error", "polisher-done"):
            ctx.remove_class(c)
        if kind:
            ctx.add_class(f"polisher-{kind}")
        self.status.set_text(text)

    # ----- polishing -----

    def polish(self):
        text = self._text(self.input)
        if not text.strip():
            self.set_status("Write something first.", "error")
            return
        if self.busy:
            return
        self.busy = True
        self.run_id += 1
        run_id = self.run_id
        self.output.get_buffer().set_text("")
        self.output.set_editable(False)
        for w in (self.polish_btn, self.copy_btn, self.tone):
            w.set_sensitive(False)
        self.spinner.start()
        self.header.set_subtitle("Thinking…")
        self.set_status("")
        self.started = time.monotonic()
        self.timer_source = GLib.timeout_add(100, self._tick)
        tone = self.tone.get_active_id()
        threading.Thread(target=self._worker, args=(run_id, text, tone), daemon=True).start()

    def _tick(self):
        self.timer_label.set_text(f"{time.monotonic() - self.started:.1f}s")
        if self.busy:
            self.progress.pulse()
        return self.busy

    def _worker(self, run_id, text, tone):
        raw = ""
        try:
            for delta in stream_polish(text, tone):
                raw += delta
                GLib.idle_add(self._on_delta, run_id, delta)
            result = clean_output(raw)
            if not result:
                raise RuntimeError("The model returned an empty response. Try again.")
            GLib.idle_add(self._on_done, run_id, result, None)
        except Exception as e:  # shown to the user in the window
            GLib.idle_add(self._on_done, run_id, None, str(e))

    def _on_delta(self, run_id, delta):
        if run_id != self.run_id:
            return False
        if self.header.get_subtitle() != "Writing…":
            self.header.set_subtitle("Writing…")
        buf = self.output.get_buffer()
        buf.insert(buf.get_end_iter(), delta)
        self.output.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)
        return False

    def _on_done(self, run_id, result, error):
        if run_id != self.run_id:
            return False
        self.busy = False
        self.spinner.stop()
        self.timer_label.set_text(f"{time.monotonic() - self.started:.1f}s")
        self.output.set_editable(True)
        for w in (self.polish_btn, self.tone):
            w.set_sensitive(True)
        if error:
            self.progress.set_fraction(0)
            self.header.set_subtitle("Failed")
            self.set_status(error, "error")
            return False
        self.progress.set_fraction(1)
        self.output.get_buffer().set_text(result)
        write_clipboard(result)
        self.header.set_subtitle("Done")
        self.set_status("Copied to clipboard. Press Enter to close, then Ctrl+V where you want it.", "done")
        self.copy_btn.set_sensitive(True)
        self.copy_btn.grab_default()
        self.copy_btn.grab_focus()
        return False

    # ----- actions -----

    def _text(self, view):
        buf = view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def copy_and_close(self):
        text = self._text(self.output).strip()
        if text:
            write_clipboard(text)
        self.destroy()

    def on_key(self, _widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        if ctrl and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.polish()
            return True
        return False


def sys_tone():
    tone = os.environ.get("POLISH_TONE") or SETTINGS.get("tone") or "natural"
    return tone if tone in PROMPTS["tones"] else "natural"


def main():
    new = "--new" in sys.argv
    text = "" if new else read_selection()
    PolisherWindow(text, auto_run=not new)
    Gtk.main()


if __name__ == "__main__":
    main()
