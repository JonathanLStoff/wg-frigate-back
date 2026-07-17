#!/usr/bin/env python3
"""
Frigate Backup Script - Uploads last 7 days of recordings to an SFTP (or FTP) server.
"""

import csv
import ftplib
import os
import sys
import paramiko
import time
from datetime import datetime, timedelta, timezone
from stat import S_ISDIR, S_IFDIR, S_IFREG
import logging
try:
    import smbclient
except ImportError:
    smbclient = None

# --- CONFIGURATION ---
LOCAL_RECORDINGS_PATH = os.getenv("LOCAL_RECORDINGS_PATH", "/media/frigate/recordings")  # Path to your Frigate recordings
USE_FTP = os.getenv("USE_FTP", "false").lower() == "true"  # Optional: use FTP instead of SFTP
USE_SMB = os.getenv("USE_SMB", "false").lower() == "true"  # Optional: use SMB instead of SFTP
SMB_VOLUME = os.getenv("SMB_VOLUME", None)  # Optional: SMB volume name if using SMB
SFTP_HOST = os.getenv("SFTP_HOST", "sftp.example.com")  # SFTP server hostname
SFTP_PORT = int(os.getenv("SFTP_PORT", 21 if USE_FTP else 22))
SFTP_USERNAME = os.getenv("SFTP_USERNAME", "your-username")
SFTP_PRIVATE_KEY_PATH = os.getenv("SFTP_PRIVATE_KEY_PATH", None)  # or use password
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", None)  # Set if not using key
REMOTE_BASE_PATH = os.getenv("REMOTE_BASE_PATH", "/backup/frigate")  # Remote base directory
DAYS_TO_KEEP = int(os.getenv("DAYS_TO_KEEP", 7))
LOG_FILE = os.getenv("LOG_FILE", "/logs/sync.csv")  # Optional: log to a file
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
# --- END CONFIGURATION ---

