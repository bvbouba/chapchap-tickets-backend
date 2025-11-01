from django.core.management.base import BaseCommand
from django.conf import settings
from saleor.thumbnail import ALLOWED_THUMBNAIL_FORMATS
from saleor.thumbnail.models import Thumbnail
from saleor.product.models import ProductMedia
from saleor.thumbnail.utils import ProcessedImage, get_thumbnail_size, prepare_thumbnail_file_name
from saleor.plugins.manager import allow_writer, get_plugins_manager
from saleor.thumbnail.models import Thumbnail
import logging
import os

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Generate thumbnails for all product images using Saleor's internal system"

    def add_arguments(self, parser):
        parser.add_argument(
            "--sizes",
            nargs="+",
            type=int,
            default=[64, 128, 1024],
            help="Thumbnail sizes to generate (pixels).",
        )
        parser.add_argument(
            "--format",
            type=str,
            default="png",
            choices=ALLOWED_THUMBNAIL_FORMATS,
            help="Image format to use for thumbnails (default: png)",
        )

    def handle(self, *args, **options):
        sizes = [get_thumbnail_size(s) for s in options["sizes"]]
        fmt = options["format"]

        self.stdout.write(f"Generating thumbnails in {fmt} for sizes: {sizes}")
        manager = get_plugins_manager(allow_replica=False)

        images = ProductMedia.objects.exclude(image="")

        for media in images:
            image = media.image
            if not image:
                continue

            self.stdout.write(f"Processing image: {image.name}")

            for size_px in sizes:
                thumbnail_file_name = prepare_thumbnail_file_name(image.name, size_px, fmt)
                try:
                    processed_image = ProcessedImage(image.name, size_px, fmt)
                    thumbnail_file, _ = processed_image.create_thumbnail()
                except FileNotFoundError:
                    logger.warning(f"Image file not found: {image.name}")
                    continue
                except ValueError as error:
                    logger.warning(f"Invalid image: {image.name} ({error})")
                    continue

                # Save the generated thumbnail in the DB and storage
                with allow_writer():
                    thumbnail = Thumbnail.objects.create(
                        size=size_px, format=fmt, product_media=media
                    )
                    thumbnail.image.save(thumbnail_file_name, thumbnail_file)
                    thumbnail.save()
                    setattr(thumbnail, "instance", media)
                    manager.thumbnail_created(thumbnail)

                self.stdout.write(self.style.SUCCESS(f"Created: {thumbnail_file_name}"))

        self.stdout.write(self.style.SUCCESS("✅ All thumbnails generated successfully!"))
