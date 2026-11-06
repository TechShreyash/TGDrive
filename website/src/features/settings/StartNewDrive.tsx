/** Keep the fresh-start entry visible while the server rejects in-place erasure. */
export default function StartNewDrive() {
    return (
        <section
            className="settings-section start-new-drive"
            aria-labelledby="start-new-drive-heading"
        >
            <h2 id="start-new-drive-heading">Start a new drive</h2>
            <p id="start-new-drive-unavailable">
                In-place reset is temporarily unavailable. To start empty,
                configure a fresh dedicated MongoDB database and a new storage
                channel. Keep the existing database, channel and encryption key
                for recovery.
            </p>
            <button
                type="button"
                className="danger-button"
                disabled
                aria-describedby="start-new-drive-unavailable"
            >
                Start new drive (unavailable)
            </button>
        </section>
    );
}
