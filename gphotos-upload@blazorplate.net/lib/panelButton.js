import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import { DBusClient } from './dbusClient.js';
import { StatusRunner } from './statusRunner.js';
import { UploadActions } from './uploadActions.js';
import { SourcesStore } from './sourcesStore.js';
import { fmtBytes, fmtPercent, sourceState, sourceLabel } from './format.js';

export const GPhotosUploadIndicator = GObject.registerClass(
class GPhotosUploadIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Photos Upload');
        this._extension = extension;
        this._settings = extension.getSettings();

        this._iconOn = Gio.icon_new_for_string(`${extension.path}/icons/gphotos-upload-on.svg`);
        this._iconOff = Gio.icon_new_for_string(`${extension.path}/icons/gphotos-upload-off.svg`);
        this._icon = new St.Icon({ gicon: this._iconOff, style_class: 'system-status-icon' });
        this.add_child(this._icon);

        const client = new DBusClient();
        this._statusRunner = new StatusRunner(client, this._settings);
        this._actions = new UploadActions(client);
        this._sources = new SourcesStore(client);

        this._busy = false;
        this._lastSnapshot = null;

        this._buildMenu();

        this.menu.connect('open-state-changed', (menu, open) => {
            if (open)
                this._statusRunner.startPolling(payload => this._onStatus(payload));
            else
                this._statusRunner.stopPolling();
        });
    }

    _wrap(widget) {
        const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        item.add_child(widget);
        return item;
    }

    _sectionLabel(text, centered = false) {
        const params = { text, style_class: 'gphotos-upload-section-head' };
        if (centered) {
            params.x_expand = true;
            params.x_align = Clutter.ActorAlign.CENTER;
        }
        return new St.Label(params);
    }

    _buildMenu() {
        this.menu.addMenuItem(this._wrap(this._buildHeader()));
        this.menu.addMenuItem(this._wrap(this._buildToggleRow()));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._wrap(this._buildQuickActionsRow()));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._wrap(this._sectionLabel('OVERALL', true)));
        this.menu.addMenuItem(this._wrap(this._buildHeroRow()));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._wrap(this._sectionLabel('SPEED', true)));
        this.menu.addMenuItem(this._wrap(this._buildSpeedRow()));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._wrap(this._sectionLabel('SOURCES', false)));
        this.menu.addMenuItem(this._wrap(this._buildSourcesGrid()));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._wrap(this._sectionLabel('STORAGE', false)));
        this.menu.addMenuItem(this._wrap(this._buildQuotaRows()));

        this._errorLabel = new St.Label({ style_class: 'gphotos-upload-error', text: '', visible: false });
        this._errorLabel.clutter_text.line_wrap = true;
        this.menu.addMenuItem(this._wrap(this._errorLabel));

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const settingsItem = new PopupMenu.PopupMenuItem('Settings…');
        settingsItem.connect('activate', () => this._extension.openPreferences());
        this.menu.addMenuItem(settingsItem);
    }

    _buildHeader() {
        const row = new St.BoxLayout({ style_class: 'gphotos-upload-header-row', x_expand: true });
        row.add_child(new St.Label({
            text: 'PHOTOS UPLOAD',
            style_class: 'gphotos-upload-title',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        }));
        return row;
    }

    _buildToggleRow() {
        const box = new St.BoxLayout({ vertical: true, x_expand: true });
        this._toggleBtn = new St.Button({ style_class: 'gphotos-upload-toggle-btn gphotos-upload-off', label: '●  OFF' });
        this._toggleBtn.connect('clicked', () => this._onToggle());
        const toggleWrap = new St.BoxLayout({ x_expand: true, x_align: Clutter.ActorAlign.CENTER });
        toggleWrap.add_child(this._toggleBtn);
        box.add_child(toggleWrap);
        this._statusLine = new St.Label({
            text: 'Upload is off',
            style_class: 'gphotos-upload-status-line',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._statusLine);
        this._refreshBtn = new St.Button({
            style_class: 'gphotos-upload-icon-btn',
            child: new St.Icon({ icon_name: 'view-refresh-symbolic', style_class: 'gphotos-upload-icon-btn-icon' }),
        });
        this._refreshBtn.connect('clicked', () => this._statusRunner.refreshNow(true));
        const refreshWrap = new St.BoxLayout({ x_expand: true, x_align: Clutter.ActorAlign.CENTER, style_class: 'gphotos-upload-refresh-wrap' });
        refreshWrap.add_child(this._refreshBtn);
        box.add_child(refreshWrap);
        return box;
    }

    _buildQuickActionsRow() {
        const row = new St.BoxLayout({
            style_class: 'gphotos-upload-chip-row',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        const pauseBtn = new St.Button({ label: 'Pause all', style_class: 'gphotos-upload-chip' });
        const resumeBtn = new St.Button({ label: 'Resume all', style_class: 'gphotos-upload-chip' });
        const cancelBtn = new St.Button({ label: 'Cancel all', style_class: 'gphotos-upload-chip' });
        const reconcileBtn = new St.Button({ label: 'Reconcile', style_class: 'gphotos-upload-chip' });
        pauseBtn.connect('clicked', () => this._bulkSourceAction('pause'));
        resumeBtn.connect('clicked', () => this._bulkSourceAction('resume'));
        cancelBtn.connect('clicked', () => this._bulkSourceAction('cancel'));
        reconcileBtn.connect('clicked', () => this._onReconcile());
        row.add_child(pauseBtn);
        row.add_child(resumeBtn);
        row.add_child(cancelBtn);
        row.add_child(reconcileBtn);
        return row;
    }

    _buildHeroRow() {
        const hero = new St.BoxLayout({
            style_class: 'gphotos-upload-hero-row',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._pctValue = new St.Label({
            style_class: 'gphotos-upload-hero-value', text: '—',
            x_expand: true, x_align: Clutter.ActorAlign.CENTER,
        });
        this._pctCaption = new St.Label({
            style_class: 'gphotos-upload-hero-caption', text: 'Uploaded',
            x_expand: true, x_align: Clutter.ActorAlign.CENTER,
        });
        const pctCol = new St.BoxLayout({ vertical: true });
        pctCol.add_child(this._pctValue);
        pctCol.add_child(this._pctCaption);

        this._remainingValue = new St.Label({
            style_class: 'gphotos-upload-hero-value', text: '—',
            x_expand: true, x_align: Clutter.ActorAlign.CENTER,
        });
        this._remainingCaption = new St.Label({
            style_class: 'gphotos-upload-hero-caption', text: 'Remaining',
            x_expand: true, x_align: Clutter.ActorAlign.CENTER,
        });
        const remainingCol = new St.BoxLayout({ vertical: true });
        remainingCol.add_child(this._remainingValue);
        remainingCol.add_child(this._remainingCaption);

        hero.add_child(pctCol);
        hero.add_child(remainingCol);
        return hero;
    }

    _buildSpeedRow() {
        const row = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._speedBadge = new St.Label({ text: '—', style_class: 'gphotos-upload-rate-badge', x_expand: true, x_align: Clutter.ActorAlign.CENTER });
        this._speedText = new St.Label({ text: '', style_class: 'gphotos-upload-speed-text', x_expand: true, x_align: Clutter.ActorAlign.CENTER });
        row.add_child(this._speedBadge);
        row.add_child(this._speedText);
        return row;
    }

    _buildSourcesGrid() {
        this._sourcesLayout = new Clutter.GridLayout({ column_spacing: 14, row_spacing: 4 });
        this._sourcesGrid = new St.Widget({ layout_manager: this._sourcesLayout, style_class: 'gphotos-upload-grid', x_expand: true });
        this._gridRow(this._sourcesLayout, 0, ['Source', 'Album', 'State'], true);
        this._sourceRowLabels = [];
        return this._sourcesGrid;
    }

    _gridRow(layout, rowIndex, cells, isHeader) {
        return cells.map((text, col) => {
            const label = new St.Label({
                text: String(text),
                style_class: isHeader ? 'gphotos-upload-grid-head' : 'gphotos-upload-grid-row',
                x_align: col === 0 ? Clutter.ActorAlign.START : Clutter.ActorAlign.CENTER,
                x_expand: true,
            });
            layout.attach(label, col, rowIndex, 1, 1);
            return label;
        });
    }

    _buildQuotaRows() {
        this._quotaBox = new St.BoxLayout({ vertical: true, style_class: 'gphotos-upload-summary-box' });
        this._driveRow = this._quotaLine('Drive', '—');
        this._apiRow = this._quotaLine('API limit (today)', '—');
        this._quotaBox.add_child(this._driveRow.row);
        this._quotaBox.add_child(this._apiRow.row);
        return this._quotaBox;
    }

    _quotaLine(key, value) {
        const row = new St.BoxLayout({ style_class: 'gphotos-upload-summary-row' });
        row.add_child(new St.Label({ text: key, style_class: 'gphotos-upload-summary-key' }));
        const v = new St.Label({ text: value, style_class: 'gphotos-upload-summary-val', x_expand: true });
        row.add_child(v);
        return { row, valueLabel: v };
    }

    _setSourceRows(sources) {
        for (const row of this._sourceRowLabels)
            row.forEach(label => label.destroy());
        this._sourceRowLabels = [];
        if (!sources || !sources.length) {
            const empty = new St.Label({ text: 'No source folders configured', style_class: 'gphotos-upload-grid-row' });
            this._sourcesLayout.attach(empty, 0, 1, 3, 1);
            this._sourceRowLabels.push([empty]);
            return;
        }
        sources.forEach((item, i) => {
            const labels = this._gridRow(this._sourcesLayout, i + 1, [sourceLabel(item), item.album || '—', sourceState(item)], false);
            this._sourceRowLabels.push(labels);
        });
    }

    async _bulkSourceAction(action) {
        const list = await this._sources.list();
        if (!list.ok)
            return this._showError(list.error || 'Could not load sources');
        for (const item of list.sources || []) {
            if (action === 'pause')
                await this._sources.pause(item.path);
            else if (action === 'resume')
                await this._sources.resume(item.path);
            else if (action === 'cancel')
                await this._sources.cancel(item.path);
        }
        await this._statusRunner.refreshNow(true);
    }

    async _onReconcile() {
        const payload = await this._actions.reconcile();
        if (!payload.ok)
            this._showError(payload.error || 'Reconcile failed');
        else
            this._onStatus(payload);
    }

    _onStatus(payload) {
        if (!payload || !payload.ok) {
            this._showError(payload?.error || 'refresh failed');
            return;
        }
        this._lastSnapshot = payload;
        this._paint(payload);
        this._refreshQuota();
    }

    async _refreshQuota() {
        const quota = await this._actions.storageQuota(false);
        if (quota.ok) {
            const usedPct = quota.total ? Math.round((quota.used / quota.total) * 100) : null;
            this._driveRow.valueLabel.text = usedPct !== null
                ? `${fmtBytes(quota.used)} / ${fmtBytes(quota.total)} (${usedPct}%)`
                : '—';
        }
    }

    _paint(snap) {
        const active = !!snap.active;
        this._icon.gicon = active ? this._iconOn : this._iconOff;
        this._toggleBtn.label = active ? '●  ON' : '●  OFF';
        this._toggleBtn.remove_style_class_name('gphotos-upload-on');
        this._toggleBtn.remove_style_class_name('gphotos-upload-off');
        this._toggleBtn.add_style_class_name(active ? 'gphotos-upload-on' : 'gphotos-upload-off');
        this._statusLine.text = snap.phase || '—';

        this._pctValue.text = fmtPercent(snap.pct);
        this._remainingValue.text = String(snap.remaining ?? '—');

        const speed = snap.speed || {};
        this._speedBadge.text = speed.label || '—';
        for (const cls of ['rate-stalled', 'rate-wait', 'rate-bad', 'rate-poor', 'rate-fair', 'rate-good', 'rate-excellent'])
            this._speedBadge.remove_style_class_name(`gphotos-upload-${cls}`);
        if (speed.css)
            this._speedBadge.add_style_class_name(`gphotos-upload-${speed.css}`);
        this._speedText.text = speed.text || '';

        this._setSourceRows(snap.sources);

        this._apiRow.valueLabel.text = `${snap.quota_used ?? 0} / ${snap.quota_limit ?? 10000} · resets ${snap.quota_reset_clock || ''}`;

        if (snap.worker_status?.error) {
            this._showError(snap.worker_status.error);
        } else {
            this._errorLabel.visible = false;
        }
    }

    _showError(message) {
        this._errorLabel.text = message;
        this._errorLabel.visible = true;
    }

    async _onToggle() {
        if (this._busy)
            return;
        this._busy = true;
        const active = this._lastSnapshot?.active;
        const payload = active ? await this._actions.serviceStop() : await this._actions.serviceStart();
        this._busy = false;
        if (!payload.ok) {
            this._showError(payload.error || 'toggle failed');
            return;
        }
        await this._statusRunner.refreshNow(true);
    }

    destroy() {
        this._statusRunner?.stopPolling();
        super.destroy();
    }
});
