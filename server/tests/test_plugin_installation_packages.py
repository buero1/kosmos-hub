import io
import zipfile

import pytest

from app.services.plugin_installation_packages import PluginInstallationPackageService, PluginPackageError


def build_plugin_zip(*, path="sample-plugin/sample-plugin.php", header="Plugin Name: Sample Plugin\nVersion: 1.2.3"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(path, "<?php\n/*\n" + header + "\n*/\n")
    return buffer.getvalue()


def test_plugin_zip_inspection_extracts_the_checked_wordpress_metadata():
    service = object.__new__(PluginInstallationPackageService)

    package = service.inspect_archive(
        package_bytes=build_plugin_zip(),
        original_filename="sample-plugin.zip",
    )

    assert package.plugin_file == "sample-plugin/sample-plugin.php"
    assert package.plugin_name == "Sample Plugin"
    assert package.plugin_version == "1.2.3"
    assert len(package.sha256) == 64


def test_plugin_zip_inspection_rejects_path_traversal():
    service = object.__new__(PluginInstallationPackageService)

    with pytest.raises(PluginPackageError, match="unsafe file path"):
        service.inspect_archive(
            package_bytes=build_plugin_zip(path="sample-plugin/../outside.php"),
            original_filename="unsafe.zip",
        )


def test_plugin_zip_inspection_rejects_multiple_plugin_headers():
    service = object.__new__(PluginInstallationPackageService)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("sample-plugin/one.php", "<?php\n/*\nPlugin Name: One\nVersion: 1.0\n*/")
        archive.writestr("sample-plugin/two.php", "<?php\n/*\nPlugin Name: Two\nVersion: 1.0\n*/")

    with pytest.raises(PluginPackageError, match="exactly one WordPress plugin header"):
        service.inspect_archive(package_bytes=package.getvalue(), original_filename="multiple.zip")
