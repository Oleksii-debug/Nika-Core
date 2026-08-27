from __future__ import annotations

from nika_core.packaging.notices import _resolve_license_evidence

_CLR_LOADER_MIT = """MIT License

Copyright (c) 2019-2026 Benedikt Reinartz

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def test_clr_loader_exact_reviewed_license_file_resolves_to_mit() -> None:
    assert _resolve_license_evidence(
        "clr_loader",
        "0.3.1",
        None,
        (("clr_loader-0.3.1.dist-info/licenses/LICENSE", _CLR_LOADER_MIT),),
    ) == ("MIT", "reviewed-license-file-fingerprint")


def test_reviewed_license_file_is_bound_to_exact_version_and_text() -> None:
    texts = (("LICENSE", _CLR_LOADER_MIT),)
    assert _resolve_license_evidence("clr-loader", "0.3.2", None, texts) == (
        None,
        "unclassified-license-file",
    )
    assert _resolve_license_evidence(
        "clr-loader",
        "0.3.1",
        None,
        (("LICENSE", _CLR_LOADER_MIT + "\nchanged"),),
    ) == (None, "unclassified-license-file")


def test_package_metadata_remains_primary_license_authority() -> None:
    assert _resolve_license_evidence(
        "example",
        "1.0",
        "Apache-2.0",
        (("LICENSE", "untrusted text"),),
    ) == ("Apache-2.0", "package-metadata")
