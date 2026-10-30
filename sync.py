#!/usr/bin/env python3
"""
Frigate Backup Script - Uploads recordings to an SFTP, FTP, or SMB server.
"""

import csv
import errno
import ftplib
import logging
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from stat import S_ISDIR, S_IFDIR, S_IFREG
import paramiko

try:
    import smbclient
    from smbprotocol.exceptions import SMBOSError
except ImportError:
    smbclient = None
    SMBOSError = None

# --- CONFIGURATION ---
LOCAL_RECORDINGS_PATH = os.getenv("LOCAL_RECORDINGS_PATH", "/media/frigate/recordings")  # Path to your Frigate recordings
USE_FTP = os.getenv("USE_FTP", "false").lower() == "true"  # Optional: use FTP instead of SFTP
USE_SMB = os.getenv("USE_SMB", "false").lower() == "true"  # Optional: use SMB instead of SFTP
SMB_VOLUME = os.getenv("SMB_VOLUME", None)  # Required if USE_SMB=true (e.g. 'backup')
SFTP_HOST = os.getenv("SFTP_HOST", "sftp.example.com")  # Remote server hostname or IP
SFTP_PORT = int(os.getenv("SFTP_PORT", 21 if USE_FTP else (445 if USE_SMB else 22)))
SMB_PORT = int(os.getenv("SMB_PORT", str(SFTP_PORT) if USE_SMB else "445"))
SMB_TIMEOUT = int(os.getenv("SMB_TIMEOUT", "30"))
CONNECT_RETRIES = int(os.getenv("CONNECT_RETRIES", "3"))
CONNECT_RETRY_DELAY = int(os.getenv("CONNECT_RETRY_DELAY", "5"))
SFTP_USERNAME = os.getenv("SFTP_USERNAME", "your-username")
SFTP_PRIVATE_KEY_PATH = os.getenv("SFTP_PRIVATE_KEY_PATH", None)  # or use password
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", None)  # Set if not using key
REMOTE_BASE_PATH = os.getenv("REMOTE_BASE_PATH", "/backup/frigate")  # Remote base directory (must be a dedicated subfolder)
DAYS_TO_KEEP = int(os.getenv("DAYS_TO_KEEP", 7))
LOG_FILE = os.getenv("LOG_FILE", "/logs/sync.csv")  # Optional: log to a file
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
# --- END CONFIGURATION ---

