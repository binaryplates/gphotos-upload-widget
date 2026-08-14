import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import { ExtensionPreferences } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';
import { DBusClient } from './lib/dbusClient.js';

const REFRESH_PRESETS = [
    [5, '5 seconds'], [15, '15 seconds'], [30, '30 seconds'], [60, '1 minute'],
    [300, '5 minutes'], [900, '15 minutes'], [1800, '30 minutes'], [3600, '1 hour'],
    [7200, '2 hours'], [21600, '6 hours'], [43200, '12 hours'],
];

export default class GPhotosUploadPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const client = new DBusClient();

        window.add(this._buildGeneralPage(settings));
        window.add(this._buildSourcesPage(client));
        window.add(this._buildCredentialsPage(client));
    }

    _buildGeneralPage(settings) {
        const page = new Adw.PreferencesPage({ title: 'General', icon_name: 'preferences-system-symbolic' });
        const group = new Adw.PreferencesGroup({ title: 'Refresh' });
        page.add(group);

        const model = new Gtk.StringList();
        REFRESH_PRESETS.forEach(([, label]) => model.append(label));
        const current = settings.get_int('refresh-seconds');
        const currentIndex = Math.max(0, REFRESH_PRESETS.findIndex(([secs]) => secs === current));

        const row = new Adw.ComboRow({
            title: 'Refresh interval',
            subtitle: 'How often status/progress updates while the menu is open',
            model,
            selected: currentIndex,
        });
        row.connect('notify::selected', () => {
            const [secs] = REFRESH_PRESETS[row.selected];
            settings.set_int('refresh-seconds', secs);
        });
        group.add(row);
        return page;
    }

    _buildSourcesPage(client) {
        const page = new Adw.PreferencesPage({ title: 'Sources', icon_name: 'folder-symbolic' });
        const group = new Adw.PreferencesGroup({ title: 'Upload sources' });
        page.add(group);

        const listGroup = new Adw.PreferencesGroup();
        page.add(listGroup);

        let currentRows = [];
        const reload = async () => {
            for (const row of currentRows)
                listGroup.remove(row);
            currentRows = [];
            const result = await client.call('SourcesList');
            const sources = result.ok ? result.sources || [] : [];
            if (!sources.length) {
                const emptyRow = new Adw.ActionRow({ title: 'No source folders configured' });
                listGroup.add(emptyRow);
                currentRows.push(emptyRow);
            }
            for (const item of sources) {
                const state = item.cancelled ? 'Cancelled' : item.paused ? 'Paused' : 'Active';
                const actionRow = new Adw.ActionRow({ title: item.path, subtitle: `${item.album || '—'} · ${state}` });

                const pauseBtn = new Gtk.Button({ icon_name: item.paused ? 'media-playback-start-symbolic' : 'media-playback-pause-symbolic', valign: Gtk.Align.CENTER, css_classes: ['flat'] });
                pauseBtn.connect('clicked', async () => {
                    await client.call(item.paused ? 'SourcesResume' : 'SourcesPause', item.path);
                    reload();
                });
                const cancelBtn = new Gtk.Button({ icon_name: 'process-stop-symbolic', valign: Gtk.Align.CENTER, css_classes: ['flat'] });
                cancelBtn.connect('clicked', async () => {
                    await client.call('SourcesCancel', item.path);
                    reload();
                });
                const removeBtn = new Gtk.Button({ icon_name: 'user-trash-symbolic', valign: Gtk.Align.CENTER, css_classes: ['flat'] });
                removeBtn.connect('clicked', async () => {
                    await client.call('SourcesRemove', item.path);
                    reload();
                });
                actionRow.add_suffix(pauseBtn);
                actionRow.add_suffix(cancelBtn);
                actionRow.add_suffix(removeBtn);
                listGroup.add(actionRow);
                currentRows.push(actionRow);
            }
        };

        const albumEntry = new Adw.EntryRow({ title: 'Album name (optional — defaults to folder name)' });
        const addRow = new Adw.ActionRow({ title: 'Add source folder…' });
        const addBtn = new Gtk.Button({ icon_name: 'folder-new-symbolic', valign: Gtk.Align.CENTER, css_classes: ['flat'] });
        addBtn.connect('clicked', () => {
            const dialog = new Gtk.FileDialog({ title: 'Choose a folder to upload' });
            dialog.select_folder(page.get_root(), null, async (_src, res) => {
                try {
                    const folder = dialog.select_folder_finish(res);
                    const path = folder.get_path();
                    if (path) {
                        await client.call('SourcesAdd', path, albumEntry.text || '');
                        albumEntry.text = '';
                        reload();
                    }
                } catch (e) {
                    // user cancelled the folder chooser
                }
            });
        });
        addRow.add_suffix(addBtn);
        group.add(albumEntry);
        group.add(addRow);

        reload();
        return page;
    }

    _buildCredentialsPage(client) {
        const page = new Adw.PreferencesPage({ title: 'Google credentials', icon_name: 'dialog-password-symbolic' });
        const group = new Adw.PreferencesGroup({ title: 'OAuth client (used by rclone)' });
        page.add(group);

        const clientIdRow = new Adw.PasswordEntryRow({ title: 'Client ID' });
        const clientSecretRow = new Adw.PasswordEntryRow({ title: 'Client secret' });
        const apiKeyRow = new Adw.PasswordEntryRow({ title: 'API key (optional)' });
        group.add(clientIdRow);
        group.add(clientSecretRow);
        group.add(apiKeyRow);

        const statusRow = new Adw.ActionRow({ title: 'Status', subtitle: 'Loading…' });
        group.add(statusRow);

        const actionsGroup = new Adw.PreferencesGroup();
        page.add(actionsGroup);
        const saveRow = new Adw.ActionRow({ title: 'Save changes' });
        const saveBtn = new Gtk.Button({ label: 'Save', valign: Gtk.Align.CENTER, css_classes: ['suggested-action'] });
        saveBtn.connect('clicked', async () => {
            const result = await client.call(
                'CredentialsSet', clientIdRow.text || '', clientSecretRow.text || '', apiKeyRow.text || '',
            );
            statusRow.subtitle = result.ok ? (result.message || 'Saved.') : (result.message || 'Save failed.');
            clientIdRow.text = '';
            clientSecretRow.text = '';
            apiKeyRow.text = '';
            refresh();
        });
        saveRow.add_suffix(saveBtn);
        actionsGroup.add(saveRow);

        const testRow = new Adw.ActionRow({ title: 'Test connection' });
        const testBtn = new Gtk.Button({ label: 'Test', valign: Gtk.Align.CENTER });
        testBtn.connect('clicked', async () => {
            statusRow.subtitle = 'Testing…';
            const result = await client.call('CredentialsTest');
            statusRow.subtitle = result.message || (result.ok ? 'OK' : 'Failed');
        });
        testRow.add_suffix(testBtn);
        actionsGroup.add(testRow);

        const refresh = async () => {
            const result = await client.call('CredentialsGet');
            if (result.ok) {
                statusRow.subtitle = `Client ID: ${result.client_id} · Secret: ${result.client_secret} · API key: ${result.api_key}`;
            } else {
                statusRow.subtitle = result.error || 'Could not read credentials.';
            }
        };
        refresh();

        return page;
    }
}
