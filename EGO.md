# extensions.gnome.org submission

See the same workflow in `cpu-turbo-widget/EGO.md`. Build zip:

```bash
./scripts/ego-pack.sh
```

Upload `dist/ego/gphotos-upload@blazorplate.net-v<version>.zip` at https://extensions.gnome.org/upload .

Backend setup is bundled: `enable()` runs `backend/run-setup.sh`, which pip-installs the D-Bus service, writes user systemd units, and starts the upload worker. No separate GitHub step for end users.