# Setup logging
logging.basicConfig(
    level=logging.INFO if not DEBUG else logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _is_not_found(e: Exception) -> bool:
    """Check if an exception indicates a file or directory was not found (including SMBOSError)."""
    if isinstance(e, FileNotFoundError):
        return True
    if isinstance(e, OSError) and getattr(e, "errno", None) == errno.ENOENT:
        return True
    ntstatus = getattr(e, "ntstatus", None)
    # 0xC0000034: STATUS_OBJECT_NAME_NOT_FOUND, 0xC000003A: STATUS_OBJECT_PATH_NOT_FOUND
    if ntstatus in (0xC0000034, 0xC000003A):
        return True
    msg = str(e).lower()
    return any(term in msg for term in ("0xc0000034", "0xc000003a", "not found", "no such file"))


def _is_already_exists(e: Exception) -> bool:
    """Check if an exception indicates a directory/file already exists (including SMBOSError)."""
    if isinstance(e, FileExistsError):
        return True
    if isinstance(e, OSError) and getattr(e, "errno", None) == errno.EEXIST:
        return True
    ntstatus = getattr(e, "ntstatus", None)
    # 0xC0000035: STATUS_OBJECT_NAME_COLLISION
    if ntstatus == 0xC0000035:
        return True
    msg = str(e).lower()
    return any(term in msg for term in ("0xc0000035", "already exists", "file exists"))


def _is_unsafe_remote_path(path: str) -> bool:
    """Return True if a remote path resolves to root, empty, current dir, or uses '..' traversal."""
    if not path or not path.strip():
        return True
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if not parts or ".." in parts:
        return True
    return False


class FTPEntry:
    """Directory entry with the attributes cleanup/sync code reads (paramiko-style)."""
    def __init__(self, filename, st_mode, st_mtime):
        self.filename = filename
        self.st_mode = st_mode
        self.st_mtime = st_mtime


class FTPAdapter:
    """
    Wraps ftplib.FTP with the subset of paramiko's SFTPClient API this script
    uses, so the sync/cleanup logic works unchanged over plain FTP.
    """

    def __init__(self, host, port, username, password):
        self._ftp = ftplib.FTP()
        self._ftp.connect(host, port, timeout=60)
        self._ftp.login(username, password)
        self._mfmt_supported = True

    @staticmethod
    def _parse_ftp_time(value):
        # RFC 3659 time-val: "YYYYMMDDHHMMSS[.sss]", always UTC
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()

    def stat(self, path):
        try:
            resp = self._ftp.voidcmd(f"MDTM {path}")  # succeeds for files only
            return FTPEntry(os.path.basename(path), S_IFREG, self._parse_ftp_time(resp[4:].strip()))
        except ftplib.error_perm:
            pass
        original_dir = self._ftp.pwd()
        try:
            self._ftp.cwd(path)
            self._ftp.cwd(original_dir)
            return FTPEntry(os.path.basename(path), S_IFDIR, 0)
        except ftplib.error_perm:
            raise FileNotFoundError(path)

    def listdir_attr(self, path):
        try:
            entries = []
            for name, facts in self._ftp.mlsd(path):
                if facts.get("type") in ("cdir", "pdir"):
                    continue
                mode = S_IFDIR if facts.get("type") == "dir" else S_IFREG
                mtime = self._parse_ftp_time(facts["modify"]) if "modify" in facts else 0
                entries.append(FTPEntry(name, mode, mtime))
            return entries
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                raise FileNotFoundError(path) from e
            return self._listdir_fallback(path)  # server without MLSD support

    def _listdir_fallback(self, path):
        entries = []
        try:
            names = self._ftp.nlst(path)
        except ftplib.error_perm as e:
            raise FileNotFoundError(path) from e
        for full in names:
            name = os.path.basename(full.rstrip("/"))
            if name in ("", ".", ".."):
                continue
            try:
                resp = self._ftp.voidcmd(f"MDTM {path}/{name}")
                entries.append(FTPEntry(name, S_IFREG, self._parse_ftp_time(resp[4:].strip())))
            except ftplib.error_perm:
                entries.append(FTPEntry(name, S_IFDIR, 0))
        return entries

    def put(self, local_path, remote_path):
        with open(local_path, "rb") as f:
            self._ftp.storbinary(f"STOR {remote_path}", f)

    def utime(self, path, times):
        if not self._mfmt_supported:
            return
        ts = datetime.fromtimestamp(times[1], tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        try:
            self._ftp.voidcmd(f"MFMT {ts} {path}")
        except ftplib.error_perm:
            self._mfmt_supported = False
            logger.warning("FTP server does not support MFMT; remote mtimes will be upload times, "
                           "so retention is counted from upload day instead of recording day.")

    def mkdir(self, path):
        self._ftp.mkd(path)

    def rmdir(self, path):
        try:
            self._ftp.rmd(path)
        except ftplib.error_perm as e:
            raise OSError(str(e)) from e  # e.g. directory not empty; caller expects OSError

    def remove(self, path):
        self._ftp.delete(path)

    def close(self):
        try:
            self._ftp.quit()
        except Exception:
            self._ftp.close()


class SMBAdapter:
    """
    Wraps smbprotocol's smbclient with a paramiko-style SFTPClient API subset.
    Reuses SFTP credentials (host, username, password) for SMB authentication.
    """

    def __init__(self, host, username, password, smb_volume, port=445, timeout=30, retries=3, retry_delay=5):
        if smbclient is None:
            raise ImportError("smbprotocol is not installed. Install it with: pip install smbprotocol")
        self.host = host
        self.username = username
        self.password = password
        self.smb_volume = (smb_volume or "backup").replace("/", "\\").strip("\\")
        self.port = port
        self.timeout = timeout
        self.base_path = f"\\\\{self.host}\\{self.smb_volume}"

        logger.info(f"SMBAdapter: Registering session for {self.base_path} (port {self.port}, timeout {self.timeout}s)")
        smbclient.register_session(
            self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            connection_timeout=self.timeout,
        )
        logger.info(f"SMBAdapter: Credentials registered for {self.host}")

        # Verify connection with retries to tolerate slow WireGuard handshakes or transient network drops
        retries = max(1, int(retries))
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                logger.info(f"SMBAdapter: Verifying connection to {self.base_path} (attempt {attempt}/{retries})...")
                smbclient.stat(self.base_path)
                logger.info(f"SMBAdapter: Successfully connected to {self.base_path}")
                return
            except Exception as e:
                last_err = e
                if _is_not_found(e):
                    # Server responded, even if share root stat varies across SMB server implementations
                    logger.info(f"SMBAdapter: Share {self.base_path} responded.")
                    return
                logger.warning(f"SMBAdapter: Connection attempt {attempt}/{retries} failed: {e}")
                if attempt < retries:
                    try:
                        smbclient.reset_connection_cache()
                    except Exception:
                        pass
                    time.sleep(retry_delay * attempt)
                    try:
                        smbclient.register_session(
                            self.host,
                            username=self.username,
                            password=self.password,
                            port=self.port,
                            connection_timeout=self.timeout,
                        )
                    except Exception:
                        pass

        if last_err is not None:
            logger.error(f"SMBAdapter: All {retries} connection attempts to {self.base_path} failed: {last_err}")
            raise last_err

    def _full_path(self, path: str) -> str:
        """Convert a relative or absolute remote path into a valid UNC path \\\\host\\share\\rel."""
        if not path:
            return self.base_path
        rel = path.replace("/", "\\").strip("\\")
        if not rel or rel == ".":
            return self.base_path
        return f"{self.base_path}\\{rel}"

    def stat(self, path: str) -> FTPEntry:
        """Get file/directory stats (paramiko-style)."""
        full_path = self._full_path(path)
        try:
            stat_info = smbclient.stat(full_path)
            mode = S_IFDIR if (stat_info.st_mode & 0o170000) == 0o040000 else S_IFREG
            mtime = int(stat_info.st_mtime)
            basename = os.path.basename(path.replace("\\", "/").rstrip("/"))
            return FTPEntry(basename, mode, mtime)
        except Exception as e:
            if _is_not_found(e):
                raise FileNotFoundError(path) from e
            logger.error(f"SMB stat error for {path} ({full_path}): {type(e).__name__}: {e}")
            raise

    def listdir_attr(self, path: str):
        """List directory contents with attributes using smbclient.scandir."""
        full_path = self._full_path(path)
        try:
            entries = []
            with smbclient.scandir(full_path) as it:
                for entry in it:
                    if entry.name in ("", ".", ".."):
                        continue
                    try:
                        stat_info = entry.stat()
                        mode = S_IFDIR if entry.is_dir() else S_IFREG
                        mtime = int(stat_info.st_mtime)
                        entries.append(FTPEntry(entry.name, mode, mtime))
                    except Exception as entry_err:
                        logger.warning(f"SMB listdir_attr: skipping entry {entry.name} due to error: {entry_err}")
            logger.debug(f"SMB listdir: {path} found {len(entries)} entries")
            return entries
        except Exception as e:
            if _is_not_found(e):
                logger.debug(f"SMB listdir_attr FileNotFoundError: {path}")
                raise FileNotFoundError(path) from e
            logger.error(f"SMB listdir_attr error for {path} ({full_path}): {type(e).__name__}: {e}")
            raise

    def put(self, local_path: str, remote_path: str):
        """Upload a file in chunks to prevent memory spikes with large recordings."""
        full_path = self._full_path(remote_path)
        with open(local_path, "rb") as local_file:
            with smbclient.open_file(full_path, mode="wb") as remote_file:
                shutil.copyfileobj(local_file, remote_file, length=64 * 1024)

    def utime(self, path: str, times):
        """Set file modification time on remote SMB storage."""
        full_path = self._full_path(path)
        try:
            smbclient.utime(full_path, times=times)
            logger.debug(f"SMB: Preserved mtime for {path}: {times}")
        except Exception as e:
            logger.warning(f"Could not set mtime for {path} ({full_path}): {e}")

    def mkdir(self, path: str):
        """Create a directory."""
        clean = path.replace("\\", "/").strip("/")
        if not clean or clean == ".":
            return
        full_path = self._full_path(path)
        try:
            smbclient.mkdir(full_path)
            logger.debug(f"SMB mkdir created: {full_path}")
        except Exception as e:
            if _is_already_exists(e):
                return
            logger.error(f"SMB mkdir error for {path} ({full_path}): {type(e).__name__}: {e}")
            raise

    def rmdir(self, path: str):
        """Remove an empty directory."""
        clean = path.replace("\\", "/").strip("/")
        if not clean or clean == ".":
            raise OSError("Refusing to remove root directory")
        full_path = self._full_path(path)
        try:
            smbclient.rmdir(full_path)
            logger.debug(f"SMB rmdir removed: {full_path}")
        except Exception as e:
            if _is_not_found(e):
                return
            # cleanup_remote expects OSError if directory is not empty
            raise OSError(str(e)) from e

    def remove(self, path: str):
        """Remove a file."""
        full_path = self._full_path(path)
        try:
            smbclient.remove(full_path)
            logger.debug(f"SMB remove removed: {full_path}")
        except Exception as e:
            if _is_not_found(e):
                return
            logger.error(f"SMB remove error for {path} ({full_path}): {type(e).__name__}: {e}")
            raise

    def close(self):
        """Close the SMB session and clear connection cache."""
        try:
            logger.debug("SMB session closing")
            smbclient.reset_connection_cache()
        except Exception as e:
            logger.warning(f"Error closing SMB session: {e}")


def write_log(status, uploaded, removed, error=""):
    """Append one row per sync run to the CSV log, creating it (with header) if needed."""
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        write_header = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "status", "files_uploaded", "files_removed", "error"])
            writer.writerow([datetime.now(timezone.utc).isoformat(), status, uploaded, removed, error])
    except OSError as e:
        logger.error(f"Could not write log file {LOG_FILE}: {e}")


