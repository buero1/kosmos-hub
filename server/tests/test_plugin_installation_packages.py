import io
import json
import zipfile
from urllib.parse import parse_qs, urlparse

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


def test_wordpress_org_catalog_search_returns_only_safe_display_metadata(monkeypatch):
    payload = {
        "info": {"page": 2, "pages": 7, "results": 121},
        "plugins": [
            {
                "slug": "sample-plugin",
                "name": "Sample Plugin",
                "short_description": "A safe sample plugin.",
                "version": "2.0.0",
                "rating": 94,
                "num_ratings": 48,
                "active_installs": 100000,
                "last_updated": "2026-08-30 12:00pm GMT",
                "requires": "6.5",
                "tested": "7.1",
                "icons": {"2x": "https://ps.w.org/sample-plugin/assets/icon-256x256.png"},
            },
            {"slug": "not valid!", "name": "Ignored"},
        ],
    }
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.services.plugin_installation_packages.urlopen", fake_urlopen)
    service = object.__new__(PluginInstallationPackageService)

    catalog = service.search_wordpress_org_plugins(search="sample plugin", browse="new", page=2)

    assert captured["timeout"] == 15
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["action"] == ["query_plugins"]
    assert query["request[search]"] == ["sample plugin"]
    assert "request[browse]" not in query
    assert catalog.page == 2
    assert catalog.pages == 7
    assert catalog.total == 121
    assert len(catalog.items) == 1
    assert catalog.items[0].name == "Sample Plugin"
    assert catalog.items[0].icon_url == "https://ps.w.org/sample-plugin/assets/icon-256x256.png"
