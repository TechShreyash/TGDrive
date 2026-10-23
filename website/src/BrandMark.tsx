/** Decorative TG interlock; use BrandLogo when the product name is needed. */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <img
      className="brand-mark"
      src="/static/brand/tgdrive-icon.svg"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
    />
  );
}
