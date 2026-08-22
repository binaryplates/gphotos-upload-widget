# Install Photos Upload

Photos Upload is **two parts**:

1. **Extension** — panel icon and menu (from Extension Manager or extensions.gnome.org)
2. **Backend** — upload service, D-Bus, and systemd units (from GitHub, one time)

Install both. The extension alone is not enough.

## Step 1 — Extension (one click)

- Open **Extension Manager**, search for **Photos Upload**, install and enable  
  **or**
- Install from [extensions.gnome.org](https://extensions.gnome.org)

On Wayland, you may need to log out and back in once before the panel icon appears.

## Step 2 — Backend (one time, terminal)

Open a terminal and run:

```bash
git clone https://github.com/binaryplates/gphotos-upload-widget.git
cd gphotos-upload-widget
./scripts/install-backend.sh
```

No sudo needed. This installs the D-Bus service and user systemd units.

## Step 3 — rclone (one time)

You need `rclone` with a Google Photos remote:

```bash
rclone config
```

Create a remote named `gphotos:` (Google Photos). Optional: `gdrive:` for quota display.

OAuth client ID/secret can also be set in the extension’s **Settings → Google credentials**.

## All-in-one from GitHub (developers)

```bash
git clone https://github.com/binaryplates/gphotos-upload-widget.git
cd gphotos-upload-widget
./scripts/install.sh
```

## Check it works

1. Click the Photos Upload icon in the top panel.
2. Open the menu — you should see upload status, not a setup error.
3. If you see a setup notification, run Step 2.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No panel icon after install | Log out/in, then enable the extension in Extension Manager |
| “Install gphotos-upload-backend first” notification | Run Step 2 |
| Upload toggle does nothing | Run `./scripts/install-backend.sh` again; check `systemctl --user status rclone-gphotos.service` |

Repository: https://github.com/binaryplates/gphotos-upload-widget
