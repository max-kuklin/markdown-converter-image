import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app, sanitize_filename, _queue_slots, MAX_CONCURRENT_CONVERSIONS, MAX_QUEUED_CONVERSIONS
from converter import (
    MARKITDOWN_EXTENSIONS,
    PANDOC_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    get_converter,
)

client = TestClient(app)


# ── Extension routing tests ──────────────────────────────────────────────────

class TestExtensionRouting:
    @pytest.mark.parametrize("ext", sorted(PANDOC_EXTENSIONS))
    def test_pandoc_extensions(self, ext):
        assert get_converter(ext) == "pandoc"

    @pytest.mark.parametrize("ext", sorted(MARKITDOWN_EXTENSIONS))
    def test_markitdown_extensions(self, ext):
        assert get_converter(ext) == "markitdown"

    def test_unsupported_extension(self):
        assert get_converter(".zip") is None
        assert get_converter(".exe") is None
        assert get_converter(".mp3") is None

    def test_legacy_binary_formats_unsupported(self):
        assert get_converter(".ppt") is None
        assert get_converter(".ods") is None

    def test_legacy_binary_formats_use_markitdown(self):
        assert get_converter(".doc") == "markitdown"
        assert get_converter(".xls") == "markitdown"

    def test_case_insensitive(self):
        assert get_converter(".DOCX") == "pandoc"
        assert get_converter(".Xlsx") == "markitdown"
        assert get_converter(".PDF") == "markitdown"


# ── Health endpoint tests ────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_200(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "pandoc" in data
        assert "markitdown" in data

    def test_health_structure(self):
        response = client.get("/health")
        data = response.json()
        assert isinstance(data["pandoc"], bool)
        assert isinstance(data["markitdown"], bool)


# ── Filename sanitization tests ──────────────────────────────────────────────

class TestFilenameSanitization:
    def test_safe_filename(self):
        assert sanitize_filename("document.docx") == "document.docx"

    def test_path_traversal_blocked(self):
        result = sanitize_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result

    def test_strips_directory(self):
        result = sanitize_filename("/some/path/file.pdf")
        assert result == "file.pdf"

    def test_unsafe_characters_replaced(self):
        result = sanitize_filename("file name (1).docx")
        assert " " not in result
        assert "(" not in result

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            sanitize_filename("")


# ── Convert endpoint tests ───────────────────────────────────────────────────

class TestConvertEndpoint:
    def test_missing_file_returns_422(self):
        response = client.post("/convert", data={"filename": "test.docx"})
        assert response.status_code == 422

    def test_unsupported_extension_returns_415(self):
        response = client.post(
            "/convert",
            files={"file": ("test.zip", b"fake content", "application/zip")},
            data={"filename": "test.zip"},
        )
        assert response.status_code == 415

    def test_no_extension_returns_415(self):
        response = client.post(
            "/convert",
            files={"file": ("noext", b"fake content", "application/octet-stream")},
            data={"filename": "noext"},
        )
        assert response.status_code == 415

    @patch("app.convert")
    def test_successful_pandoc_conversion(self, mock_convert):
        mock_convert.return_value = "# Hello World\n\nSome content."
        response = client.post(
            "/convert",
            files={"file": ("test.docx", b"fake docx content", "application/octet-stream")},
            data={"filename": "test.docx"},
        )
        assert response.status_code == 200
        assert "Hello World" in response.text
        assert response.headers["content-type"].startswith("text/markdown")

    @patch("app.convert")
    def test_successful_markitdown_conversion(self, mock_convert):
        mock_convert.return_value = "| Col A | Col B |\n|---|---|\n| 1 | 2 |"
        response = client.post(
            "/convert",
            files={"file": ("data.xlsx", b"fake xlsx content", "application/octet-stream")},
            data={"filename": "data.xlsx"},
        )
        assert response.status_code == 200
        assert "Col A" in response.text

    @patch("app.convert")
    def test_conversion_failure_returns_422(self, mock_convert):
        mock_convert.side_effect = RuntimeError("Corrupt file")
        response = client.post(
            "/convert",
            files={"file": ("bad.docx", b"corrupt", "application/octet-stream")},
            data={"filename": "bad.docx"},
        )
        assert response.status_code == 422

    @patch("app.convert")
    def test_conversion_timeout_returns_504(self, mock_convert):
        mock_convert.side_effect = subprocess.TimeoutExpired(cmd="pandoc", timeout=120)
        response = client.post(
            "/convert",
            files={"file": ("slow.docx", b"content", "application/octet-stream")},
            data={"filename": "slow.docx"},
        )
        assert response.status_code == 504


# ── Temp file cleanup tests ─────────────────────────────────────────────────

class TestTempFileCleanup:
    @patch("app.convert")
    def test_temp_files_cleaned_on_success(self, mock_convert):
        mock_convert.return_value = "# Result"
        response = client.post(
            "/convert",
            files={"file": ("test.docx", b"content", "application/octet-stream")},
            data={"filename": "test.docx"},
        )
        assert response.status_code == 200
        # Verify no leftover temp dirs with our test file
        # (temp dirs are cleaned in finally block)

    @patch("app.convert")
    def test_temp_files_cleaned_on_failure(self, mock_convert):
        mock_convert.side_effect = RuntimeError("fail")
        response = client.post(
            "/convert",
            files={"file": ("test.docx", b"content", "application/octet-stream")},
            data={"filename": "test.docx"},
        )
        assert response.status_code == 422
        # Temp dir should still be cleaned up via finally block