def get_cutoff_time(days):
    """Return a datetime object for the cutoff time."""
    return datetime.now(timezone.utc) - timedelta(days=days)


def should_upload_file(file_path, cutoff_time):
    """Determine if a file should be uploaded based on its modification time."""
    try:
        mtime = os.path.getmtime(file_path)
        file_time = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return file_time >= cutoff_time
    except OSError:
        return False


def ensure_remote_dir(sftp, remote_dir):
    """Recursively create remote directories if they don't exist."""
    clean_dir = remote_dir.replace("\\", "/").rstrip("/")
    if not clean_dir or clean_dir == ".":
        return

    try:
        sftp.stat(clean_dir)
        logger.debug(f"Remote directory exists: {clean_dir}")
    except (FileNotFoundError, OSError) as e:
        if not _is_not_found(e):
            logger.error(f"Error checking directory {clean_dir}: {e}")
            raise
        logger.debug(f"Remote directory not found, creating: {clean_dir}")
        parent = os.path.dirname(clean_dir)
        if parent and parent != clean_dir and parent not in ("/", "\\", "."):
            ensure_remote_dir(sftp, parent)
        try:
            sftp.mkdir(clean_dir)
            logger.info(f"Created remote directory: {clean_dir}")
        except Exception as mk_err:
            if not _is_already_exists(mk_err):
                logger.error(f"Failed to create directory {clean_dir}: {mk_err}")
                raise


