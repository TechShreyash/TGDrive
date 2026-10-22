/** TGDrive's compact TG monogram. Source: public/static/tgdrive-mark.svg. */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <img
      className="brand-mark"
      src="/static/tgdrive-mark.svg"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
    />
  );
}
