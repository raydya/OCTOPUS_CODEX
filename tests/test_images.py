import base64
import io
import struct
import unittest
import zlib
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from octopus.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    ImageInputError,
    prepare_image,
)


def encoded_image(format="PNG", mode="RGB", size=(8, 6), **options):
    with Image.new(mode, size, "red") as image, io.BytesIO() as output:
        image.save(output, format=format, **options)
        return output.getvalue()


def decode_url(url):
    prefix, payload = url.split(",", 1)
    return prefix, base64.b64decode(payload, validate=True)


class ImageTests(unittest.TestCase):
    def test_supported_formats_are_decoded_to_standard_images(self):
        for format in ("PNG", "JPEG", "WEBP", "GIF"):
            with self.subTest(format=format):
                prefix, data = decode_url(prepare_image(encoded_image(format)))
                target = "JPEG" if format == "JPEG" else "PNG"
                self.assertEqual(prefix, f"data:image/{target.lower()};base64")
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    self.assertEqual(image.format, target)
                    self.assertEqual(image.size, (8, 6))

    def test_transparency_is_preserved(self):
        with Image.new("RGBA", (3, 2), (10, 20, 30, 40)) as source, io.BytesIO() as stream:
            source.save(stream, format="PNG")
            _, data = decode_url(prepare_image(stream.getvalue()))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.getpixel((0, 0)), (10, 20, 30, 40))

    def test_png_metadata_is_removed(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("secret", "private-place")
        source = encoded_image(pnginfo=metadata, icc_profile=b"private-profile")
        _, data = decode_url(prepare_image(source))
        self.assertNotIn(b"private-place", data)
        self.assertNotIn(b"private-profile", data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.info, {})

    def test_exif_is_applied_then_removed(self):
        exif = Image.Exif()
        exif[274] = 6  # 90 degrees clockwise.
        exif[270] = "private-place"
        _, data = decode_url(prepare_image(encoded_image("JPEG", exif=exif)))
        self.assertNotIn(b"private-place", data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (6, 8))
            self.assertFalse(image.getexif())
            self.assertNotIn("exif", image.info)

    def test_animated_images_are_rejected_instead_of_using_first_frame(self):
        for format in ("GIF", "PNG", "WEBP"):
            with self.subTest(format=format):
                with Image.new("RGB", (2, 2), "red") as first:
                    with Image.new("RGB", (2, 2), "blue") as second, io.BytesIO() as output:
                        first.save(output, format=format, save_all=True, append_images=[second], duration=100)
                        with self.assertRaisesRegex(ImageInputError, "动态"):
                            prepare_image(output.getvalue())

    def test_invalid_and_unsupported_images_fail_safely(self):
        for data in (b"", None, "not bytes", b"secret-invalid-content", encoded_image("BMP")):
            with self.subTest(data_type=type(data).__name__):
                with self.assertRaises(ImageInputError) as error:
                    prepare_image(data)
                self.assertNotIn("secret", str(error.exception))
        png = encoded_image()
        for data in (png[:30], png[:-15], png[:50] + b"corrupt" + png[57:]):
            with self.subTest(data_length=len(data)):
                with self.assertRaises(ImageInputError):
                    prepare_image(data)

    def test_oversize_input_is_rejected_before_opening_decoder(self):
        with patch("octopus.images.Image.open") as open_image:
            with self.assertRaisesRegex(ImageInputError, "超过 5 MB"):
                prepare_image(b"x" * (MAX_IMAGE_BYTES + 1))
        open_image.assert_not_called()

    def test_oversize_dimensions_are_rejected_before_decoding_pixels(self):
        # Build real PNG headers with valid CRC, without allocating large images.
        for width, height in ((4000, 3001), (MAX_IMAGE_DIMENSION + 1, 1), (100_000, 100_000)):
            with self.subTest(size=(width, height)):
                header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
                chunk = b"IHDR" + header
                data = (
                    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk
                    + struct.pack(">I", zlib.crc32(chunk))
                    + b"\x00\x00\x00\x00IDAT\x35\xaf\x06\x1e"
                )
                with patch("PIL.PngImagePlugin.PngImageFile.load") as load:
                    with self.assertRaises(ImageInputError):
                        prepare_image(data)
                load.assert_not_called()

    def test_exact_dimension_and_pixel_limits_are_allowed(self):
        self.assertEqual(MAX_IMAGE_PIXELS, 12_000_000)
        # Exercise boundary logic with lowered limits, avoiding large test allocations.
        with patch("octopus.images.MAX_IMAGE_PIXELS", 48), patch(
            "octopus.images.MAX_IMAGE_DIMENSION", 8
        ):
            prepare_image(encoded_image(size=(8, 6)))
            with self.assertRaisesRegex(ImageInputError, "尺寸过大"):
                prepare_image(encoded_image(size=(8, 7)))
            with self.assertRaisesRegex(ImageInputError, "尺寸过大"):
                prepare_image(encoded_image(size=(9, 1)))

    def test_reencoded_output_is_also_bounded(self):
        # A small palette GIF expands to a larger PNG even for a valid, tiny image.
        source = encoded_image("GIF")
        with patch("octopus.images.MAX_IMAGE_BYTES", len(source)):
            with self.assertRaisesRegex(ImageInputError, "转换后"):
                prepare_image(source)

    def test_decoder_and_encoder_exception_details_are_not_exposed(self):
        for target in ("octopus.images.Image.open", "PIL.Image.Image.save"):
            source = encoded_image()
            with self.subTest(target=target):
                with patch(target, side_effect=RuntimeError("private attacker data")):
                    with self.assertRaisesRegex(ImageInputError, "^图片无法读取") as error:
                        prepare_image(source)
                self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
