import errno
import os
import shutil
import stat
import sys
import tempfile
import pytest
from unittest.mock import MagicMock, patch, mock_open

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sync import (
    _is_not_found,
    _is_already_exists,
    FTPEntry,
    SMBAdapter,
    ensure_remote_dir,
    cleanup_remote,
    sync_frigate_to_sftp,
    upload_file,
)
from smbprotocol.exceptions import SMBOSError, NtStatus


class TestErrorClassification:
    def test_is_not_found_standard(self):
        assert _is_not_found(FileNotFoundError("file not found"))
        assert _is_not_found(OSError(errno.ENOENT, "No such file or directory"))

    def test_is_not_found_smb(self):
        e1 = SMBOSError(ntstatus=NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, filename="test.txt")
        assert _is_not_found(e1)

        e2 = SMBOSError(ntstatus=NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, filename="folder/test.txt")
        assert _is_not_found(e2)

        # Non-matching SMBOSError
        e3 = SMBOSError(ntstatus=NtStatus.STATUS_ACCESS_DENIED, filename="test.txt")
        assert not _is_not_found(e3)

    def test_is_already_exists_standard(self):
        assert _is_already_exists(FileExistsError("already exists"))
        assert _is_already_exists(OSError(errno.EEXIST, "File exists"))

    def test_is_already_exists_smb(self):
        e = SMBOSError(ntstatus=NtStatus.STATUS_OBJECT_NAME_COLLISION, filename="folder")
        assert _is_already_exists(e)

        e_other = SMBOSError(ntstatus=NtStatus.STATUS_ACCESS_DENIED, filename="folder")
        assert not _is_already_exists(e_other)


class TestSMBAdapter:
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_full_path_formatting(self, mock_reg, mock_stat):
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        assert adapter.base_path == r"\\192.168.1.112\asastar"

        assert adapter._full_path("") == r"\\192.168.1.112\asastar"
        assert adapter._full_path("/") == r"\\192.168.1.112\asastar"
        assert adapter._full_path(None) == r"\\192.168.1.112\asastar"
        assert adapter._full_path("/frigate_backup") == r"\\192.168.1.112\asastar\frigate_backup"
        assert adapter._full_path("frigate_backup/cam1/file.mp4") == r"\\192.168.1.112\asastar\frigate_backup\cam1\file.mp4"
        assert adapter._full_path("\\frigate_backup\\cam1\\") == r"\\192.168.1.112\asastar\frigate_backup\cam1"

    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_stat_success(self, mock_reg, mock_stat):
        mock_result = MagicMock()
        mock_result.st_mode = stat.S_IFREG | 0o644
        mock_result.st_mtime = 1700000000.5
        mock_stat.return_value = mock_result

        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        res = adapter.stat("/frigate_backup/recording.mp4.zip")

        assert isinstance(res, FTPEntry)
        assert res.filename == "recording.mp4.zip"
        assert res.st_mode == stat.S_IFREG
        assert res.st_mtime == 1700000000

    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_stat_not_found(self, mock_reg, mock_stat):
        mock_stat.side_effect = SMBOSError(ntstatus=NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, filename="missing.txt")
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)

        with pytest.raises(FileNotFoundError):
            adapter.stat("/missing.txt")

    @patch("sync.smbclient.scandir")
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_listdir_attr_success(self, mock_reg, mock_stat, mock_scandir):
        entry_dir = MagicMock()
        entry_dir.name = "subfolder"
        entry_dir.is_dir.return_value = True
        dir_stat = MagicMock()
        dir_stat.st_mtime = 1700000010
        entry_dir.stat.return_value = dir_stat

        entry_file = MagicMock()
        entry_file.name = "video.mp4.zip"
        entry_file.is_dir.return_value = False
        file_stat = MagicMock()
        file_stat.st_mtime = 1700000020
        entry_file.stat.return_value = file_stat

        # Mock context manager returned by smbclient.scandir
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = [entry_dir, entry_file]
        mock_scandir.return_value = mock_cm

        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        entries = adapter.listdir_attr("/frigate_backup")

        assert len(entries) == 2
        assert entries[0].filename == "subfolder"
        assert stat.S_ISDIR(entries[0].st_mode)
        assert entries[0].st_mtime == 1700000010

        assert entries[1].filename == "video.mp4.zip"
        assert stat.S_ISREG(entries[1].st_mode)
        assert entries[1].st_mtime == 1700000020

    @patch("sync.smbclient.scandir")
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_listdir_attr_not_found(self, mock_reg, mock_stat, mock_scandir):
        mock_scandir.side_effect = SMBOSError(ntstatus=NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, filename="missing_dir")
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)

        with pytest.raises(FileNotFoundError):
            adapter.listdir_attr("/missing_dir")

    @patch("sync.shutil.copyfileobj")
    @patch("sync.smbclient.open_file")
    @patch("builtins.open", new_callable=mock_open, read_data=b"data")
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_put_buffered_streaming(self, mock_reg, mock_stat, mock_file_open, mock_smb_open, mock_copyfileobj):
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        adapter.put("/local/file.zip", "/frigate_backup/file.zip")

        mock_copyfileobj.assert_called_once()
        # Verify length buffer passed
        _, kwargs = mock_copyfileobj.call_args
        assert kwargs.get("length") == 64 * 1024

    @patch("sync.smbclient.utime")
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_utime_preservation(self, mock_reg, mock_stat, mock_utime):
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        adapter.utime("/frigate_backup/file.zip", (1700000000, 1700000000))

        mock_utime.assert_called_once_with(
            r"\\192.168.1.112\asastar\frigate_backup\file.zip",
            times=(1700000000, 1700000000),
        )

    @patch("sync.smbclient.reset_connection_cache")
    @patch("sync.smbclient.stat")
    @patch("sync.smbclient.register_session")
    def test_close_resets_connection_cache(self, mock_reg, mock_stat, mock_reset):
        adapter = SMBAdapter("192.168.1.112", "user", "pass", "asastar", retries=1)
        adapter.close()
        mock_reset.assert_called_once()


