# Writing Polisher

Write rough, get back clear, correct, human-sounding text. Uses Groq's free tier. Change the model and default tone in `settings.json`; writing rules and tones are in `prompts.json`. Full guide: https://claude.ai/code/artifact/2a3ea055-b347-49e9-9d71-4c72906908a2

## Setup (once)
    polish set-key        # paste your Groq API key from console.groq.com/keys (saved to ~/.config/writing-polisher/api_key)

## Use it
- **Anywhere (hotkey):** highlight text in any app, press **Ctrl+Alt+P**. A small window opens and shows the rewrite as it is written; the result is copied automatically. Press Enter to close, then Ctrl+V.
- **Desktop app:** "Writing Polisher" in the app menu (or `polisher_app.py --new`) to type text in directly. `polish serve` still opens a browser version.
- **Terminal:** `polish "your text"`, `polish --tone firm "..."`, or `echo text | polish`

Tones: natural, professional, friendly, firm, concise, grammar.

Free-tier limits (check console.groq.com/settings/limits): ~30 requests/min, 1,000/day, 200K tokens/day. Roughly 150-300 polishes a day.

## Install on a new machine (Linux Mint / Cinnamon)
    git clone https://github.com/shahidiqbal407/grammer-assistant.git ~/projects/writing-polisher
    cd ~/projects/writing-polisher && npm install
    printf '#!/bin/sh\nexec "%s" "%s/polish.mjs" "$@"\n' "$(which node)" "$PWD" > ~/.local/bin/polish && chmod +x ~/.local/bin/polish
    polish set-key

Then add the shortcut in **System Settings → Keyboard → Shortcuts → Custom Shortcuts**:
command `/usr/bin/python3 ~/projects/writing-polisher/polisher_app.py`, keys **Ctrl+Alt+P**.
Needs `xclip` (`sudo apt install xclip` if missing).
