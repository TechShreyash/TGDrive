/** TGDrive's storage symbol. Source: public/static/tgdrive-symbol.svg. */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <img
      className="brand-mark"
      src="/static/tgdrive-symbol.svg"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
    />
  );
}
