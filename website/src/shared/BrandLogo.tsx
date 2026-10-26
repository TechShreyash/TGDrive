/** One accessible name and a wordmark that follows the chosen app appearance. */
export default function BrandLogo({
    size = "default",
}: {
    size?: "compact" | "default" | "large";
}) {
    return (
        <span
            className={`brand-logo brand-logo-${size}`}
            role="img"
            aria-label="TGDrive"
        >
            <img
                className="brand-logo-light"
                src="/static/brand/tgdrive-logo.svg"
                width="2010"
                height="401"
                alt=""
                aria-hidden="true"
            />
            <img
                className="brand-logo-dark"
                src="/static/brand/tgdrive-logo-dark.svg"
                width="2010"
                height="401"
                alt=""
                aria-hidden="true"
            />
            {size === "compact" && (
                <img
                    className="brand-logo-icon"
                    src="/static/brand/tgdrive-icon.svg"
                    width="512"
                    height="512"
                    alt=""
                    aria-hidden="true"
                />
            )}
        </span>
    );
}