class TestSafetyGuards:
    def test_cleanup_remote_refuses_root(self):
        mock_sftp = MagicMock()
        cutoff = 1700000000
        # Root variations
        assert cleanup_remote(mock_sftp, "/", cutoff) == 0
        assert cleanup_remote(mock_sftp, "", cutoff) == 0
        assert cleanup_remote(mock_sftp, ".", cutoff) == 0
        assert cleanup_remote(mock_sftp, "\\", cutoff) == 0

        # listdir_attr should NOT have been called
        mock_sftp.listdir_attr.assert_not_called()

    def test_cleanup_remote_deletes_old_files(self):
        mock_sftp = MagicMock()
        from datetime import datetime, timezone
        cutoff = datetime(2026, 10, 25, tzinfo=timezone.utc)

        old_file = FTPEntry("old.mp4.zip", stat.S_IFREG, datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
        new_file = FTPEntry("new.mp4.zip", stat.S_IFREG, datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp())
        mock_sftp.listdir_attr.return_value = [old_file, new_file]

        removed = cleanup_remote(mock_sftp, "/frigate_backup/cam1", cutoff)
        assert removed == 1
        mock_sftp.remove.assert_called_once_with("/frigate_backup/cam1/old.mp4.zip")

    @patch("sync.REMOTE_BASE_PATH", "/")
    def test_sync_frigate_to_sftp_refuses_root(self):
        with pytest.raises(ValueError, match="CRITICAL SAFETY ERROR: REMOTE_BASE_PATH cannot be '/'"):
            sync_frigate_to_sftp()

    @patch("sync.REMOTE_BASE_PATH", "")
    def test_sync_frigate_to_sftp_refuses_empty(self):
        with pytest.raises(ValueError, match="CRITICAL SAFETY ERROR: REMOTE_BASE_PATH cannot be '/'"):
            sync_frigate_to_sftp()


class TestUploadFile:
    @patch("sync.os.path.getmtime", return_value=1700000000)
    @patch("sync.ensure_remote_dir")
    def test_upload_file_retries_on_failure(self, mock_ensure, mock_mtime):
        mock_sftp = MagicMock()
        # Fail first call, succeed on second
        mock_sftp.put.side_effect = [OSError("temporary network glitch"), None]

        success = upload_file(mock_sftp, "/local/path.zip", "/remote/path.zip")
        assert success is True
        assert mock_sftp.put.call_count == 2
        mock_sftp.utime.assert_called_once_with("/remote/path.zip", (1700000000, 1700000000))


class TestEnsureRemoteDir:
    def test_ensure_remote_dir_hierarchy(self):
        mock_sftp = MagicMock()
        # Pretend /backup/frigate does not exist, but /backup does
        def mock_stat(path):
            if path in ("/backup/frigate", "backup/frigate"):
                raise FileNotFoundError(path)
            return FTPEntry(os.path.basename(path), stat.S_IFDIR, 0)

        mock_sftp.stat.side_effect = mock_stat

        ensure_remote_dir(mock_sftp, "/backup/frigate")

        mock_sftp.mkdir.assert_called_once_with("/backup/frigate")
        # Ensure it never attempts to mkdir on "/"
        for call in mock_sftp.mkdir.call_args_list:
            assert call.args[0] not in ("/", "\\", "")
