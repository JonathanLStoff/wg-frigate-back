#!/usr/bin/env python3
"""
Frigate Backup Script - Uploads last 7 days of recordings to an SFTP server.
"""

import os
import sys
import paramiko
import time
from datetime import datetime, timedelta, timezone
from stat import S_ISDIR
import logging

# --- CONFIGURATION ---
LOCAL_RECORDINGS_PATH = os.getenv("LOCAL_RECORDINGS_PATH", "/media/frigate/recordings")  # Path to your Frigate recordings
SFTP_HOST = os.getenv("SFTP_HOST", "sftp.example.com")  # SFTP server hostname
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USERNAME = os.getenv("SFTP_USERNAME", "your-username")
SFTP_PRIVATE_KEY_PATH = os.getenv("SFTP_PRIVATE_KEY_PATH", None)  # or use password
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", None)  # Set if not using key
REMOTE_BASE_PATH = os.getenv("REMOTE_BASE_PATH", "/backup/frigate")  # Remote base directory
DAYS_TO_KEEP = int(os.getenv("DAYS_TO_KEEP", 7))
# --- END CONFIGURATION ---

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
    except FileNotFoundError:
        parent = os.path.dirname(remote_dir)
        if parent and parent != remote_dir:
            ensure_remote_dir(sftp, parent)
        sftp.mkdir(remote_dir)
        logger.debug(f"Created remote directory: {remote_dir}")

def upload_file(sftp, local_path, remote_path):
    """Upload a single file, creating directories as needed."""
    try:
        remote_dir = os.path.dirname(remote_path)
        ensure_remote_dir(sftp, remote_dir)
        sftp.put(local_path, remote_path)
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
    Also removes empty directories.
    """
    try:
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = os.path.join(remote_dir, entry.filename)
            if S_ISDIR(entry.st_mode):
                cleanup_remote(sftp, remote_path, cutoff_time)
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
                    logger.info(f"Removed old remote file: {remote_path}")
    except FileNotFoundError:
        pass

def sync_frigate_to_sftp():
    """Main sync function."""
    cutoff = get_cutoff_time(DAYS_TO_KEEP)
    logger.info(f"Syncing files newer than: {cutoff.isoformat()}")

    # Connect to SFTP
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if SFTP_PRIVATE_KEY_PATH and os.path.exists(SFTP_PRIVATE_KEY_PATH):
            key = paramiko.RSAKey.from_private_key_file(SFTP_PRIVATE_KEY_PATH)
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USERNAME, pkey=key)
        elif SFTP_PASSWORD:
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USERNAME, password=SFTP_PASSWORD)
        else:
            raise ValueError("No authentication method provided (key or password).")
        logger.info(f"Connected to {SFTP_HOST}")
    except Exception as e:
        logger.error(f"SFTP connection failed: {e}")
        sys.exit(1)

    sftp = ssh.open_sftp()

    try:
        # Ensure remote base directory exists
        ensure_remote_dir(sftp, REMOTE_BASE_PATH)

        # Walk local recordings directory
        uploaded_count = 0
        for root, dirs, files in os.walk(LOCAL_RECORDINGS_PATH):
            for file in files:
                local_path = os.path.join(root, file)
                # Skip if file is older than cutoff
                if not should_upload_file(local_path, cutoff):
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
                    pass  # File doesn't exist remotely, upload it

                if upload_file(sftp, local_path, remote_path):
                    uploaded_count += 1

        logger.info(f"Uploaded {uploaded_count} new/updated files.")

        # Cleanup remote files older than cutoff
        logger.info("Starting remote cleanup...")
        cleanup_remote(sftp, REMOTE_BASE_PATH, cutoff)
        logger.info("Remote cleanup complete.")

    finally:
        sftp.close()
        ssh.close()

if __name__ == "__main__":
    sync_frigate_to_sftp()