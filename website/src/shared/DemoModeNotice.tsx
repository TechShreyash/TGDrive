export default function DemoModeNotice() {
    return (
        <p className="settings-help demo-mode-notice" role="note">
            Public demo: configuration and management are read-only. You can
            inspect settings and try the drive’s file features. Settings
            changes, connection changes, backup actions and recovery-key
            downloads are disabled.
        </p>
    );
}
