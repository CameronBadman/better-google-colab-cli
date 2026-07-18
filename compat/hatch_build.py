"""Build metadata that locks the compatibility shim to the core version."""

from hatchling.metadata.plugin.interface import MetadataHookInterface
from setuptools_scm import get_version


class CustomMetadataHook(MetadataHookInterface):
    """Derive both packages from the same Git version and pin them exactly."""

    def update(self, metadata: dict) -> None:
        version = get_version(root="..", relative_to=__file__)
        metadata["version"] = version
        metadata["dependencies"] = [f"better-google-colab-cli=={version}"]
