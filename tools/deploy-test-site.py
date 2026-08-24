"""Deploy selected plugin files to a WordPress test site via FileZilla credentials."""

from __future__ import annotations

from argparse import ArgumentParser
from base64 import b64decode
from ftplib import FTP, error_perm
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable
from xml.etree import ElementTree


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "wordpress-plugin"
DEFAULT_SITE_NAME = "07-test-gasthof löwen"
DEFAULT_PLUGIN_SLUG = "kosmos-bridge"
COMMON_PLUGIN_ROOTS = (
    "html/wordpress/wp-content/plugins",
    "html/wp-content/plugins",
    "wp-content/plugins",
    "wordpress/wp-content/plugins",
)


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        help="Plugin files to deploy, relative to the wordpress-plugin directory. Defaults to all plugin files.",
    )
    parser.add_argument("--site", default=DEFAULT_SITE_NAME, help="Name of the FileZilla Site Manager profile.")
    parser.add_argument("--plugin-slug", default=DEFAULT_PLUGIN_SLUG, help="Remote plugin directory slug.")
    parser.add_argument("--remote-root", default="", help="Remote plugin directory. Auto-detected when omitted.")
    return parser.parse_args()


def filezilla_credentials(site_name: str) -> tuple[str, int, str, str]:
    manager_path = Path.home() / "AppData/Roaming/FileZilla/sitemanager.xml"
    root = ElementTree.parse(manager_path).getroot()
    server = next((node for node in root.findall(".//Server") if node.findtext("Name") == site_name), None)

    if server is None:
        raise RuntimeError(f"FileZilla profile '{site_name}' was not found.")

    password_node = server.find("Pass")
    password = password_node.text or ""
    if password_node is not None and password_node.get("encoding") == "base64":
        password = b64decode(password).decode("utf-8")

    host = server.findtext("Host") or ""
    user = server.findtext("User") or ""
    port = int(server.findtext("Port") or "21")
    return host, port, user, password


def local_file(relative_path: Path) -> Path:
    source = (SOURCE_ROOT / relative_path).resolve()

    try:
        source.relative_to(SOURCE_ROOT)
    except ValueError as error:
        raise RuntimeError(f"'{relative_path}' is outside the plugin repository.") from error

    if not source.is_file():
        raise RuntimeError(f"'{relative_path}' is not a file in the plugin repository.")

    return source


def all_plugin_files() -> list[Path]:
    return sorted(
        p.relative_to(SOURCE_ROOT)
        for p in SOURCE_ROOT.rglob("*")
        if p.is_file() and "vendor" not in p.parts and "__pycache__" not in p.parts
    )


def ensure_remote_directory(ftp: FTP, remote_directory: str) -> None:
    current = PurePosixPath()

    for part in PurePosixPath(remote_directory).parts:
        current /= part

        try:
            ftp.mkd(current.as_posix())
        except error_perm as error:
            if not str(error).startswith("550"):
                raise


def remote_file_exists(ftp: FTP, remote_path: str) -> bool:
    try:
        ftp.size(remote_path)
        return True
    except error_perm:
        return False


def detect_remote_root(ftp: FTP, plugin_slug: str) -> str:
    for base in COMMON_PLUGIN_ROOTS:
        candidate = f"{base.rstrip('/')}/{plugin_slug}"
        if remote_file_exists(ftp, f"{candidate}/kosmos-bridge.php"):
            return candidate
    raise RuntimeError(
        f"Could not detect remote plugin root for '{plugin_slug}'. "
        "Use --remote-root to provide the exact plugin directory."
    )


def deploy_file(ftp: FTP, source: Path, relative_path: Path, remote_root: str) -> None:
    content = source.read_bytes()
    remote_path = f"{remote_root.rstrip('/')}/{relative_path.as_posix()}"
    ensure_remote_directory(ftp, PurePosixPath(remote_path).parent.as_posix())

    with source.open("rb") as stream:
        ftp.storbinary(f"STOR {remote_path}", stream)

    received = bytearray()
    ftp.retrbinary(f"RETR {remote_path}", received.extend)

    if sha256(content).digest() != sha256(received).digest():
        raise RuntimeError(f"Verification failed for {relative_path.as_posix()}.")

    print(f"Verified {relative_path.as_posix()}")


def normalize_file_list(files: Iterable[str]) -> list[Path]:
    if not files:
        return all_plugin_files()
    return [Path(path) for path in files]


def main() -> None:
    arguments = parse_arguments()
    relative_files = normalize_file_list(arguments.files)
    files = [(local_file(path), path) for path in relative_files]

    host, port, username, password = filezilla_credentials(arguments.site)
    ftp = FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(username, password)

    try:
        remote_root = arguments.remote_root or detect_remote_root(ftp, arguments.plugin_slug)
        print(f"Using remote root: {remote_root}")
        for source, relative_path in files:
            deploy_file(ftp, source, relative_path, remote_root)
    finally:
        ftp.quit()


if __name__ == "__main__":
    main()
