import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import { GPhotosUploadIndicator } from './lib/panelButton.js';

export default class GPhotosUploadExtension extends Extension {
    enable() {
        this._indicator = new GPhotosUploadIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