def compress_file(local_path):
    """Compress a single local file into a temporary zip archive and return its path."""
    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(local_path, arcname=os.path.basename(local_path))
    return zip_path


def upload_file(sftp, local_path, remote_path, mtime=None):
    """Upload a single file with retries, creating directories as needed."""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            remote_dir = os.path.dirname(remote_path)
            ensure_remote_dir(sftp, remote_dir)
            sftp.put(local_path, remote_path)
            # Preserve the original recording's mtime so "already copied" checks and
            # remote cleanup work off the recording time, not the upload time.
            local_mtime = int(mtime if mtime is not None else os.path.getmtime(local_path))
            sftp.utime(remote_path, (local_mtime, local_mtime))
            logger.info(f"Uploaded: {local_path} -> {remote_path}")
            return True
        except Exception as e:
            logger.warning(f"Upload attempt {attempt}/{max_retries} failed for {local_path}: {e}")
            try:
                sftp.remove(remote_path)
            except Exception:
                pass
            if attempt < max_retries:
                time.sleep(2 * attempt)
            else:
                logger.error(f"Failed to upload {local_path} after {max_retries} attempts: {e}")
                return False


def cleanup_remote(sftp, remote_dir, cutoff_time):
    """
    Recursively delete remote files older than cutoff_time.
    Also removes empty directories. Returns the number of files removed.
    """
    # Safety guard: NEVER run cleanup on the root directory
    if _is_unsafe_remote_path(remote_dir):
        logger.error(f"SAFETY GUARD: Refusing to run cleanup on unsafe/root directory: {remote_dir!r}")
        return 0

    removed_count = 0
    try:
        entries = sftp.listdir_attr(remote_dir)
    except Exception as e:
        if _is_not_found(e):
            return 0
        logger.error(f"Error listing remote directory {remote_dir} during cleanup: {e}")
        return 0

    clean_remote_dir = remote_dir.replace("\\", "/").rstrip("/")
    for entry in entries:
        remote_path = f"{clean_remote_dir}/{entry.filename}"
        if S_ISDIR(entry.st_mode):
            removed_count += cleanup_remote(sftp, remote_path, cutoff_time)
            # Remove directory if empty
            try:
                sftp.rmdir(remote_path)
                logger.info(f"Removed empty remote directory: {remote_path}")
            except OSError:
                pass  # Directory not empty or cannot be removed
        else:
            # Check if file is older than cutoff
            file_time = datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc)
            if file_time < cutoff_time:
                try:
                    sftp.remove(remote_path)
                    removed_count += 1
                    logger.info(f"Removed old remote file: {remote_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove old file {remote_path}: {e}")

    return removed_count


