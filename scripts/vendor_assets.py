"""Download pinned browser assets and verify their npm package integrity."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

ASSETS = Path(__file__).resolve().parents[1] / "assets"
VERSIONS = {"katex": "0.19.0", "mermaid": "12.1.0"}


def vendor(name: str, version: str) -> None:
    """Extract only production assets and upstream license files."""
    with urlopen(
        f"https://registry.npmjs.org/{name}/{version}", timeout=60
    ) as response:
        metadata = json.load(response)
    with urlopen(metadata["dist"]["tarball"], timeout=60) as response:
        package = response.read()
    actual = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode()
    if actual != metadata["dist"]["integrity"]:
        raise ValueError(f"Integrity check failed for {name}@{version}")
    target = ASSETS / name
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = member.name.removeprefix("package/")
            selected = (
                path == "LICENSE"
                or (
                    name == "katex"
                    and (
                        path in {"dist/katex.min.js", "dist/katex.min.css"}
                        or path.startswith("dist/fonts/")
                        and path.endswith(".woff2")
                    )
                )
                or (
                    name == "mermaid"
                    and path
                    in {"dist/mermaid.min.js", "dist/mermaid.min.js.LICENSE.txt"}
                )
            )
            if not selected or not member.isfile():
                continue
            destination = (target / path.removeprefix("dist/")).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError(f"Invalid asset path: {path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extractfile(member).read())
    print(f"Vendored {name}@{version}")


if __name__ == "__main__":
    for name, version in VERSIONS.items():
        vendor(name, version)
