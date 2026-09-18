# Silent Zoom Attendee

Joins Zoom meetings through the **browser client** (no Zoom desktop app), with:

- microphone muted and camera off, enforced every 15 seconds
- a fake camera/mic at the Chromium level, so there is no real hardware to leak
- no chat, no reactions, no responses of any kind
- a schedule: joins at start time, leaves at end time
- automatic rejoin if the host ends-and-restarts, if you get removed, or if the
  page crashes — it keeps retrying until the end time passes

## Files

| File | Purpose |
|---|---|
| `zoom_bot.py` | the whole program |
| `config.json` | your meetings and schedule |
| `requirements.txt` | dependencies |
| `Dockerfile` | container build (includes Xvfb) |
| `github-actions-workflow.yml` | free scheduled runs on GitHub |

## 1. Local setup (test here first)

```bash
pip install -r requirements.txt
playwright install chromium
```

Edit `config.json`:

- `url` — paste the full invite link (`https://us05web.zoom.us/j/123...?pwd=...`).
  The script converts it to `app.zoom.us/wc/...` automatically so it never tries
  to open the desktop app.
- `passcode` — only if the link has no `pwd=` in it.
- `days` — any of `mon tue wed thu fri sat sun`.
- `start` / `end` — 24-hour `HH:MM` in your `timezone`.

Test a real join for 2 minutes with a visible window:

```bash
python zoom_bot.py --test "Morning Class" --headful
```

Watch it. Check `screenshots/` afterwards. Once that works:

```bash
python zoom_bot.py            # scheduler mode, runs forever
```

## 2. Run it on a server (Ubuntu VM)

```bash
sudo apt update && sudo apt install -y python3-pip xvfb
pip3 install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

Create `/etc/systemd/system/zoombot.service`:

```ini
[Unit]
Description=Zoom attendee bot
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/zoombot
Environment=DISPLAY=:99
ExecStartPre=/bin/sh -c 'Xvfb :99 -screen 0 1280x800x24 -nolisten tcp & sleep 2'
ExecStart=/usr/bin/python3 /home/ubuntu/zoombot/zoom_bot.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now zoombot
journalctl -u zoombot -f     # live logs
```

## 3. Or run it with Docker

```bash
docker build -t zoombot .
docker run -d --name zoombot --restart unless-stopped --shm-size=1g zoombot
docker logs -f zoombot
```

`--shm-size=1g` matters — Chromium crashes with the default 64 MB.

## Troubleshooting

**It sits on a screen and never joins.** Zoom renames its buttons regularly.
Run with `--test` and `--headful`, open `screenshots/`, find the button it got
stuck on, right-click → Inspect, and add that selector to the matching list in
`zoom_bot.py` (`click_first([...])`).

**Stuck in the waiting room.** Nothing to fix — the host has to admit you. The
bot waits and keeps retrying.

**"This meeting requires sign-in."** The web client will demand a Zoom account.
You would need a persistent logged-in profile: launch with
`pw.chromium.launch_persistent_context("./profile", ...)`, log in once manually
with `--headful`, then reuse that folder. The current script does not do this.

**Headless mode fails.** Keep `"headless": false` and use Xvfb. The Zoom web
client behaves badly in true headless Chromium.

**RAM.** Chromium + Zoom web needs roughly 700 MB–1 GB. On a 1 GB VM add 2 GB of
swap: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap
/swapfile && sudo swapon /swapfile`.
