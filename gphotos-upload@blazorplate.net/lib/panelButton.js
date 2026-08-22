import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
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
        this._dateFixPollId = 0;
        this._dateFixCleanupUrl = '';
        this._menuHeightSource = 0;

        this._buildMenu();

        this.menu.connect('open-state-changed', (menu, open) => {
            if (open) {
                // Position is only final after the BoxPointer lays out; constrain
                // once immediately and again on the next idle ticks.
                this._scheduleMenuHeightConstraint();
                this._statusRunner.startPolling(payload => this._onStatus(payload));
                if (this._dateFixItem.visible)
                    this._refreshDateFix().catch(() => {});
            } else {
                this._cancelMenuHeightConstraint();
                this._statusRunner.stopPolling();
                this._stopDateFixPoll();
            }
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

    _addMenuItem(item) {
        this._contentSection.addMenuItem(item);
    }

    _cancelMenuHeightConstraint() {
        if (this._menuHeightSource) {
            GLib.source_remove(this._menuHeightSource);
            this._menuHeightSource = 0;
        }
    }

    _scheduleMenuHeightConstraint() {
        this._cancelMenuHeightConstraint();
        this._constrainMenuHeight();
        // Re-run after BoxPointer positions itself relative to the panel.
        let passes = 0;
        this._menuHeightSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
            this._constrainMenuHeight();
            passes += 1;
            if (passes >= 3 || !this.menu.isOpen) {
                this._menuHeightSource = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _constrainMenuHeight() {
        // Global scrollbar: ScrollView lives *inside* the BoxPointer chrome so
        // rounded corners stay intact. Cap height to the free work-area space
        // on the open side of the panel button (works for top or bottom panel,
        // any resolution / scale factor).
        if (!this._menuScroll)
            return;
        const monitor = Main.layoutManager.findMonitorForActor(this);
        if (!monitor)
            return;

        const workArea = Main.layoutManager.getWorkAreaForMonitor(monitor.index);
        const [, buttonY] = this.get_transformed_position();
        const buttonH = Math.max(this.height, 1);
        const themeContext = St.ThemeContext.get_for_stage(global.stage);
        const scale = themeContext.scale_factor || 1;

        // Prefer the side the menu actually opens toward.
        const above = Math.max(0, buttonY - workArea.y);
        const below = Math.max(0, (workArea.y + workArea.height) - (buttonY + buttonH));
        const openUp = above >= below;
        const freePx = (openUp ? above : below);

        // Leave room for BoxPointer padding/arrow/shadow so the chrome itself
        // is never pushed off-screen (that is what clipped the round corners).
        const chromePadPx = 48;
        const maxSt = Math.max(200, Math.floor((freePx - chromePadPx) / scale));
        this._menuScroll.style = `max-height: ${maxSt}px;`;
    }

    _buildMenu() {
        // One scrollable column for every section. Nested sample scrolls are
        // intentionally avoided — a single adaptive scrollbar is clearer.
        this._contentSection = new PopupMenu.PopupMenuSection();
        this._menuScroll = new St.ScrollView({
            style_class: 'gphotos-upload-menu-scroll',
            overlay_scrollbars: true,
            x_expand: true,
            y_expand: true,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            clip_to_allocation: true,
        });
        // Add the section actor (not .box) so we do not tear its hierarchy apart.
        const contentActor = this._contentSection.actor ?? this._contentSection;
        this._menuScroll.add_child(contentActor);

        const scrollHolder = new PopupMenu.PopupMenuSection();
        const holderBox = scrollHolder.box ?? scrollHolder.actor ?? scrollHolder;
        holderBox.add_child(this._menuScroll);
        this.menu.addMenuItem(scrollHolder);

        this._addMenuItem(this._wrap(this._buildHeader()));
        this._addMenuItem(this._wrap(this._buildToggleRow()));

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addMenuItem(this._wrap(this._buildQuickActionsRow()));

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addMenuItem(this._wrap(this._sectionLabel('OVERALL', true)));
        this._addMenuItem(this._wrap(this._buildHeroRow()));

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addMenuItem(this._wrap(this._sectionLabel('SPEED', true)));
        this._addMenuItem(this._wrap(this._buildSpeedRow()));

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addMenuItem(this._wrap(this._sectionLabel('SOURCES', false)));
        this._addMenuItem(this._wrap(this._buildSourcesGrid()));

        this._dateFixSeparator = new PopupMenu.PopupSeparatorMenuItem();
        this._addMenuItem(this._dateFixSeparator);
        this._dateFixHeading = this._wrap(this._sectionLabel('FIX DATES', false));
        this._addMenuItem(this._dateFixHeading);
        this._dateFixItem = this._wrap(this._buildDateFixSection());
        this._addMenuItem(this._dateFixItem);
        this._setDateFixVisible(false);

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addMenuItem(this._wrap(this._sectionLabel('STORAGE', false)));
        this._addMenuItem(this._wrap(this._buildQuotaRows()));

        this._errorLabel = new St.Label({ style_class: 'gphotos-upload-error', text: '', visible: false });
        this._errorLabel.clutter_text.line_wrap = true;
        this._addMenuItem(this._wrap(this._errorLabel));

        this._addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const settingsItem = new PopupMenu.PopupMenuItem('Settings…');
        settingsItem.connect('activate', () => this._extension.openPreferences());
        this._addMenuItem(settingsItem);
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
        const fixDatesBtn = new St.Button({ label: 'Fix dates', style_class: 'gphotos-upload-chip' });
        pauseBtn.connect('clicked', () => this._bulkSourceAction('pause'));
        resumeBtn.connect('clicked', () => this._bulkSourceAction('resume'));
        cancelBtn.connect('clicked', () => this._bulkSourceAction('cancel'));
        reconcileBtn.connect('clicked', () => this._onReconcile());
        fixDatesBtn.connect('clicked', () => this._onDateFixScan());
        row.add_child(pauseBtn);
        row.add_child(resumeBtn);
        row.add_child(cancelBtn);
        row.add_child(reconcileBtn);
        row.add_child(fixDatesBtn);
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

    _buildDateFixSection() {
        const box = new St.BoxLayout({ vertical: true, x_expand: true, style_class: 'gphotos-upload-datefix-box' });
        this._dateFixSummary = new St.Label({ text: '', style_class: 'gphotos-upload-datefix-summary' });
        this._dateFixSummary.clutter_text.line_wrap = true;
        box.add_child(this._dateFixSummary);

        this._dateFixSamples = new St.BoxLayout({ vertical: true, style_class: 'gphotos-upload-datefix-samples' });
        this._dateFixSamples.visible = false;
        box.add_child(this._dateFixSamples);

        this._dateFixNote = new St.Label({ text: '', style_class: 'gphotos-upload-datefix-note', visible: false });
        this._dateFixNote.clutter_text.line_wrap = true;
        box.add_child(this._dateFixNote);

        const buttons = new St.BoxLayout({ style_class: 'gphotos-upload-chip-row', x_expand: true, x_align: Clutter.ActorAlign.CENTER });
        this._dateFixApplyBtn = new St.Button({ label: 'Apply', style_class: 'gphotos-upload-chip', reactive: false });
        this._dateFixCancelBtn = new St.Button({ label: 'Cancel', style_class: 'gphotos-upload-chip' });
        this._dateFixOpenBtn = new St.Button({ label: 'Open album', style_class: 'gphotos-upload-chip', visible: false });
        this._dateFixApplyBtn.connect('clicked', () => this._onDateFixApply());
        this._dateFixCancelBtn.connect('clicked', () => this._onDateFixCancel());
        this._dateFixOpenBtn.connect('clicked', () => {
            if (this._dateFixCleanupUrl) {
                Gio.AppInfo.launch_default_for_uri(this._dateFixCleanupUrl, null);
                this.menu.close();
            }
        });
        buttons.add_child(this._dateFixApplyBtn);
        buttons.add_child(this._dateFixCancelBtn);
        buttons.add_child(this._dateFixOpenBtn);
        box.add_child(buttons);
        return box;
    }

    _setCleanupLink(url) {
        this._dateFixOpenBtn.visible = !!url;
        this._dateFixCleanupUrl = url || '';
    }

    _setDateFixVisible(visible) {
        this._dateFixSeparator.visible = visible;
        this._dateFixHeading.visible = visible;
        this._dateFixItem.visible = visible;
        if (visible && this.menu.isOpen)
            this._scheduleMenuHeightConstraint();
    }

    _setApplyEnabled(enabled) {
        this._dateFixApplyBtn.reactive = enabled;
        if (enabled)
            this._dateFixApplyBtn.remove_style_class_name('gphotos-upload-chip-disabled');
        else
            this._dateFixApplyBtn.add_style_class_name('gphotos-upload-chip-disabled');
    }

    _shortDate(value) {
        // Backend hands us ISO strings; show just the day, which is what
        // actually went wrong.
        return String(value || '').slice(0, 10) || '?';
    }

    async _onDateFixScan() {
        this._setDateFixVisible(true);
        this._setApplyEnabled(false);
        this._dateFixSamples.destroy_all_children();
        this._dateFixSummary.text = 'Scanning your Google Photos albums…';
        this._dateFixNote.visible = false;
        this._dateFixSamples.visible = false;
        this._setCleanupLink('');
        const payload = await this._actions.dateFixScan();
        if (!payload.ok) {
            this._dateFixSummary.text = payload.error || 'Scan could not start';
            return;
        }
        this._pollDateFix();
    }

    async _onDateFixApply() {
        if (!this._dateFixApplyBtn.reactive)
            return;
        this._setApplyEnabled(false);
        this._dateFixSummary.text = 'Applying…';
        const payload = await this._actions.dateFixApply();
        if (!payload.ok) {
            this._dateFixSummary.text = payload.error || 'Apply could not start';
            return;
        }
        this._pollDateFix();
    }

    async _onDateFixCancel() {
        const payload = await this._actions.dateFixCancel();
        if (!payload.ok)
            this._showError(payload.error || 'Could not cancel');
        this._stopDateFixPoll();
        this._setDateFixVisible(false);
    }

    _stopDateFixPoll() {
        if (this._dateFixPollId) {
            GLib.source_remove(this._dateFixPollId);
            this._dateFixPollId = 0;
        }
    }

    _pollDateFix() {
        this._stopDateFixPoll();
        this._dateFixPollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._dateFixPollId = 0;
            this._refreshDateFix().catch(() => {});
            return GLib.SOURCE_REMOVE;
        });
    }

    async _refreshDateFix() {
        const state = await this._actions.dateFixStatus();
        this._paintDateFix(state);
        if (state.running || !state.finished)
            this._pollDateFix();
    }

    _paintDateFix(state) {
        if (state.error) {
            this._dateFixSummary.text = state.error;
            this._setApplyEnabled(false);
            return;
        }
        if (!state.finished) {
            this._dateFixSummary.text = state.phase || 'Working…';
            return;
        }

        this._dateFixSummary.text = state.phase || 'Done';
        this._dateFixSamples.destroy_all_children();

        if (state.mode === 'scan') {
            const candidates = state.candidates || [];
            // candidates is a sample; the real total comes separately.
            const total = state.candidate_count ?? candidates.length;
            for (const item of candidates) {
                this._dateFixSamples.add_child(new St.Label({
                    text: `${item.filename}:  ${this._shortDate(item.current)} → ${this._shortDate(item.proposed)}`,
                    style_class: 'gphotos-upload-datefix-sample',
                }));
            }
            if (total > candidates.length) {
                this._dateFixSamples.add_child(new St.Label({
                    text: `…and ${total - candidates.length} more`,
                    style_class: 'gphotos-upload-datefix-sample',
                }));
            }
            this._dateFixSamples.visible = candidates.length > 0;
            this._setApplyEnabled(total > 0);
            this._dateFixNote.visible = total > 0;
            this._dateFixNote.text =
                'Applying re-uploads a corrected copy. Google cannot change a date in ' +
                'place, so each original stays in your timeline until you delete it.';
            if (this.menu.isOpen)
                this._scheduleMenuHeightConstraint();
            return;
        }

        this._dateFixSamples.visible = false;
        this._setApplyEnabled(false);
        this._dateFixNote.visible = true;
        const parts = [];
        const corralled = state.corralled || 0;
        if (corralled) {
            parts.push(`${corralled} original${corralled === 1 ? '' : 's'} moved to “${state.cleanup_album}” — open that album and delete them.`);
        } else if (state.fixed) {
            parts.push(`${state.fixed} wrong-date original${state.fixed === 1 ? '' : 's'} left in your timeline — delete them in Google Photos.`);
        }
        if (state.remaining)
            parts.push(`${state.remaining} more to go: run Fix dates again.`);
        if (state.cancelled)
            parts.push('Cancelled before finishing.');
        this._dateFixNote.text = parts.join(' ');
        this._setCleanupLink(state.cleanup_url);
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
        this._cancelMenuHeightConstraint();
        this._stopDateFixPoll();
        this._statusRunner?.stopPolling();
        super.destroy();
    }
});
