import GLib from 'gi://GLib';

import { DEPENDENCY_ERROR } from './messages.js';

const VENV_SERVICE = GLib.build_filenamev([
    GLib.get_home_dir(),
    '.local',
    'share',
    'gphotos-upload-widget',
    'venv',
    'bin',
    'gphotos-upload-service',
]);

export function isBackendInstalled() {
    return GLib.file_test(VENV_SERVICE, GLib.FileTest.IS_EXECUTABLE);
}

export async function ensureBackend() {
    if (isBackendInstalled())
        return { ok: true };

    return {
        ok: false,
        error: DEPENDENCY_ERROR,
    };
}
