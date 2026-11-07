export default function PerformanceExplanation() {
    return (
        <section
            className="performance-section performance-explanation"
            aria-labelledby="performance-explanation-heading"
        >
            <h3 id="performance-explanation-heading">
                Performance explanation
            </h3>
            <p className="settings-help">
                Performance explanations are unavailable from this server. Raw
                measurements remain available below.
            </p>
            <details className="performance-detail-note">
                <summary>How to interpret measurements</summary>
                <p>
                    Useful throughput and responsive playback matter more than
                    filling CPU or memory. Idle resources can be appropriate
                    when no work can benefit.
                </p>
                <p>
                    Telegram reads and HTTP delivery measure different parts of
                    the path. Cached bytes can be delivered without another
                    Telegram read. Low throughput alone cannot locate a network
                    bottleneck, and delivery backpressure does not identify the
                    client as its cause.
                </p>
            </details>
        </section>
    );
}