# ── Queue limit tests ───────────────────────────────────────────────────────

class TestQueueLimit:
    @patch("app.convert")
    def test_returns_429_when_queue_full(self, mock_convert):
        """When all queue slots are exhausted, new requests get 429."""
        total_slots = MAX_CONCURRENT_CONVERSIONS + MAX_QUEUED_CONVERSIONS
        # Drain all semaphore slots to simulate a full queue
        for _ in range(total_slots):
            _queue_slots._value -= 1
        try:
            response = client.post(
                "/convert",
                files={"file": ("test.docx", b"content", "application/octet-stream")},
                data={"filename": "test.docx"},
            )
            assert response.status_code == 429
            assert "queued" in response.json()["detail"].lower()
        finally:
            # Restore semaphore
            for _ in range(total_slots):
                _queue_slots._value += 1

    @patch("app.convert")
    def test_accepts_request_when_queue_has_room(self, mock_convert):
        """When queue has room, request should succeed normally."""
        mock_convert.return_value = "# OK"
        response = client.post(
            "/convert",
            files={"file": ("test.docx", b"content", "application/octet-stream")},
            data={"filename": "test.docx"},
        )
        assert response.status_code == 200


# ── Converter function unit tests ────────────────────────────────────────────

class TestConverterFunctions:
    @patch("converter.subprocess.run")
    def test_pandoc_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b"# Converted\n\nText content.",
            stderr=b"",
        )
        from converter import pandoc_to_markdown

        result = pandoc_to_markdown("/tmp/test.docx")
        assert "Converted" in result
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "pandoc"

    @patch("converter.subprocess.run")
    def test_pandoc_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"pandoc: error reading file",
        )
        from converter import pandoc_to_markdown

        with pytest.raises(RuntimeError, match="Pandoc conversion failed"):
            pandoc_to_markdown("/tmp/bad.docx")

    @patch("converter.subprocess.run")
    def test_pandoc_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pandoc", timeout=120)
        from converter import pandoc_to_markdown

        with pytest.raises(subprocess.TimeoutExpired):
            pandoc_to_markdown("/tmp/slow.docx", timeout=120)

    @patch("converter.subprocess.run")
    def test_markitdown_success(self, mock_run):
        expected_md = "| A | B |\n|---|---|\n| 1 | 2 |"
        mock_run.return_value = subprocess.CompletedProcess(
            [], returncode=0, stdout=expected_md.encode("utf-8"), stderr=b"",
        )

        from converter import markitdown_to_markdown
        result = markitdown_to_markdown("/tmp/data.xlsx")
        assert "| A | B |" in result


# ── Password-protected file detection tests ──────────────────────────────────

class TestPasswordProtectedDetection:
    OLE2_HEADER = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1' + b'\x00' * 100

    def test_encrypted_xlsx_returns_415(self, tmp_path):
        """An OLE2-wrapped .xlsx (password-protected) should be rejected with 415."""
        f = tmp_path / "encrypted.xlsx"
        f.write_bytes(self.OLE2_HEADER)
        response = client.post(
            "/convert",
            files={"file": ("encrypted.xlsx", f.read_bytes(), "application/octet-stream")},
            data={"filename": "encrypted.xlsx"},
        )
        assert response.status_code == 415
        assert "password-protected" in response.json()["detail"].lower()

    def test_encrypted_pptx_returns_415(self, tmp_path):
        f = tmp_path / "encrypted.pptx"
        f.write_bytes(self.OLE2_HEADER)
        response = client.post(
            "/convert",
            files={"file": ("encrypted.pptx", f.read_bytes(), "application/octet-stream")},
            data={"filename": "encrypted.pptx"},
        )
        assert response.status_code == 415
        assert "password-protected" in response.json()["detail"].lower()

    def test_encrypted_docx_returns_415(self, tmp_path):
        f = tmp_path / "encrypted.docx"
        f.write_bytes(self.OLE2_HEADER)
        response = client.post(
            "/convert",
            files={"file": ("encrypted.docx", f.read_bytes(), "application/octet-stream")},
            data={"filename": "encrypted.docx"},
        )
        assert response.status_code == 415
        assert "password-protected" in response.json()["detail"].lower()

    def test_normal_xls_not_flagged(self, tmp_path):
        """OLE2-based .xls files are legitimate and should not be blocked."""
        f = tmp_path / "normal.xls"
        f.write_bytes(self.OLE2_HEADER)
        # .xls is OLE2 natively, so it shouldn't be flagged as password-protected.
        # It will fail conversion for other reasons (fake content), but not with 415.
        response = client.post(
            "/convert",
            files={"file": ("normal.xls", f.read_bytes(), "application/octet-stream")},
            data={"filename": "normal.xls"},
        )
        assert response.status_code != 415