def sync_frigate_to_sftp():
    """Main sync function."""
    if _is_unsafe_remote_path(REMOTE_BASE_PATH):
        raise ValueError(
            "CRITICAL SAFETY ERROR: REMOTE_BASE_PATH cannot be '/', empty, or contain '..'. "
            "Cleanup scans REMOTE_BASE_PATH and deletes all files older than DAYS_TO_KEEP. "
            "Setting REMOTE_BASE_PATH to root would delete non-Frigate data on your SMB share "
            "or storage (e.g., other folders like 'mqtt'). "
            "Please configure REMOTE_BASE_PATH to a dedicated subfolder (e.g. REMOTE_BASE_PATH=/frigate_backup)."
        )

    if DAYS_TO_KEEP <= 0:
        raise ValueError(f"DAYS_TO_KEEP must be a positive integer, got {DAYS_TO_KEEP}")

    cutoff = get_cutoff_time(DAYS_TO_KEEP)
    logger.info(f"Syncing files newer than: {cutoff.isoformat()}")

    # Connect to the remote server (FTP, SFTP, or SMB)
    ssh = None
    if USE_SMB:
        protocol = "SMB"
    elif USE_FTP:
        protocol = "FTP"
    else:
        protocol = "SFTP"

    try:
        if USE_SMB:
            if not SFTP_PASSWORD:
                raise ValueError("SMB mode requires SFTP_PASSWORD for authentication.")
            if not SMB_VOLUME:
                raise ValueError("SMB mode requires SMB_VOLUME to be set.")
            sftp = SMBAdapter(
                SFTP_HOST,
                SFTP_USERNAME,
                SFTP_PASSWORD,
                SMB_VOLUME,
                port=SMB_PORT,
                timeout=SMB_TIMEOUT,
                retries=CONNECT_RETRIES,
                retry_delay=CONNECT_RETRY_DELAY,
            )
        elif USE_FTP:
            if not SFTP_PASSWORD:
                raise ValueError("FTP mode requires SFTP_PASSWORD (key auth is SFTP-only).")
            sftp = FTPAdapter(SFTP_HOST, SFTP_PORT, SFTP_USERNAME, SFTP_PASSWORD)
        else:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if SFTP_PRIVATE_KEY_PATH:
                if not os.path.exists(SFTP_PRIVATE_KEY_PATH):
                    raise ValueError(f"SFTP private key not found: {SFTP_PRIVATE_KEY_PATH}")
                key = paramiko.RSAKey.from_private_key_file(SFTP_PRIVATE_KEY_PATH)
                ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USERNAME, pkey=key)
            elif SFTP_PASSWORD:
                ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USERNAME, password=SFTP_PASSWORD)
            else:
                raise ValueError("No authentication method provided (key or password).")
            sftp = ssh.open_sftp()
        logger.info(f"Connected to {SFTP_HOST} via {protocol}")
    except Exception as e:
        logger.error(f"{protocol} connection failed: {e}")
        raise RuntimeError(f"{protocol} connection failed: {e}") from e

    try:
        logger.info(f"Local recordings path: {LOCAL_RECORDINGS_PATH}")
        logger.info(f"Remote base path: {REMOTE_BASE_PATH}")

        # Ensure remote base directory exists
        logger.info("Ensuring remote base directory exists...")
        ensure_remote_dir(sftp, REMOTE_BASE_PATH)
        logger.info("Remote base directory ready")

        # Cleanup remote files older than cutoff first, so stale recordings are
        # purged before we spend time/bandwidth uploading new ones.
        logger.info("Starting remote cleanup...")
        removed_count = cleanup_remote(sftp, REMOTE_BASE_PATH, cutoff)
        logger.info(f"Remote cleanup complete. Removed {removed_count} old files.")

        # Walk local recordings directory
        logger.info(f"Walking local directory: {LOCAL_RECORDINGS_PATH}")
        uploaded_count = 0
        failed_count = 0
        total_files = 0

        for root, dirs, files in os.walk(LOCAL_RECORDINGS_PATH):
            logger.debug(f"Scanning directory: {root} ({len(files)} files)")
            total_files += len(files)
            for file in files:
                local_path = os.path.join(root, file)
                # Skip if file is older than cutoff
                if not should_upload_file(local_path, cutoff):
                    logger.debug(f"Skipping old file: {local_path}")
                    continue

                # Build remote path preserving folder structure; files are
                # zip-compressed before upload, so the remote name gets a .zip suffix
                rel_path = os.path.relpath(local_path, LOCAL_RECORDINGS_PATH)
                remote_path = os.path.join(REMOTE_BASE_PATH, rel_path).replace("\\", "/") + ".zip"
                local_mtime = int(os.path.getmtime(local_path))

                # Check if file already exists remotely and is up-to-date
                try:
                    remote_attr = sftp.stat(remote_path)
                    remote_mtime = int(remote_attr.st_mtime)
                    # If remote file is newer or same age, skip
                    if remote_mtime >= local_mtime:
                        logger.debug(f"Skipping (up-to-date): {remote_path}")
                        continue
                except Exception:
                    # If stat fails (file not found or any error), we'll try to upload
                    logger.debug(f"Remote file check failed (will attempt upload): {remote_path}")
                    pass

                zip_path = compress_file(local_path)
                try:
                    if upload_file(sftp, zip_path, remote_path, mtime=local_mtime):
                        uploaded_count += 1
                    else:
                        failed_count += 1
                finally:
                    os.remove(zip_path)

        logger.info(f"Scanned {total_files} total files, uploaded {uploaded_count} new/updated files, {failed_count} failed.")

        return uploaded_count, failed_count, removed_count

    except Exception as e:
        logger.error(f"Fatal error during sync: {e}", exc_info=True)
        raise
    finally:
        try:
            sftp.close()
            logger.debug("Closed remote connection")
        except Exception as e:
            logger.warning(f"Error closing connection: {e}")
        if ssh is not None:
            try:
                ssh.close()
                logger.debug("Closed SSH connection")
            except Exception as e:
                logger.warning(f"Error closing SSH: {e}")


if __name__ == "__main__":
    try:
        uploaded, failed, removed = sync_frigate_to_sftp()
    except Exception as e:
        logger.exception("Sync failed")
        write_log("failure", 0, 0, error=str(e))
        sys.exit(1)

    if failed:
        write_log("failure", uploaded, removed,
                  error=f"{failed} of {uploaded + failed} uploads failed (see container logs)")
        sys.exit(1)

    write_log("success", uploaded, removed)
