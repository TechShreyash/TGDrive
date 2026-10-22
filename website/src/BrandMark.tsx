/** TGDrive: a file folder carrying a paper plane. Source: public/static/tgdrive.svg. */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <img
      className="brand-mark"
      src="/static/tgdrive.svg"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
    />
  );
}
