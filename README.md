# NeuroSync

A desktop toolkit for Linux that brings your files, notes, music, study sessions and AI assistant together in one place. It includes a terminal file manager with tags and search, slide-out panels (an AI chat and a music player), widgets that sit on your desktop, a gamified study timer, Obsidian and Google Drive note syncing, a VPN switch, and a Telegram bot, all in one violet-terminal theme.

> **Platform:** Linux with an X11 session only. The interface and AI prompts are mostly in Russian.

---

## Table of contents

- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Installation](#installation)
- [Setup: your settings and keys](#setup-your-settings-and-keys)
- [Setup: VPN (optional)](#setup-vpn-optional)
- [Running](#running)
- [Using it](#using-it)
- [Where your data is stored](#where-your-data-is-stored)
- [Troubleshooting](#troubleshooting)

---

## What you get

**Terminal file manager (`ns`)** – browse your home folder in the terminal, tag files and search by name, path or tag. Files are indexed in the background.

**AI chat panel (slides out from the left edge)** – a chat powered by Google Gemini. Ask it in plain language to sort a folder, find a file, work with your Obsidian notes (overview, definitions, splitting long notes, fixing duplicates), or switch the VPN on and off.

**AI file sorter** – hands a messy folder to Gemini, which moves files into sensibly named folders and tags each file. A log of what moved where is saved as `neuro.txt` in that folder.

**Top panel (slides out from the top edge)** – SoundCloud music player, notes and the desktop pet.

**Desktop widgets** – small draggable windows that stay on your desktop:
- system stats with an equalizer and "now playing"
- detailed system info (CPU, RAM, disk, battery, network, uptime, temperatures, optional NVIDIA GPU)
- a random note from your Obsidian vault
- an RPG-style progress card (level, XP, HP, achievements)
- a pet that wanders around the screen
- a switch that turns hover-to-open on or off for the two slide-out panels

**Study timer** – focus sessions that earn XP and cost HP, with rewards for coming back after a break, sleep rewards and achievements. Sessions are logged into your Obsidian vault.

**Notes sync** – keeps your `~/NeuroNotes` folder in sync with Google Drive through `rclone`, and converts incoming `.txt` and `.docx` files to Markdown.

**VPN switch** – a button in the chat panel that turns your VPN on and off and shows its current state (see [Setup: VPN](#setup-vpn-optional)).

**Telegram bot** – browse your files from your phone, create and edit notes, and check a transit card balance (Minsk Metro, optional and Belarus-specific). The bot only answers you, not anyone else.

---

## Requirements

- Linux with an X11 session
- Python 3.10 or newer
- The Python packages from `requirements.txt` (installed in the steps below)

Optional extras, needed only for the feature in brackets:

- [`rclone`](https://rclone.org/) with a remote named `gdrive` (Google Drive sync)
- OpenVPN set up as a systemd service (VPN switch, see below)
- `nvidia-smi` (GPU info in the system info widget)
- `xdg-open` (opening files from the file manager, preinstalled on most distributions)

---

## Installation

```bash
git clone https://github.com/bohushpalina/<repo-name>.git
cd <repo-name>

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Setup: your settings and keys

NeuroSync reads your personal settings from a file called `local_config.py`. Create it from the template:

```bash
cp local_config.example.py local_config.py
```

Open `local_config.py` in any text editor and fill in what you need:

| Setting | What it is and where to get it |
|---|---|
| `GEMINI_API_KEY` | Needed for the AI chat and file sorter. Create a key in [Google AI Studio](https://aistudio.google.com/apikey) |
| `SOUNDCLOUD_CLIENT_ID`, `SOUNDCLOUD_OAUTH_TOKEN` | Needed for the music player only. Your SoundCloud account details |
| `OBSIDIAN_VAULT` | Full path to your Obsidian vault, for example `/home/you/Obsidian/MyVault` |
| `NOTES_DIR` | Folder for your notes. Default: `~/NeuroNotes` |
| `VPN_SERVICE_NAME` | Name of your VPN systemd service. Default: `openvpn-client@myvpn` |
| `TELEGRAM_BOT_TOKEN` | Needed for the bot. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token |
| `TELEGRAM_ALLOWED_USER_IDS` | Your own Telegram ID. Send any message to [@userinfobot](https://t.me/userinfobot) to find it. Several IDs can be separated by commas |
| `METRO_CARD_NUMBER` | Transit card number for the balance check (optional) |

You only need to fill in the settings for features you plan to use. Leave the rest empty.

Instead of the file, you can also set any value as an environment variable with the same name (it takes priority over the file):

```bash
export GEMINI_API_KEY="your-key"
```

Other options (Gemini model name, XP and HP balance, achievements, colors) can be changed directly in `ns_config.py`.

> The Telegram bot can show and send files from your home folder, so it answers only the IDs listed in `TELEGRAM_ALLOWED_USER_IDS`. If that setting is empty, the bot will not start.

---

## Setup: VPN (optional)

The chat panel has a VPN button that starts and stops an OpenVPN connection and shows whether it is active. When the VPN is on, all of your traffic goes through it, including requests to Gemini, which is handy if the AI service is not reachable from your region.

The button works with a systemd service named `openvpn-client@myvpn`. Here is how to set it up.

**1. Install OpenVPN and add your VPN config**

```bash
sudo apt install openvpn
sudo cp your-vpn-file.ovpn /etc/openvpn/client/myvpn.conf
```

The file name `myvpn` becomes the service name. If you choose a different name, set `VPN_SERVICE_NAME` in `local_config.py` accordingly (for example `openvpn-client@work`).

**2. Check that it works from the command line**

```bash
sudo systemctl start openvpn-client@myvpn
systemctl status openvpn-client@myvpn
sudo systemctl stop openvpn-client@myvpn
```

**3. Optional: command-line shortcuts**

Add these lines to `~/.bashrc` (then run `source ~/.bashrc`):

```bash
alias vpn-on='sudo systemctl start openvpn-client@myvpn'
alias vpn-off='sudo systemctl stop openvpn-client@myvpn'
alias vpn-status='systemctl status openvpn-client@myvpn'
```

Now `vpn-on`, `vpn-off` and `vpn-status` work in any terminal. The widget does not use these aliases: it talks to `systemctl` directly, so it works without them.

**4. Let the button work without a password prompt**

The button cannot type a sudo password, so allow exactly these two commands without one. Find your `systemctl` path with `which systemctl` (usually `/usr/bin/systemctl`), then run:

```bash
sudo visudo -f /etc/sudoers.d/neurosync-vpn
```

and add one line (replace `youruser` with your Linux username):

```
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl start openvpn-client@myvpn, /usr/bin/systemctl stop openvpn-client@myvpn
```

Save and exit. Reading the VPN status does not need sudo.

---

## Running

Every component is started separately:

| Command | What it starts |
|---|---|
| `python main.py` | Terminal file manager |
| `python llm.py` | AI chat panel (left edge) |
| `python neuro_panel.py` | Top panel with music, notes and pet |
| `python neuropanel_widget.py` | System stats and equalizer widget |
| `python info_widget.py` | Detailed system info widget |
| `python neurotext.py` | Random Obsidian note widget |
| `python neuro_game.py` | RPG progress widget |
| `python study_timer.py` | Study timer |
| `python neuro_pet.py` | Desktop pet |
| `python neuro_shutter_switch.py` | Panel on/off switch |
| `python bot.py` | Telegram bot |
| `python gdrive_sync.py` | One-time sync of your notes with Google Drive |

Run these from the project folder with the virtual environment active (`source venv/bin/activate`).

For quick access, add an alias to `~/.bashrc`, for example for the file manager:

```bash
alias ns='cd /path/to/NeuroSync && source venv/bin/activate && python main.py'
```

---

## Using it

- **Widgets** have no title bar. Drag them with the left mouse button to place them where you like.
- **Slide-out panels** open when you move the mouse to their screen edge: the AI chat on the left, the music panel at the top. Use the shutter switch widget to turn this on or off. Turning a panel off does not stop it, music keeps playing, it just stops reacting to the mouse until you switch it back on.
- **AI chat examples:** ask it to sort a folder (for example "sort my Downloads"), find a file ("where is the report about …"), or give an overview of a topic in your Obsidian notes.
- **Telegram bot:** open your bot in Telegram, send `/start`, and use the menu buttons (status, file explorer, notes).

---

## Where your data is stored

Everything stays on your computer, in your home folder:

| Path | Contents |
|---|---|
| `~/.neurosync/index.db` | File index and tags |
| `~/.neurosync_game.json` | Game progress (level, XP, HP, achievements) |
| `~/.neurosync_live.json` | Current state of the study timer |
| `~/.neurosync_pet.json` | Pet state |
| `~/NeuroNotes` | Your notes |
| `~/NeuroSync_Downloads` | Files you send to the Telegram bot |

To reset the game or the index, close NeuroSync and delete the matching file.

---

## Troubleshooting

- **"database is locked" or NeuroSync says it is already running** – another copy is still open. Close it (`pkill -f main.py`) and start again.
- **A window does not appear or you get a Qt platform error** – NeuroSync needs an X11 session. If you use Wayland, pick the "on Xorg" session at the login screen.
- **The AI chat returns errors** – check that `GEMINI_API_KEY` is filled in and valid. If the service is blocked in your region, turn the VPN on first.
- **Music does not play** – check the SoundCloud client ID and token in `local_config.py`.
- **The bot closes right after starting** – fill in both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS`.
- **The VPN button shows an error about a password** – the sudoers rule from step 4 of the VPN setup is missing or the service name does not match `VPN_SERVICE_NAME`.
- **The VPN button shows "off" even though the VPN is running** – the service name in `local_config.py` must match the one you started with `systemctl`.