# Setup logging
logging.basicConfig(level=logging.INFO if not DEBUG else logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

    def __init__(self, host, username, password, smb_volume):
        if smbclient is None:
            raise ImportError("smbprotocol is not installed. Install it with: pip install smbprotocol")
        self.host = host
        self.username = username
        self.password = password
        self.smb_volume = smb_volume or "backup"
        self.base_path = f"//{host}/{self.smb_volume}"
        logger.info(f"SMBAdapter: Connecting to {self.base_path}")
        # Register credentials for smbclient
        smbclient.register_session(host, username=username, password=password)
        logger.info(f"SMBAdapter: Credentials registered for {host}")

    def stat(self, path):
        """Get file/directory stats (paramiko-style)."""
        try:
            full_path = f"{self.base_path}{path}"
            logger.debug(f"SMB stat: {full_path}")
            stat_info = smbclient.stat(full_path)
            is_dir = (stat_info.st_mode & 0o170000) == 0o040000
            mode = S_IFDIR if is_dir else S_IFREG
            mtime = int(stat_info.st_mtime)
            logger.debug(f"SMB stat OK: {path} (is_dir={is_dir})")
            return FTPEntry(os.path.basename(path), mode, mtime)
        except FileNotFoundError:
            logger.debug(f"SMB stat FileNotFoundError: {path}")
            raise FileNotFoundError(path)
        except Exception as e:
            logger.error(f"SMB stat error for {path}: {type(e).__name__}: {e}")
            raise

    def listdir_attr(self, path):
        """List directory contents with attributes."""
        try:
            full_path = f"{self.base_path}{path}"
            logger.debug(f"SMB listdir_attr: {full_path}")
            entries = []
            for entry in smbclient.listdir_attr(full_path):
                is_dir = (entry.st_mode & 0o170000) == 0o040000
                mode = S_IFDIR if is_dir else S_IFREG
                mtime = int(entry.st_mtime)
                entries.append(FTPEntry(entry.name, mode, mtime))
            logger.debug(f"SMB listdir_attr OK: {path} ({len(entries)} entries)")
            return entries
        except FileNotFoundError:
            logger.debug(f"SMB listdir_attr FileNotFoundError: {path}")
            raise FileNotFoundError(path)
        except Exception as e:
            logger.error(f"SMB listdir_attr error for {path}: {type(e).__name__}: {e}")
            raise

    def put(self, local_path, remote_path):
        """Upload a file."""
        try:
            full_path = f"{self.base_path}{remote_path}"
            logger.debug(f"SMB put: {local_path} -> {full_path}")
            with open(local_path, "rb") as local_file:
                with smbclient.open_file(full_path, mode="wb") as remote_file:
                    remote_file.write(local_file.read())
            logger.debug(f"SMB put OK: {remote_path}")
        except Exception as e:
            logger.error(f"SMB put error for {remote_path}: {type(e).__name__}: {e}")
            raise

    def utime(self, path, times):
        """Set file modification time."""
        try:
            full_path = f"{self.base_path}{path}"
            smbclient.stat(full_path)  # Verify file exists
            logger.debug(f"SMB: cannot preserve mtime for {path}, using transfer time instead")
        except Exception as e:
            logger.warning(f"Could not set mtime for {path}: {e}")

    def mkdir(self, path):
        """Create a directory."""
        full_path = f"{self.base_path}{path}"
        try:
            logger.debug(f"SMB mkdir: {full_path}")
            smbclient.mkdir(full_path)
            logger.debug(f"SMB mkdir OK: {path}")
        except FileExistsError:
            logger.debug(f"SMB mkdir FileExistsError (OK): {path}")
        except Exception as e:
            logger.error(f"SMB mkdir error for {path}: {type(e).__name__}: {e}")
            raise

    def rmdir(self, path):
        """Remove an empty directory."""
        try:
            full_path = f"{self.base_path}{path}"
            logger.debug(f"SMB rmdir: {full_path}")
            smbclient.rmdir(full_path)
            logger.debug(f"SMB rmdir OK: {path}")
        except Exception as e:
            logger.error(f"SMB rmdir error for {path}: {type(e).__name__}: {e}")
            raise

    def remove(self, path):
        """Remove a file."""
        try:
            full_path = f"{self.base_path}{path}"
            logger.debug(f"SMB remove: {full_path}")
            smbclient.remove(full_path)
            logger.debug(f"SMB remove OK: {path}")
        except Exception as e:
            logger.error(f"SMB remove error for {path}: {type(e).__name__}: {e}")
            raise

    def close(self):
        """Close the SMB session."""
        try:
            logger.debug("SMB session closing")
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

def parse_frigate_path(file_path):
    """
    Frigate stores recordings in: YYYY-MM-DD/HH/camera_name/MM.SS.mp4
    This extracts the file's date from the path.
    """
    try:
        # Split the path and get the date part (YYYY-MM-DD)
        parts = file_path.split(os.sep)
        # Find the part that looks like a date
        for part in parts:
            try:
                datetime.strptime(part, "%Y-%m-%d")
                return part
            except ValueError:
                continue
    except Exception:
        pass
    return None

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
    try:
        sftp.stat(remote_dir)
        logger.debug(f"Remote directory exists: {remote_dir}")
    except FileNotFoundError:
        logger.debug(f"Remote directory not found, creating: {remote_dir}")
        parent = os.path.dirname(remote_dir)
        if parent and parent != remote_dir:
            ensure_remote_dir(sftp, parent)
        try:
            sftp.mkdir(remote_dir)
            logger.info(f"Created remote directory: {remote_dir}")
        except Exception as e:
            logger.error(f"Failed to create directory {remote_dir}: {e}")
            raise
    except Exception as e:
        logger.error(f"Error checking/creating directory {remote_dir}: {e}")
        raise

def upload_file(sftp, local_path, remote_path):
    """Upload a single file, creating directories as needed."""
    try:
        remote_dir = os.path.dirname(remote_path)
        ensure_remote_dir(sftp, remote_dir)
        sftp.put(local_path, remote_path)
        # Preserve the local mtime so "already copied" checks and remote
        # cleanup work off the recording time, not the upload time.
        local_mtime = int(os.path.getmtime(local_path))
        sftp.utime(remote_path, (local_mtime, local_mtime))
        logger.info(f"Uploaded: {local_path} -> {remote_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to upload {local_path}: {e}")
        return False

def get_remote_files(sftp, remote_dir):
    """Recursively get all files and their modification times from remote."""
    files = {}
    try:
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = os.path.join(remote_dir, entry.filename)
            if S_ISDIR(entry.st_mode):
                files.update(get_remote_files(sftp, remote_path))
            else:
                files[remote_path] = entry.st_mtime
    except FileNotFoundError:
        pass
    return files

def cleanup_remote(sftp, remote_dir, cutoff_time):
    """
    Recursively delete remote files older than cutoff_time.
    Also removes empty directories. Returns the number of files removed.
    """
    removed_count = 0
    try:
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = os.path.join(remote_dir, entry.filename)
            if S_ISDIR(entry.st_mode):
                removed_count += cleanup_remote(sftp, remote_path, cutoff_time)
                # Remove directory if empty
                try:
                    sftp.rmdir(remote_path)
                    logger.info(f"Removed empty remote directory: {remote_path}")
                except OSError:
                    pass  # Directory not empty
            else:
                # Check if file is older than cutoff
                file_time = datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc)
                if file_time < cutoff_time:
                    sftp.remove(remote_path)
                    removed_count += 1
                    logger.info(f"Removed old remote file: {remote_path}")
    except FileNotFoundError:
        pass
    return removed_count

def sync_frigate_to_sftp():
    """Main sync function."""
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
            sftp = SMBAdapter(SFTP_HOST, SFTP_USERNAME, SFTP_PASSWORD, SMB_VOLUME)
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

                # Build remote path preserving folder structure
                rel_path = os.path.relpath(local_path, LOCAL_RECORDINGS_PATH)
                remote_path = os.path.join(REMOTE_BASE_PATH, rel_path).replace("\\", "/")

                # Check if file already exists remotely and is up-to-date
                try:
                    remote_attr = sftp.stat(remote_path)
                    remote_mtime = remote_attr.st_mtime
                    local_mtime = os.path.getmtime(local_path)
                    # If remote file is newer or same age, skip
                    if remote_mtime >= local_mtime:
                        logger.debug(f"Skipping (up-to-date): {remote_path}")
                        continue
                except FileNotFoundError:
                    logger.debug(f"Remote file not found, will upload: {remote_path}")
                except Exception as e:
                    logger.error(f"Error checking remote file {remote_path}: {e}")
                    failed_count += 1
                    continue

                if upload_file(sftp, local_path, remote_path):
                    uploaded_count += 1
                else:
                    failed_count += 1

        logger.info(f"Scanned {total_files} total files, uploaded {uploaded_count} new/updated files, {failed_count} failed.")

        # Cleanup remote files older than cutoff
        logger.info("Starting remote cleanup...")
        removed_count = cleanup_remote(sftp, REMOTE_BASE_PATH, cutoff)
        logger.info(f"Remote cleanup complete. Removed {removed_count} old files.")

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